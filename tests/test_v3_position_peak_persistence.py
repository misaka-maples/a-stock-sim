"""Observed peaks and ratcheting protection lines survive SQLite restarts."""
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields

import pytest

import src.astock.trade.ledger.sqlite_ledger as ledger_module
from src.astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from src.astock.trade.models import Position, OrderSide, TradeFill
from src.astock.trade.risk.position import PositionRiskRule


def fill(ledger, tag, *, price=10., shares=100, day='2026-11-13', side=OrderSide.BUY):
    trade=TradeFill(fill_id='fill-'+tag,order_id='order-'+tag,client_order_id='client-'+tag,
        account_id='balanced',symbol='sh600000',name='浦发银行',side=side,price=price,
        shares=shares,amount=price*shares,commission=5.,stamp_tax=0.,transfer_fee=.01,
        slippage=0.,realized_pnl=0.,timestamp=day+'T10:00:00')
    assert ledger.apply_fill(trade,sector='银行') is True
    return trade


def ledger(tmp_path):
    db=tmp_path/'ledger.sqlite'
    obj=SQLiteAccountLedger(str(db));obj.init_account('balanced',initial_cash=100000.)
    return obj,db


def core_state(db):
    out={}
    with sqlite3.connect(db) as conn:
        for table in ['accounts','positions','position_lots','orders','fills','cash_journal']:
            columns=[r[1] for r in conn.execute('PRAGMA table_info('+table+')')]
            columns=[c for c in columns if c not in ['highest_price','trailing_stop_price']]
            out[table]=conn.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY rowid').fetchall()
    return out


def test_legacy_migration_preserves_financial_state_and_does_not_invent_old_peak(tmp_path):
    obj,db=ledger(tmp_path);fill(obj,'seed')
    obj.update_market_prices({'sh600000':{'current':11.,'fresh':True}})
    with obj.connect() as conn:
        # Reproduce the deployed old V3 positions schema, without protection state.
        conn.execute('ALTER TABLE positions DROP COLUMN highest_price')
        conn.execute('ALTER TABLE positions DROP COLUMN trailing_stop_price')
    before=core_state(db)
    restored=SQLiteAccountLedger(str(db));pos=restored.get_positions('balanced')['sh600000']
    assert core_state(db)==before
    assert pos.highest_price==11.  # Known current mark, not an unobserved historical high.
    assert pos.trailing_stop_price==0.
    assert pos.entry_date=='2026-11-13'
    SQLiteAccountLedger(str(db))
    assert core_state(db)==before
    with restored.connect() as conn:assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_observed_peak_and_protection_line_survive_restart_and_trigger_atr(tmp_path):
    obj,db=ledger(tmp_path);fill(obj,'seed');obj.roll_t1('2026-11-16')
    obj.update_market_prices({'sh600000':{'current':15.,'fresh':True,'time':'20261116140000'}})
    assert obj.raise_trailing_stop('balanced','sh600000',14.3)==14.3
    obj.update_market_prices({'sh600000':{'current':14.,'fresh':True,'time':'20261116140100'}})
    restored=SQLiteAccountLedger(str(db));pos=restored.get_positions('balanced')['sh600000']
    assert pos.highest_price==15.
    assert pos.current_price==14.
    assert pos.trailing_stop_price==14.3
    assert pos.entry_date=='2026-11-13'
    assert pos.available_shares==100
    should_exit,reason,_=PositionRiskRule().check_position_exit(pos,14.,atr=.2,ma20=0.)
    assert should_exit is True and 'ATR' in reason


@pytest.mark.parametrize('quote',[{'current':1000.,'fresh':False},
    {'current':1000.,'fresh':True,'is_stale':True},
    {'current':1000.,'fresh':True,'is_suspended':True}, {'current':1000.},
    {'current':float('nan'),'fresh':True}, {'current':float('inf'),'fresh':True},
    {'current':True,'fresh':True}, {'current':-1,'fresh':True}])
def test_unverified_or_invalid_quote_cannot_raise_holding_peak(tmp_path,quote):
    obj,_=ledger(tmp_path);fill(obj,'seed')
    obj.update_market_prices({'sh600000':quote})
    assert obj.get_positions('balanced')['sh600000'].highest_price==10.


