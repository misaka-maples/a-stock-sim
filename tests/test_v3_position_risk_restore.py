"""Legacy risk-state restoration proves holding identity before raising lines."""
import copy
import hashlib
import json
import sqlite3

import pytest

from src.astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from src.astock.trade.position_risk_restore import restore_legacy_position_risk

STRATEGY='Product_AB_Balanced_v2'


def fixture(tmp_path):
    ledger=SQLiteAccountLedger(str(tmp_path/'ledger.sqlite'))
    for account in ('balanced','aggressive'):ledger.init_account(account,account,100000.)
    positions={
        'sh600261':{'symbol':'sh600261','total_shares':1300,'cost_price':5.2139,
            'cost_basis_cents':677807,'buy_lots':[{'date':'2026-11-09','shares':1300,'price':5.21}]},
        'sh603955':{'symbol':'sh603955','total_shares':100,'cost_price':32.0703,
            'cost_basis_cents':320703,'buy_lots':[{'date':'2026-11-09','shares':100,'price':32.02}]},
    }
    monitors={
        'sh600261':{'symbol':'sh600261','cost_price':5.2139,'peak_price':6.32,'trailing_stop_line':5.37},
        'sh603955':{'symbol':'sh603955','cost_price':32.0703,'peak_price':36.44,'trailing_stop_line':0.},
    }
    snapshot={'updated_at':'2026-11-15T01:08:37.256693+08:00','positions':positions,
        'strategy_state':{STRATEGY:{'position_monitors':monitors},
            'Product_AB':{'position_monitors':{s:{**m,'peak_price':100.,'trailing_stop_line':90.}
                                               for s,m in monitors.items()}}}}
    with ledger.connect() as conn:
        for symbol,p in positions.items():
            price=5.18 if symbol=='sh600261' else 33.35
            conn.execute('''INSERT INTO positions (account_id,symbol,name,sector,total_shares,
                tradable_shares,frozen_shares,avg_cost,last_price,market_value_cents,
                highest_price,trailing_stop_price,updated_at)
                VALUES ('balanced',?,'legacy','其他',?,?,0,?,?,?,?,0,'original-position-time')''',
                (symbol,p['total_shares'],p['total_shares'],p['cost_price'],price,
                 round(price*p['total_shares']*100),max(price,p['cost_price'])))
            lot=p['buy_lots'][0]
            conn.execute('''INSERT INTO position_lots (account_id,symbol,buy_date,shares,
                remaining_shares,cost_price,is_tradable_t1,fill_id)
                VALUES ('balanced',?,?,?,?,?,1,'MIGRATION_LOT')''',
                (symbol,lot['date'],lot['shares'],lot['shares'],lot['price']))
    path=tmp_path/'sim_account.json'
    return ledger,path,snapshot


def run(ledger,path,snapshot,strategy=STRATEGY):
    path.write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf8')
    return restore_legacy_position_risk(ledger,'balanced',path,strategy)


