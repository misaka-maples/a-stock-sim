import copy
import datetime
import json
import sqlite3

import pytest

from sim_trader import SimAccount, BEIJING
from sqlite_account import SQLiteSimAccount, JournalSequence
from sqlite_ledger import SQLiteLedger, LedgerConflict


def trade(i=1):
    return dict(time='2026-09-21 10:00:00',symbol='sh600000',name='A',side='BUY',
                price=10.0,shares=100,amount=1000.0,fees=5.01,order_id=str(i))


def test_atomic_state_journal_rollups_and_stale_writer(tmp_path):
    ledger = SQLiteLedger(tmp_path/'a.db')
    assert ledger.commit({'cash':8994.99},expected_revision=0,trades=[trade()]) == 1
    with pytest.raises(LedgerConflict):
        ledger.commit({'cash':0},expected_revision=0,trades=[trade(2)])
    assert ledger.load() == (1,{'cash':8994.99})
    assert ledger.count('trades') == 1
    assert ledger.aggregate()['fees_cents'] == 501
    def failing():
        yield trade(2)
        raise OSError('simulated interrupted import')
    with pytest.raises(OSError):
        ledger.commit({'cash':0},expected_revision=1,trades=failing())
    assert ledger.load() == (1,{'cash':8994.99})
    assert ledger.count('trades') == 1
    assert ledger.aggregate()['fees_cents'] == 501


def test_pagination_backup_and_history_copy_are_bounded(tmp_path):
    ledger = SQLiteLedger(tmp_path/'a.db')
    ledger.commit({'cash':1},expected_revision=0,trades=(trade(i) for i in range(1200)))
    first = ledger.page('trades',limit=25)
    second = ledger.page('trades',before=first[-1]['sequence'],limit=25)
    assert first[0]['sequence']==1200 and second[0]['sequence']==1175
    sequence = JournalSequence(ledger,'trades')
    sequence.append(trade(1201))
    clone = copy.deepcopy(sequence)
    assert clone.committed == 1200 and len(clone.pending)==1
    assert clone.ledger is ledger
    assert sequence[-2]['order_id']=='1199'
    ledger.backup(tmp_path/'backup.db')
    restored = SQLiteLedger(tmp_path/'backup.db')
    assert restored.load()==ledger.load()
    assert restored.count('trades')==1200
    assert restored.aggregate()==ledger.aggregate()


def test_json_migration_and_matching_accounting_equivalence(tmp_path):
    original = SimAccount(initial_cash=100000,save_path=str(tmp_path/'legacy.json'),ledger_only=True,strict_t1=False)
    original._execution_now = datetime.datetime(2026,9,21,10,tzinfo=BEIJING)
    assert original.execute_buy('sh600000','A',10,100)['success']
    original.save()
    evidence = original.save_path.read_bytes()
    account = SQLiteSimAccount(database_path=tmp_path/'account.db',legacy_json=original.save_path,
                               ledger_only=True,strict_t1=False)
    account._execution_now = original._execution_now
    assert original.save_path.read_bytes()==evidence
    assert account.cash == original.cash
    assert account.positions == original.positions
    assert list(account.trades)==original.trades
    for instance in (original,account):
        assert instance.execute_buy('sh600000','A',11,200)['success']
        assert instance.execute_sell('sh600000',12,100)['success']
        assert instance.execute_sell('sh600000',9,200)['success']
    assert account.cash == original.cash
    assert account.positions == original.positions
    assert list(account.trades)==original.trades or [t['realized_pnl'] for t in account.trades if t['side']=='SELL']==[t['realized_pnl'] for t in original.trades if t['side']=='SELL']
    assert account.get_summary()==original.get_summary()
    assert account.get_performance_metrics()==original.get_performance_metrics()
    restarted = SQLiteSimAccount(database_path=tmp_path/'account.db',ledger_only=True,strict_t1=False)
    restarted._execution_now = original._execution_now
    assert restarted.get_summary()==account.get_summary()
    assert restarted.cash==account.cash
    assert list(restarted.trades)==list(account.trades)


def test_commit_failure_rolls_back_and_stops_trading(tmp_path,monkeypatch):
    account = SQLiteSimAccount(database_path=tmp_path/'a.db',initial_cash=10000,ledger_only=True)
    original = account.cash
    def fail(*args,**kwargs):
        raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(account._ledger,'commit',fail)
    result = account.execute_buy('sh600000','A',10,100)
    assert result['status']=='ERROR'
    assert account.cash==original and len(account.trades)==0 and len(account.orders)==0
    assert account._persistence_error
    assert account.execute_buy('sh600000','A',10,100)['status']=='ERROR'
    assert SQLiteLedger(tmp_path/'a.db').count('trades')==0


def test_rejection_dedup_keeps_original_semantics(tmp_path):
    account = SQLiteSimAccount(database_path=tmp_path/'a.db',initial_cash=10000)
    now = datetime.datetime(2026,9,21,10,tzinfo=BEIJING)
    account._execution_now=now
    assert not account.execute_buy('sh600000','A',10,100)['success']
    account._execution_now=now+datetime.timedelta(seconds=3)
    assert not account.execute_buy('sh600000','A',10,100)['success']
    assert len(account.orders)==1
    assert account.orders[0]['repeat_count']==2