def test_new_buy_and_partial_sell_never_lower_existing_peak_or_stop(tmp_path):
    obj,db=ledger(tmp_path);fill(obj,'first',price=10.)
    obj.update_market_prices({'sh600000':{'current':15.,'fresh':True}})
    obj.raise_trailing_stop('balanced','sh600000',13.9)
    fill(obj,'second',price=12.,day='2026-11-16')
    pos=obj.get_positions('balanced')['sh600000']
    assert pos.shares==200 and pos.highest_price==15. and pos.trailing_stop_price==13.9
    assert pos.entry_date=='2026-11-13'
    assert pos.cost_price==pytest.approx((1005.01+1205.01)/200,abs=.0005)
    obj.roll_t1('2026-11-17')
    fill(obj,'partial',price=14.,shares=100,day='2026-11-17',side=OrderSide.SELL)
    pos=SQLiteAccountLedger(str(db)).get_positions('balanced')['sh600000']
    assert pos.shares==100 and pos.highest_price==15. and pos.trailing_stop_price==13.9
    assert pos.entry_date=='2026-11-16'  # Earliest lot still held, not a fully sold historical lot.
    fill(obj,'last',price=14.,shares=100,day='2026-11-17',side=OrderSide.SELL)
    assert obj.get_positions('balanced')=={}
    assert obj.raise_trailing_stop('balanced','sh600000',20.)==0.
    fill(obj,'fresh-position',price=9.,day='2026-11-18')
    pos=obj.get_positions('balanced')['sh600000']
    assert pos.highest_price==9. and pos.trailing_stop_price==0.
    assert pos.entry_date=='2026-11-18'


@pytest.mark.parametrize('bad',[0,-1,float('nan'),float('inf'),None,True,'bad'])
def test_invalid_trailing_line_never_changes_valid_line(tmp_path,bad):
    obj,_=ledger(tmp_path);fill(obj,'seed')
    obj.raise_trailing_stop('balanced','sh600000',12.)
    obj.raise_trailing_stop('balanced','sh600000',bad)
    assert obj.get_positions('balanced')['sh600000'].trailing_stop_price==12.


def test_trailing_stop_can_only_increase_even_with_parallel_writers(tmp_path):
    obj,db=ledger(tmp_path);fill(obj,'seed')
    def update(value):
        return SQLiteAccountLedger(str(db)).raise_trailing_stop('balanced','sh600000',value)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(update,[13.,12.,16.,14.,15.,11.]))
    assert SQLiteAccountLedger(str(db)).get_positions('balanced')['sh600000'].trailing_stop_price==16.
    assert [f.name for f in fields(Position)][-1]=='trailing_stop_price'


@pytest.mark.parametrize('failures', [1, 6])
def test_wal_initialization_retries_only_locks_with_a_bounded_budget(tmp_path, monkeypatch, failures):
    real_connect=sqlite3.connect
    raw=real_connect(tmp_path/'wal.sqlite')
    attempts=[];delays=[]
    class BusyConnection:
        row_factory=None
        closed=False
        def execute(self,sql):
            if sql=='PRAGMA journal_mode=WAL':
                attempts.append(sql)
                if len(attempts)<=failures:
                    raise sqlite3.OperationalError('database is locked')
            return raw.execute(sql)
        def close(self):
            self.closed=True;raw.close()
    fake=BusyConnection()
    monkeypatch.setattr(ledger_module.sqlite3,'connect',lambda *a,**k:fake)
    monkeypatch.setattr(ledger_module.time,'sleep',delays.append)
    obj=object.__new__(SQLiteAccountLedger);obj.db_path=tmp_path/'wal.sqlite'
    if failures==1:
        assert obj.connect() is fake
        assert len(attempts)==2 and delays==[.01]
        assert raw.execute('PRAGMA busy_timeout').fetchone()[0]==30000
        fake.close()
    else:
        with pytest.raises(sqlite3.OperationalError,match='database is locked'):obj.connect()
        assert len(attempts)==6 and sum(delays)<1 and fake.closed


def test_wal_initialization_does_not_retry_or_hide_other_errors(tmp_path, monkeypatch):
    delays=[]
    class BadConnection:
        row_factory=None
        closed=False
        def execute(self,sql):
            if sql=='PRAGMA journal_mode':raise sqlite3.OperationalError('disk I/O error')
        def close(self):self.closed=True
    fake=BadConnection()
    monkeypatch.setattr(ledger_module.sqlite3,'connect',lambda *a,**k:fake)
    monkeypatch.setattr(ledger_module.time,'sleep',delays.append)
    obj=object.__new__(SQLiteAccountLedger);obj.db_path=tmp_path/'bad.sqlite'
    with pytest.raises(sqlite3.OperationalError,match='disk I/O error'):obj.connect()
    assert not delays and fake.closed


def test_repeated_concurrent_initializers_keep_single_migrations(tmp_path):
    # Each round starts from DELETE mode, exercising real first-open WAL races.
    for batch in range(12):
        path=tmp_path/f'concurrent-{batch}.sqlite'
        with sqlite3.connect(path) as conn:conn.execute('CREATE TABLE legacy_marker (id INTEGER)')
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _:SQLiteAccountLedger(str(path)),range(12)))
        with sqlite3.connect(path) as conn:
            columns=[r[1] for r in conn.execute('PRAGMA table_info(positions)')]
            assert columns.count('highest_price')==columns.count('trailing_stop_price')==1
            assert conn.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
            assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
