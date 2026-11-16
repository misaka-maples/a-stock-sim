"""API risk displays must reflect ledger observations, never a fabricated peak/stop."""
import copy
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from src.astock.trade.models import MarketRegime,OrderSide,TradeFill
from src.astock.trade.risk.portfolio import PortfolioRiskRule
from src.astock.trade.execution_policy import load_execution_policies
import src.astock.trade.summary_builder as summary


def summary_fixture(tmp_path,monkeypatch,indicators=True):
    db=tmp_path/'risk.sqlite';ledger=SQLiteAccountLedger(str(db))
    for account,symbol in [('balanced','sh600000'),('aggressive','sh600001')]:
        ledger.init_account(account,account,100000)
        ledger.apply_fill(TradeFill(fill_id=account,order_id=account,client_order_id=account,
            account_id=account,symbol=symbol,name='普通股票',side=OrderSide.BUY,price=10,shares=100,
            amount=1000,commission=0,stamp_tax=0,transfer_fee=0,slippage=0,realized_pnl=0,timestamp='2026-11-13T10:00:00'))
    policies=load_execution_policies(Path('.'))
    end=dt.date(2026,11,16)
    bars=[{'date':(end-dt.timedelta(days=29-i)).isoformat(),'close':10,'raw_close':10,
           'high':100 if i<10 else 10.2,'low':9.8,'adjustment_factor':1,'is_final':True}
          for i in range(30)]
    service=SimpleNamespace(get_bars=Mock(return_value=bars if indicators else []))
    snapshot=SimpleNamespace(market_regime=MarketRegime.RISK_OFF,signal_asof_date='2026-11-16')
    scheduler=SimpleNamespace(service=service,get_latest_snapshot=lambda:snapshot,
        get_status=lambda:{'last_sync_result':{'trading_ready':True,'trade_date':'2026-11-16'}})
    runtime=SimpleNamespace(db_path=db,mode='ACTIVE',last_regime=MarketRegime.RISK_OFF,
        last_trade_date='2026-11-16',last_step_summary={'market_risk_audit':{'passed':True}},
        execution_policies=policies,data_scheduler=scheduler)
    monkeypatch.setattr(summary,'_current_completed_signal_day',lambda:'2026-11-16')
    with ledger.connect() as conn:
        conn.execute("UPDATE positions SET highest_price=10.8,trailing_stop_price=0")
    return ledger,runtime,service


def read_all(ledger):
    with ledger.connect() as conn:
        return {table:[tuple(r) for r in conn.execute('SELECT * FROM '+table)]
                for table in ['accounts','positions','position_lots','orders','fills','cash_journal']}


def build(runtime,tmp_path):
    return summary.build_dual_accounts_summary(runtime,backup_path=str(tmp_path/'absent.json'))


def test_14_day_high_never_invents_holding_peak_or_plus_three_percent_lock(tmp_path,monkeypatch):
    ledger,runtime,service=summary_fixture(tmp_path,monkeypatch)
    before=read_all(ledger);result=build(runtime,tmp_path)
    for key in ['primary','secondary']:
        p=result[key]['positions_list'][0]
        assert p['peak_price']==10.8  # Earlier pre-holding history contains a high of 100.
        assert p['trailing_stop_line'] is None
        assert p['trailing_stop_active'] is False
        assert p['profit_floor_locked'] is False
        assert p['trailing_peak_eligible'] is False
        assert p['atr14']==pytest.approx(.4)
        assert p['indicator_status']=='verified_completed_session'
    for call in service.get_bars.call_args_list:
        assert call.kwargs=={'end_date':'2026-11-16','auto_fetch':False}
    assert read_all(ledger)==before


def test_stop_and_floor_display_uses_persisted_line_and_account_policy(tmp_path,monkeypatch):
    ledger,runtime,_=summary_fixture(tmp_path,monkeypatch)
    with ledger.connect() as conn:
        conn.execute("UPDATE positions SET highest_price=12.5,trailing_stop_price=10.8")
    result=build(runtime,tmp_path)
    bal=result['primary']['positions_list'][0];agg=result['secondary']['positions_list'][0]
    assert bal['trailing_stop_line']==agg['trailing_stop_line']==10.8
    assert bal['profit_floor_locked'] is True
    assert agg['profit_floor_locked'] is False  # Aggressive has no fixed profit floor.
    assert bal['atr_trailing_multiplier']==3.5
    assert agg['atr_trailing_multiplier']==4.5
    assert bal['trailing_stop_activation_pct']==agg['trailing_stop_activation_pct']==20
    assert bal['hard_stop_line']==agg['hard_stop_line']==9.4


def test_unknown_history_or_peak_has_no_fake_atr_point_one_or_peak(tmp_path,monkeypatch):
    ledger,runtime,_=summary_fixture(tmp_path,monkeypatch,indicators=False)
    with ledger.connect() as conn:conn.execute('UPDATE positions SET highest_price=0,trailing_stop_price=0')
    p=build(runtime,tmp_path)['primary']['positions_list'][0]
    assert p['atr14'] is None and p['completed_ma20'] is None
    assert p['peak_price'] is None and p['peak_price_source']=='unverified'
    assert p['indicator_status']=='unverified'
    assert p['trailing_stop_line'] is None


def test_ready_data_without_a_trading_step_is_pending_not_a_fake_ban(tmp_path,monkeypatch):
    _,runtime,_=summary_fixture(tmp_path,monkeypatch)
    runtime.last_step_summary={}
    for key in ['primary','secondary']:
        account=build(runtime,tmp_path)[key]
        assert account['allow_new_entries'] is None
        assert account['entry_validation_pending'] is True
        assert account['entry_permission_state']=='awaiting_execution_validation'
        assert '等待下一交易时段' in account['no_entry_reason']
        assert account['is_frozen'] is False


def test_freeze_status_reads_configured_runtime_threshold(tmp_path,monkeypatch):
    ledger,runtime,_=summary_fixture(tmp_path,monkeypatch)
    runtime.risk_manager_agg=SimpleNamespace(portfolio_rule=PortfolioRiskRule('aggressive',15,20))
    with ledger.connect() as conn:
        conn.execute("UPDATE accounts SET peak_equity_cents=12000000 WHERE account_id='aggressive'")
    account=build(runtime,tmp_path)['secondary']
    assert 12<account['current_drawdown_pct']<20
    assert account['is_frozen'] is False
    assert account['is_alert'] is True
