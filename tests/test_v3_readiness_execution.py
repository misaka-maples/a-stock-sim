"""Next-session orders consume actual signal dates and saved account policies."""
import copy
import datetime as dt
from pathlib import Path

import pytest

from src.astock.trade.execution_policy import load_execution_policies
from src.astock.trade.models import AccountBalance, MarketRegime, Position, Signal, TradeFill, OrderSide
from src.astock.trade.runtime import TradingRuntime, _live_exit_price
from src.astock.trade.strategy.balanced import BalancedStrategy
from src.astock.trade.strategy.aggressive import AggressiveStrategy
from test_v3_symbol_data_isolation import payload, NOW, SIG, EXEC


def inputs():
    data = payload(MarketRegime.RISK_OFF)
    data['execution_policy'] = load_execution_policies(Path.cwd())
    for candidate in data['candidates']:
        candidate.update(ma20=10.1, ma60=9.8, ma120=9.5)
    quotes = {c['symbol']: {'price': c['price'], 'current': c['price'],
                           'open': c['price'], 'pre_close': c['price'],
                           'timestamp': NOW.isoformat(), 'fresh': True,
                           'name': c['name'], 'volume_lots': 10000}
              for c in data['candidates']}
    return data, quotes


def make_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTOCK_ENV', 'test')
    monkeypatch.setenv('ASTOCK_AUDIT_PATH', str(tmp_path/'audit.json'))
    return TradingRuntime(db_path=str(tmp_path/'ledger.sqlite'), mode='ACTIVE')


def test_dated_residual_suspension_quote_is_not_an_executable_price():
    assert _live_exit_price({'price':10,'open':0,'volume_lots':0,'fresh':True,
                             'timestamp':NOW.isoformat()}, EXEC, NOW) is None


def test_saved_policies_open_both_accounts_with_real_industry_and_t1(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, monkeypatch)
    data, quotes = inputs()
    result = rt.step(EXEC, quotes=quotes, market_data=data, now=NOW, signal_asof_date=SIG)
    for account, cap in [('balanced', .30), ('aggressive', .50)]:
        assert result[account]['executed_fills'] == 2
        positions = rt.ledger.get_positions(account)
        assert {p.sector for p in positions.values()} == {'行业0', '行业1'}
        assert all(p.tradable_shares == 0 for p in positions.values())
        assert sum(p.market_value for p in positions.values()) / rt.ledger.get_balance(account).total_equity <= cap
    assert rt.risk_manager_bal.position_rule.atr_multiplier == 3.5
    assert rt.risk_manager_agg.position_rule.atr_multiplier == 4.5
    assert rt.risk_manager_agg.portfolio_rule.freeze_drawdown_pct == 20


@pytest.mark.parametrize('fault', ['stale', 'missing', 'gap_down', 'gap_up', 'intraday_drop', 'st'])
def test_bad_live_quote_rejects_only_that_stock(tmp_path, monkeypatch, fault):
    rt = make_runtime(tmp_path, monkeypatch)
    data, quotes = inputs()
    q = quotes['sh600001']
    if fault == 'stale': q['fresh'] = False
    elif fault == 'missing': del quotes['sh600001']
    elif fault == 'gap_down': q['open'] *= .95
    elif fault == 'gap_up': q['open'] *= 1.07
    elif fault == 'intraday_drop': q['price'] = q['current'] = q['open'] * .96
    elif fault == 'st': q['name'] = '*ST测试'
    result = rt.step(EXEC, quotes=quotes, market_data=data, now=NOW, signal_asof_date=SIG)
    for account in ['balanced', 'aggressive']:
        assert result[account]['allowed_signal_count'] == 1
        assert set(rt.ledger.get_positions(account)) == {'sh600000'}


def test_hard_stop_cannot_rebuy_or_refill_same_day_even_after_restart(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, monkeypatch)
    for account in ['balanced', 'aggressive']:
        rt.ledger.apply_fill(TradeFill(
            fill_id='seed-'+account, order_id='seed-order-'+account,
            client_order_id='seed-client-'+account, account_id=account, symbol='sh600000',
            name='普通股票', side=OrderSide.BUY, price=12, shares=500, amount=6000,
            commission=5, stamp_tax=0, transfer_fee=.01, slippage=0, realized_pnl=0,
            timestamp='2026-11-13T10:00:00'))
    data, quotes = inputs()
    result = rt.step(EXEC, quotes=quotes, market_data=data, now=NOW, signal_asof_date=SIG)
    for account in ['balanced', 'aggressive']:
        assert result[account]['allowed_signal_count'] == 2
        assert result[account]['executed_fills'] == 0
        assert not rt.ledger.get_positions(account)
    restarted = TradingRuntime(db_path=str(tmp_path/'ledger.sqlite'), mode='ACTIVE')
    restarted.step(EXEC, quotes=quotes, market_data=data, now=NOW, signal_asof_date=SIG)
    assert all(not restarted.ledger.get_positions(a) for a in ['balanced', 'aggressive'])


@pytest.mark.parametrize('strategy, held_count, expected', [(BalancedStrategy, 2, 6), (AggressiveStrategy, 3, 5)])
def test_existing_positions_are_counted_once_and_do_not_consume_candidate_slots(strategy, held_count, expected):
    s = strategy()
    positions = {f'sh60000{i}': Position(symbol=f'sh60000{i}', name='old', shares=1000,
                                        available_shares=1000, cost_price=10, current_price=10,
                                        market_value=10000, sector=f'old{i}') for i in range(held_count)}
    signals = [Signal(run_id='t', signal_asof=SIG, symbol=f'sh60000{i}', name='new',
                      rank=i+1, score=90-i, sector=f'sector{i}', price=10) for i in range(10)]
    balance = AccountBalance(account_id=s.account_id, cash=100000-held_count*10000,
                             frozen_cash=0, stock_market_value=held_count*10000, total_equity=100000)
    plan = s.plan_targets(EXEC, MarketRegime.STRONG_RISK_ON, signals, balance, positions)
    assert len(plan.target_positions) == expected
    assert plan.total_weight <= plan.regime_cap
