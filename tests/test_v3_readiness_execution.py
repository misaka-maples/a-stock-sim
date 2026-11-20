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


def test_hard_stop_blocks_same_symbol_rebuy_and_refills_other_symbol_without_restart_duplicates(tmp_path, monkeypatch):
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
    initial_shares = {}
    for account in ['balanced', 'aggressive']:
        assert result[account]['allowed_signal_count'] == 2
        assert result[account]['executed_fills'] == 1
        positions = rt.ledger.get_positions(account)
        assert set(positions) == {'sh600001'}  # Stopped-out sh600000 stays in cooldown; sh600001 refills immediately.
        initial_shares[account] = positions['sh600001'].total_shares
    restarted = TradingRuntime(db_path=str(tmp_path/'ledger.sqlite'), mode='ACTIVE')
    res_restart = restarted.step(EXEC, quotes=quotes, market_data=data, now=NOW, signal_asof_date=SIG)
    for account in ['balanced', 'aggressive']:
        assert res_restart[account]['executed_fills'] == 0
        positions = restarted.ledger.get_positions(account)
        assert set(positions) == {'sh600001'}
        assert positions['sh600001'].total_shares == initial_shares[account]


def test_morning_entry_then_intraday_stop_refills_next_ranked_candidate(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, monkeypatch)
    # Pre-existing T+1 tradable holding sh600000 bought on 2026-11-16 (~25% of equity)
    rt.ledger.apply_fill(TradeFill(
        fill_id='seed-agg-prev', order_id='seed-ord-agg',
        client_order_id='seed-cli-agg', account_id='aggressive', symbol='sh600000',
        name='普通股票', side=OrderSide.BUY, price=10.29, shares=2400, amount=24696.0,
        commission=5.0, stamp_tax=0.0, transfer_fee=0.0, slippage=0.0, realized_pnl=0.0,
        timestamp='2026-11-16T10:00:00'), name='普通股票', sector='行业0')
    rt.ledger.roll_t1(EXEC)

    data, quotes = inputs()
    bars = copy.deepcopy(data['candidate_bars']['sh600000'])
    data['candidates'].append({
        'symbol': 'sh600002', 'name': '普通股票', 'price': 10.29, 'score': 78,
        'sector': '行业2', 'as_of': SIG, 'ret20': 10, 'ret60': 20, 'amount': 40000000,
        'ma20': 10.1, 'ma60': 9.8, 'ma120': 9.5,
    })
    data['top_sectors'].append('行业2')
    data['candidate_bars']['sh600002'] = copy.deepcopy(bars)
    quotes['sh600002'] = {
        'price': 10.29, 'current': 10.29, 'open': 10.29, 'pre_close': 10.29,
        'timestamp': NOW.isoformat(), 'fresh': True, 'name': '普通股票', 'volume_lots': 10000,
    }
    # 09:31: aggressive holds sh600000 (25%) and buys sh600001 (25%) up to 50% RISK_OFF cap
    res_open = rt.step(EXEC, quotes=quotes, market_data=data, now=NOW, signal_asof_date=SIG)
    assert res_open['aggressive']['executed_fills'] == 1
    assert set(rt.ledger.get_positions('aggressive')) == {'sh600000', 'sh600001'}

    # 10:16: sh600000 triggers -6% hard stop intraday while still rank #1; sh600002 refills immediately!
    later = NOW + dt.timedelta(minutes=45)
    for q in quotes.values():
        q['timestamp'] = later.isoformat()
    # Trigger -6% hard stop on sh600000 without triggering entry_intraday_drop (> -3% check is skipped via sell_cooldown anyway, and -2.5% from open wouldn't hit -3%)
    quotes['sh600000'].update({'price': 9.60, 'current': 9.60, 'open': 9.80, 'pre_close': 10.29})
    res_intraday = rt.step(EXEC, quotes=quotes, market_data=data, now=later, signal_asof_date=SIG)
    assert set(rt.ledger.get_positions('aggressive')) == {'sh600001', 'sh600002'}
    assert res_intraday['aggressive']['execution_cycle']['reason'] == 'explicit_refill_enabled'
    assert res_intraday['aggressive']['executed_fills'] == 1



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
