# -*- coding: utf-8 -*-
"""
Unit and Integration Tests for A-Stock Quant Trading Core Architecture (V3)
Validates:
1. Strategy Engine (Pure signal and target generation, no direct ledger mutation)
2. Risk Manager (Market, Portfolio, Position, Order risk layers)
3. Order State Machine & Order Manager (Transitions, idempotency, partial fills)
4. Simulated Broker Adapter (A-share fees, slippage, T+1 enforcement)
5. Account Ledger (Double-entry, cash cents, overnight interest, persistence)
6. Autonomous Dual Accounts (Balanced & Aggressive peer independence)
"""

import pytest
import datetime
from pathlib import Path
import tempfile
import json

from astock.trade.models import (
    MarketRegime, OrderSide, OrderType, OrderStatus, RiskLayer, RiskSeverity,
    Signal, TargetPosition, TargetPortfolio, Order, TradeFill, Position, AccountBalance
)
from astock.trade.errors import (
    TradeError, RiskCheckError, OrderValidationError, OrderStateError,
    InsufficientCashError, InsufficientSharesError, DuplicateOrderError
)
from astock.trade.order import OrderStateMachine, OrderManager, VALID_TRANSITIONS
from astock.trade.broker import SimulatedBrokerAdapter
from astock.trade.ledger import AccountLedger, CashJournalEntry
from astock.trade.risk import (
    RiskManager, MarketRiskRule, PortfolioRiskRule, PositionRiskRule, OrderRiskRule,
    BALANCED_REGIME_CAPS, AGGRESSIVE_REGIME_CAPS
)
from astock.trade.strategy import BalancedStrategy, AggressiveStrategy
from astock.trade.portfolio import PortfolioManager, PortfolioRebalancer


# ==============================================================================
# 1. Order State Machine Tests
# ==============================================================================

def test_order_state_machine_valid_lifecycle():
    order = Order(
        order_id="ORD_001", client_order_id="CID_001", account_id="test",
        symbol="sh600519", side=OrderSide.BUY, shares=100, price=1500.0,
        status=OrderStatus.PROPOSED
    )
    # PROPOSED -> RISK_APPROVED
    OrderStateMachine.transition(order, OrderStatus.RISK_APPROVED)
    assert order.status == OrderStatus.RISK_APPROVED

    # RISK_APPROVED -> PENDING_SUBMIT
    OrderStateMachine.transition(order, OrderStatus.PENDING_SUBMIT)
    assert order.status == OrderStatus.PENDING_SUBMIT

    # PENDING_SUBMIT -> SUBMITTED
    OrderStateMachine.transition(order, OrderStatus.SUBMITTED)
    assert order.status == OrderStatus.SUBMITTED

    # SUBMITTED -> PARTIALLY_FILLED
    OrderStateMachine.transition(order, OrderStatus.PARTIALLY_FILLED)
    assert order.status == OrderStatus.PARTIALLY_FILLED

    # PARTIALLY_FILLED -> FILLED
    OrderStateMachine.transition(order, OrderStatus.FILLED)
    assert order.status == OrderStatus.FILLED
    assert OrderStateMachine.is_terminal(order.status)


def test_order_state_machine_invalid_transition():
    order = Order(
        order_id="ORD_002", client_order_id="CID_002", account_id="test",
        symbol="sh600519", side=OrderSide.BUY, shares=100, price=1500.0,
        status=OrderStatus.FILLED
    )
    # Terminal state FILLED cannot transition to SUBMITTED
    with pytest.raises(OrderStateError):
        OrderStateMachine.transition(order, OrderStatus.SUBMITTED)


def test_order_manager_idempotency_and_partial_fill():
    mgr = OrderManager()
    order = mgr.create_order(
        account_id="balanced", symbol="sh600036", side=OrderSide.BUY,
        shares=500, price=30.0, client_order_id="CID_IDEMP_01"
    )
    assert order.shares == 500
    assert order.remaining_shares == 500

    # Duplicate submission with same client_order_id raises DuplicateOrderError
    with pytest.raises(DuplicateOrderError):
        mgr.create_order(
            account_id="balanced", symbol="sh600036", side=OrderSide.BUY,
            shares=500, price=30.0, client_order_id="CID_IDEMP_01"
        )

    # Partial fill
    OrderStateMachine.transition(order, OrderStatus.SUBMITTED)
    mgr.record_partial_fill(order.order_id, fill_shares=200, fill_price=30.0, fee=5.0)
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_shares == 200
    assert order.remaining_shares == 300

    # Final fill
    mgr.record_partial_fill(order.order_id, fill_shares=300, fill_price=30.0, fee=5.0)
    assert order.status == OrderStatus.FILLED
    assert order.remaining_shares == 0


# ==============================================================================
# 2. Risk Manager 4-Layer Hierarchy Tests
# ==============================================================================

