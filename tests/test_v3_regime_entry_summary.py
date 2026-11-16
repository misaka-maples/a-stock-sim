"""Regime budgets and API entry permissions agree, using temporary ledgers."""
from types import SimpleNamespace
import pytest
from src.astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from src.astock.trade.models import MarketRegime, Signal
from src.astock.trade.risk.portfolio import PortfolioRiskRule
from src.astock.trade.strategy.balanced import BalancedStrategy
from src.astock.trade.strategy.aggressive import AggressiveStrategy
from src.astock.trade.summary_builder import build_dual_accounts_summary

CAPS = [(MarketRegime.RISK_OFF,.30,.50),(MarketRegime.NEUTRAL,.45,.70),
        (MarketRegime.RISK_ON,.65,.90),(MarketRegime.STRONG_RISK_ON,.80,1.)]

def make_runtime(tmp_path, regime, passed=True):
    path=tmp_path/'ledger.sqlite'
    ledger=SQLiteAccountLedger(str(path))
    ledger.init_account('balanced','balanced',100000.)
    ledger.init_account('aggressive','aggressive',100000.)
    return ledger,SimpleNamespace(db_path=path,mode='ACTIVE',last_regime=regime,
        last_trade_date='2026-11-16',last_step_summary={'market_risk_audit':{'passed':passed}})

@pytest.mark.parametrize('regime,balanced_cap,aggressive_cap',CAPS)
def test_summary_and_planner_use_requested_caps(tmp_path,regime,balanced_cap,aggressive_cap):
    ledger,runtime=make_runtime(tmp_path,regime)
    result=build_dual_accounts_summary(runtime,backup_path=str(tmp_path/'absent.json'))
    for account,key,cap,cls in [('balanced','primary',balanced_cap,BalancedStrategy),
                              ('aggressive','secondary',aggressive_cap,AggressiveStrategy)]:
        row=result[key]
        assert row['max_regime_total_wt']==cap
        assert row['allow_new_entries'] is True
        assert row['no_entry_reason']==''
        assert row['regime_caps']['RISK_OFF']==(.30 if account=='balanced' else .50)
        assert PortfolioRiskRule(account).get_effective_regime_cap(regime)==cap
        signals=[Signal(run_id='test',signal_asof='2026-11-13',symbol=f'sh60000{i}',
            name='test',rank=i+1,score=90.-i,sector=f'sector-{i}',price=10.) for i in range(6)]
        plan=cls(account_id=account).plan_targets('2026-11-16',regime,signals,ledger.get_balance(account),{})
        assert plan.regime_cap==cap
        assert 0<plan.total_weight<=cap

@pytest.mark.parametrize('passed',[False,None])
def test_data_failure_or_missing_step_still_blocks_entries(tmp_path,passed):
    _,runtime=make_runtime(tmp_path,MarketRegime.RISK_OFF,passed)
    if passed is None:runtime.last_step_summary={}
    result=build_dual_accounts_summary(runtime,backup_path=str(tmp_path/'absent.json'))
    for key in ['primary','secondary']:
        assert result[key]['allow_new_entries'] is False
        assert '校验' in result[key]['no_entry_reason']

def test_drawdown_freeze_not_overridden_by_riskoff_budget(tmp_path):
    ledger,runtime=make_runtime(tmp_path,MarketRegime.RISK_OFF)
    with ledger.connect() as conn:conn.execute('UPDATE accounts SET peak_equity_cents=20000000')
    result=build_dual_accounts_summary(runtime,backup_path=str(tmp_path/'absent.json'))
    for key in ['primary','secondary']:
        assert result[key]['is_frozen'] is True
        assert result[key]['allow_new_entries'] is False
        assert '冻结' in result[key]['no_entry_reason']


def test_account_data_freeze_is_visible_even_when_shared_audit_passes(tmp_path):
    _,runtime=make_runtime(tmp_path,MarketRegime.RISK_OFF)
    runtime.last_step_summary['aggressive']={'entry_risk_freeze':True}
    result=build_dual_accounts_summary(runtime,backup_path=str(tmp_path/'absent.json'))
    assert result['primary']['allow_new_entries'] is True
    assert result['secondary']['allow_new_entries'] is False


def test_summary_uses_actual_runtime_drawdown_threshold(tmp_path):
    ledger,runtime=make_runtime(tmp_path,MarketRegime.RISK_OFF)
    runtime.risk_manager_agg=SimpleNamespace(portfolio_rule=PortfolioRiskRule('aggressive'))
    with ledger.connect() as conn:
        conn.execute("UPDATE accounts SET peak_equity_cents=11764705 WHERE account_id='aggressive'")
    row=build_dual_accounts_summary(runtime,backup_path=str(tmp_path/'absent.json'))['secondary']
    assert 12<row['current_drawdown_pct']<20
    assert row['allow_new_entries'] is False
    assert row['is_frozen'] is True