def financial_state(ledger):
    with ledger.connect() as conn:
        out={}
        for table in ('accounts','positions','position_lots','orders','fills','cash_journal','risk_events'):
            columns=[r['name'] for r in conn.execute('PRAGMA table_info('+table+')')
                     if r['name'] not in ('highest_price','trailing_stop_price')]
            out[table]=[tuple(r) for r in conn.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY rowid')]
        return out


def test_actual_legacy_shapes_restore_two_exact_holdings_and_source_audit_only(tmp_path):
    ledger,path,snapshot=fixture(tmp_path);before=financial_state(ledger)
    audit=run(ledger,path,snapshot)
    assert audit['status']=='RESTORED' and audit['updated_symbols']==['sh600261','sh603955']
    assert audit['source_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert audit['source_mtime_ns']==path.stat().st_mtime_ns
    assert audit['source_path']==str(path.resolve())
    restored=SQLiteAccountLedger(str(ledger.db_path)).get_positions('balanced')
    assert restored['sh600261'].highest_price==6.32 and restored['sh600261'].trailing_stop_price==5.37
    assert restored['sh603955'].highest_price==36.44 and restored['sh603955'].trailing_stop_price==0.
    assert all(p.entry_date=='2026-11-09' for p in restored.values())
    assert financial_state(ledger)==before
    assert ledger.get_positions('aggressive')=={}
    again=run(ledger,path,snapshot)
    assert again['updated_symbols']==[] and financial_state(ledger)==before


def test_never_lowers_more_recent_protection_or_uses_other_strategy_namespace(tmp_path):
    ledger,path,snapshot=fixture(tmp_path)
    with ledger.connect() as conn:
        conn.execute("UPDATE positions SET highest_price=7.,trailing_stop_price=6. WHERE symbol='sh600261'")
    run(ledger,path,snapshot)
    pos=ledger.get_positions('balanced')['sh600261']
    assert pos.highest_price==7. and pos.trailing_stop_price==6.
    other=run(ledger,path,snapshot,'MISSING_STRATEGY')
    assert other['status']=='SKIPPED' and other['updated_symbols']==[]


@pytest.mark.parametrize('mutation,reason',[
    (lambda s:s['positions']['sh600261'].update(total_shares=1200),'quantity_mismatch'),
    (lambda s:s['positions']['sh600261'].update(cost_price=5.4),'average_or_monitor_cost_mismatch'),
    (lambda s:s['positions']['sh600261'].update(cost_basis_cents=600000),'cost_basis_mismatch'),
    (lambda s:s['positions']['sh600261']['buy_lots'][0].update(date='2026-11-10'),'earliest_remaining_buy_date_mismatch'),
    (lambda s:s['positions']['sh600261']['buy_lots'][0].update(shares=1200),'remaining_lot_quantity_mismatch'),
    (lambda s:s['positions']['sh600261']['buy_lots'][0].update(price=4.0),'unexplained_lot_execution_cost_mismatch'),
    (lambda s:s['positions']['sh600261'].pop('buy_lots'),'missing_source_buy_lots'),
    (lambda s:s['strategy_state'][STRATEGY]['position_monitors']['sh600261'].update(cost_price=5.4),'average_or_monitor_cost_mismatch'),
    (lambda s:s['strategy_state'][STRATEGY]['position_monitors'].pop('sh600261'),'missing_snapshot_position_or_selected_monitor'),
])
def test_inconsistent_holding_identity_is_skipped_without_touching_it(tmp_path,mutation,reason):
    ledger,path,snapshot=fixture(tmp_path);old=ledger.get_positions('balanced')['sh600261']
    before=financial_state(ledger);mutation(snapshot)
    audit=run(ledger,path,snapshot)
    item=next(i for i in audit['positions'] if i['symbol']=='sh600261')
    assert item['status']=='SKIPPED' and item['reason']==reason
    pos=ledger.get_positions('balanced')['sh600261']
    assert pos.highest_price==old.highest_price and pos.trailing_stop_price==0.
    assert financial_state(ledger)==before


@pytest.mark.parametrize('field,value',[
    ('peak_price',float('nan')),('peak_price',float('inf')),('peak_price',-1),('peak_price',True),
    ('trailing_stop_line',float('nan')),('trailing_stop_line',-1),('trailing_stop_line',True),
    ('trailing_stop_line',7.),('trailing_stop_line',None),
])
def test_invalid_peak_and_stop_cannot_be_restored(tmp_path,field,value):
    ledger,path,snapshot=fixture(tmp_path)
    snapshot['strategy_state'][STRATEGY]['position_monitors']['sh600261'][field]=value
    audit=run(ledger,path,snapshot)
    item=next(i for i in audit['positions'] if i['symbol']=='sh600261')
    assert item['reason']=='invalid_or_unsupported_peak_and_stop'
    assert ledger.get_positions('balanced')['sh600261'].trailing_stop_price==0.


def test_new_holding_and_changed_lot_identity_cannot_reuse_old_monitor(tmp_path):
    ledger,path,snapshot=fixture(tmp_path)
    with ledger.connect() as conn:
        conn.execute("UPDATE position_lots SET buy_date='2026-11-16' WHERE symbol='sh600261'")
    audit=run(ledger,path,snapshot)
    assert audit['positions'][0]['reason']=='earliest_remaining_buy_date_mismatch'


def test_fee_inclusive_lot_migration_uses_verified_basis_and_does_not_guess_fees(tmp_path):
    ledger,path,snapshot=fixture(tmp_path)
    with ledger.connect() as conn:
        conn.execute("UPDATE position_lots SET cost_price=5.2139 WHERE symbol='sh600261'")
    audit=run(ledger,path,snapshot)
    assert audit['positions'][0]['status']=='RESTORED'
    assert audit['positions'][0]['lot_cost_representation']=='verified_source_average_cost'
    snapshot['positions']['sh600261'].pop('cost_basis_cents')
    audit=run(ledger,path,snapshot)
    assert audit['positions'][0]['reason']=='unexplained_lot_execution_cost_mismatch'


def test_missing_or_invalid_source_returns_audited_skip(tmp_path):
    ledger,path,snapshot=fixture(tmp_path);before=financial_state(ledger)
    assert restore_legacy_position_risk(ledger,'balanced',path,STRATEGY)['reason']=='source_unavailable_or_invalid'
    path.write_text('{invalid')
    audit=restore_legacy_position_risk(ledger,'balanced',path,STRATEGY)
    assert audit['status']=='SKIPPED' and audit['source_sha256']
    assert financial_state(ledger)==before