def test_market_risk_regime_classification():
    rule = MarketRiskRule()
    # Mock CSI 300 bars with 130 days
    bars = []
    base_px = 3500.0
    for i in range(130):
        d_str = (datetime.date(2024, 1, 1) + datetime.timedelta(days=i)).isoformat()
        bars.append({"date": d_str, "close": base_px + i * 2.0})

    # With confirmed breadth >= 60% -> STRONG_RISK_ON
    regime, breadth, audit = rule.determine_regime(bars, as_of=bars[-1]["date"], market_breadth=0.65)
    assert regime == MarketRegime.STRONG_RISK_ON
    assert audit.passed is True
    assert audit.layer == RiskLayer.MARKET

    # With moderate breadth 45% -> RISK_ON
    regime_m, breadth_m, audit_m = rule.determine_regime(bars, as_of=bars[-1]["date"], market_breadth=0.45)
    assert regime_m == MarketRegime.RISK_ON
    assert audit_m.passed is True

    # With missing breadth -> RISK_OFF (data quality error, blocks new buys)
    regime_miss, breadth_miss, audit_miss = rule.determine_regime(bars, as_of=bars[-1]["date"], market_breadth=None)
    assert regime_miss == MarketRegime.RISK_OFF
    assert audit_miss.passed is False
    assert audit_miss.action_taken == "BLOCK_NEW_BUYS"


def test_market_risk_off_entry_gate():
    rule = MarketRiskRule()
    bars = [{"date": f"2024-01-{i:02d}", "close": 20.0 + i * 0.1, "amount": 50_000_000} for i in range(1, 131)]

    # Candidate meeting all criteria
    audit = rule.check_risk_off_entry_gate(
        symbol="sh601138", price=35.0, bars=bars, sector="电子",
        top_sectors=["电子", "银行"], ret20=10.0, ret60=20.0, amount=80_000_000
    )
    assert audit.passed is True
    assert audit.severity == RiskSeverity.INFO

    # Candidate failing due to weak sector
    audit_fail_sec = rule.check_risk_off_entry_gate(
        symbol="sh601138", price=35.0, bars=bars, sector="房地产",
        top_sectors=["电子", "银行"], ret20=10.0, ret60=20.0, amount=80_000_000
    )
    assert audit_fail_sec.passed is False
    assert audit_fail_sec.severity == RiskSeverity.BLOCK


def test_portfolio_risk_drawdown_circuit_breaker():
    rule = PortfolioRiskRule(account_type="balanced", alert_drawdown_pct=8.0, freeze_drawdown_pct=12.0)
    bal = AccountBalance(
        account_id="balanced", cash=80000.0, frozen_cash=0.0, stock_market_value=0.0,
        total_equity=80000.0, peak_equity=100000.0, current_drawdown_pct=20.0
    )
    is_alert, is_freeze, audit = rule.check_drawdown_circuit_breaker(bal)
    assert is_alert is True
    assert is_freeze is True
    assert audit.severity == RiskSeverity.FREEZE
    assert audit.action_taken == "FREEZE_NEW_ENTRIES"


def test_position_risk_hard_stop_and_take_profit():
    pos_rule = PositionRiskRule(hard_stop_loss_pct=-6.0, atr_multiplier=3.0, take_profit_floor_pct=3.0)
    pos = Position(
        symbol="sz000001", shares=1000, available_shares=1000, cost_price=10.0,
        highest_price=12.0
    )

    # 1. Hard stop loss (-7% drop)
    exit_needed, reason, audit = pos_rule.check_position_exit(
        pos, current_price=9.30, atr=0.3, ma20=9.8
    )
    assert exit_needed is True
    assert "硬止损触发" in reason

    # 2. Dynamic trailing ATR take-profit (highest 12.0, ATR 0.5 -> stop at 12.0 - 1.5 = 10.5)
    exit_needed, reason, audit = pos_rule.check_position_exit(
        pos, current_price=10.40, atr=0.5, ma20=10.0
    )
    assert exit_needed is True
    assert "动态ATR止盈" in reason


def test_order_risk_lot_size_and_limits():
    order_rule = OrderRiskRule()
    order = Order(
        order_id="ORD_R01", client_order_id="CID_R01", account_id="balanced",
        symbol="sh600519", side=OrderSide.BUY, shares=150, price=1600.0
    )
    # Buy shares must be multiple of 100
    ok, audit = order_rule.validate_order(order, available_cash=500000.0)
    assert ok is False
    assert "multiple of 100" in audit.message

    # Limit-up check
    order_100 = Order(
        order_id="ORD_R02", client_order_id="CID_R02", account_id="balanced",
        symbol="sh600519", side=OrderSide.BUY, shares=100, price=1760.0
    )
    ok, audit = order_rule.validate_order(order_100, available_cash=500000.0, prev_close=1600.0)
    assert ok is False
    assert "limit-up" in audit.message


# ==============================================================================
# 3. Simulated Broker Adapter & Fee Calculations
# ==============================================================================

def test_simulated_broker_execution_and_fees():
    broker = SimulatedBrokerAdapter(initial_cash=100000.0, slippage=0.001)
    broker.init_account("test_acc", cash=100000.0)

    # BUY 1,000 shares at 10.00
    order_buy = Order(
        order_id="O_B1", client_order_id="C_B1", account_id="test_acc",
        symbol="sz000001", side=OrderSide.BUY, shares=1000, price=10.00
    )
    fill_buy = broker.submit_order(order_buy)
    assert fill_buy.status == OrderStatus.FILLED
    # Price with 0.1% slippage = 10.01
    assert fill_buy.filled_amount > 10000.0
    # T+1: available_shares = 0 immediately after buy
    assert broker.get_sellable_shares("test_acc", "sz000001") == 0

    # Next day roll
    broker.roll_trading_day("test_acc")
    assert broker.get_sellable_shares("test_acc", "sz000001") == 1000

    # SELL 1,000 shares at 11.00
    order_sell = Order(
        order_id="O_S1", client_order_id="C_S1", account_id="test_acc",
        symbol="sz000001", side=OrderSide.SELL, shares=1000, price=11.00
    )
    fill_sell = broker.submit_order(order_sell)
    assert fill_sell.status == OrderStatus.FILLED
    # Cash credited
    assert broker.get_cash("test_acc") > 100000.0
    assert broker.get_sellable_shares("test_acc", "sz000001") == 0


# ==============================================================================
# 4. Account Ledger Double-Entry & Overnight Interest
# ==============================================================================

def test_account_ledger_accounting_and_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage_file = Path(tmpdir) / "test_ledger.json"
        ledger = AccountLedger(account_id="balanced", initial_cash=100000.0, storage_path=storage_file)

        # Apply overnight interest for 10 days
        for i in range(10):
            interest = ledger.apply_overnight_interest(f"2024-01-{i+1:02d}")
            assert interest > 0.0

        assert ledger.total_interest_earned > 0.0
        assert ledger.cash > 100000.0

        # Save and reload
        ledger.save()
        assert storage_file.exists()

        loaded_ledger = AccountLedger(account_id="balanced", storage_path=storage_file)
        loaded_ledger.load()
        assert loaded_ledger.cash == ledger.cash
        assert loaded_ledger.total_interest_earned == ledger.total_interest_earned


# ==============================================================================
# 5. Dual Peer Account Autonomy
# ==============================================================================

def test_dual_peer_account_autonomy():
    broker = SimulatedBrokerAdapter()
    broker.init_account("balanced", cash=100000.0)
    broker.init_account("aggressive", cash=100000.0)

    # Buy 500 shares at 30.00 in balanced (15,000 RMB)
    o_b = Order(
        order_id="OB1", client_order_id="CB1", account_id="balanced",
        symbol="sh600036", side=OrderSide.BUY, shares=500, price=30.0
    )
    broker.submit_order(o_b)

    # Aggressive cash and positions remain untouched
    assert broker.get_cash("aggressive") == 100000.0
    assert len(broker.get_positions("aggressive")) == 0
    assert len(broker.get_positions("balanced")) == 1


# ==============================================================================
# 6. Strategy Pure Target Generation
# ==============================================================================

def test_balanced_strategy_target_generation():
    strat = BalancedStrategy(account_id="balanced")
    cand_sigs = [
        Signal(run_id="r1", signal_asof="2024-03-01", symbol="sh601138", score=90.0, sector="电子", price=20.0),
        Signal(run_id="r1", signal_asof="2024-03-01", symbol="sh601918", score=85.0, sector="煤炭", price=8.0),
        Signal(run_id="r1", signal_asof="2024-03-01", symbol="sz002423", score=80.0, sector="金融", price=10.0),
    ]
    bal = AccountBalance(account_id="balanced", cash=100000.0, frozen_cash=0.0, stock_market_value=0.0, total_equity=100000.0)
    target = strat.plan_targets(as_of="2024-03-01", regime=MarketRegime.STRONG_RISK_ON, signals=cand_sigs, balance=bal, positions={})

    assert target.regime == MarketRegime.STRONG_RISK_ON
    assert target.regime_cap == 0.80
    assert len(target.target_positions) <= 3
    for sym, tpos in target.target_positions.items():
        assert tpos.target_weight <= 0.15  # Single stock cap


# ==============================================================================
# 7. Unified Backtest Engine Integration Test
# ==============================================================================

def test_unified_backtest_engine_execution():
    from astock.trade.backtest import BacktestDataset, BacktestEngine

    cache_dir = Path("/home/openarmx/test/data/history_cache")
    dataset = BacktestDataset(cache_dir=cache_dir)
    dataset.load(start_date="2024-01-02", end_date="2024-03-29", min_bars=60)
    assert len(dataset.trading_days) > 30

    engine = BacktestEngine(dataset=dataset, initial_cash=100000.0, slippage=0.001)
    strategy = BalancedStrategy(account_id="balanced_backtest")
    res = engine.run(strategy=strategy, account_type="balanced", rebalance_interval=5)

    assert res["account_id"] == "balanced_backtest"
    assert "total_return_pct" in res
    assert "max_drawdown_pct" in res
    assert len(res["daily_nav"]) == len(dataset.trading_days)
    assert len(res["orders"]) > 0
    assert res["final_equity"] > 0
