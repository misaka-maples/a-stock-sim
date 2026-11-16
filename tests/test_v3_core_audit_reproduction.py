# tests/test_v3_core_audit_reproduction.py
# -*- coding: utf-8 -*-
"""
Failure reproduction and correctness verification test suite for:
1. Market breadth calculation & lookahead bias in determine_regime
2. Order partial fill overfilling & execution on completed orders
3. RISK_OFF entry gate default 50M turnover & empty top_sectors loophole
4. Daily audit recorder forged sync completion time & unverified task inclusion
5. Stale signal rejection & lookahead truncation in backtest
6. SQLite transaction boundary overfill check
7. Missing breadth blocking new buy allocations in TradingRuntime
"""

import os
import tempfile
import pytest
from astock.trade.runtime import TradingRuntime
from astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from astock.trade.broker.sim_broker import SimulatedBrokerAdapter
from astock.trade.order.manager import OrderManager
from astock.trade.risk.market import MarketRiskRule
from astock.trade.daily_audit_recorder import DailyAuditRecorder, is_valid_acceptance_record, validate_acceptance_record
from astock.trade.models import (
    MarketRegime, Order, OrderSide, OrderType, OrderStatus, TradeFill, Signal
)
from astock.trade.errors import OrderStateError, OrderValidationError


def make_bench_bars(as_of: str, count: int = 150, trend: str = "BULL"):
    bars = []
    base_price = 3000.0
    for i in range(count):
        day = f"2026-01-{(i%28)+1:02d}"
        if trend == "BULL":
            p = base_price + i * 10.0
        else:
            p = base_price - i * 10.0
        bars.append({"date": day, "close": p, "open": p, "high": p+5, "low": p-5, "volume": 1000000})
    bars[-1]["date"] = as_of
    return bars


def test_reproduce_problem1_breadth_20pct_must_not_be_strong_risk_on():
    """
    P0-1 Reproduction:
    When CSI300 is above MA20, MA60, MA120 (Golden Cross), but real market breadth
    is only 20% (e.g. 1 out of 5 stocks above MA120), the regime MUST NOT be STRONG_RISK_ON!
    It must be RISK_ON or lower.
    Furthermore, missing candidate breadth must NEVER default to 1.0 (100%).
    """
    rule = MarketRiskRule(breadth_min=0.50)
    as_of = "2026-11-16"
    bench_bars = make_bench_bars(as_of, count=150, trend="BULL")

    # Construct candidate bars where only 1 of 5 stocks is above MA120 (20% breadth)
    cand_bars = {}
    for i in range(5):
        sym = f"sh60000{i}"
        trend = "BULL" if i == 0 else "BEAR"
        cand_bars[sym] = make_bench_bars(as_of, count=150, trend=trend)

    # 1. Calling determine_regime with 20% breadth:
    regime, breadth, audit = rule.determine_regime(bench_bars, as_of, candidate_bars=cand_bars)
    assert breadth == 0.20, f"Expected breadth 0.20, got {breadth}"
    assert regime != MarketRegime.STRONG_RISK_ON, (
        f"CRITICAL BUG: Real breadth is 20% (< 50%), but regime was declared {regime.value}!"
    )
    assert regime == MarketRegime.RISK_ON

    # 2. When candidate_bars is None or empty:
    # Must NOT default breadth to 1.0 (100%) and must NOT declare STRONG_RISK_ON!
    regime_none, breadth_none, audit_none = rule.determine_regime(bench_bars, as_of, candidate_bars=None)
    assert breadth_none is None, f"Expected breadth is None when missing, got {breadth_none}"
    assert regime_none != MarketRegime.STRONG_RISK_ON, (
        f"CRITICAL BUG: Declared STRONG_RISK_ON when market breadth data was completely missing!"
    )


def test_lookahead_bias_truncation_in_determine_regime():
    """
    P0-1 Lookahead Protection:
    If bench_bars contains future bars (e.g. 2026-11-20), calling determine_regime
    for as_of = 2026-11-16 must STRICTLY ignore bars after 2026-11-16.
    """
    rule = MarketRiskRule(breadth_min=0.50)
    as_of = "2026-11-16"
    bars_past = make_bench_bars(as_of, count=130, trend="BULL")
    # Append future bars with extreme crash
    future_bars = list(bars_past)
    for i in range(1, 10):
        future_bars.append({
            "date": f"2026-11-{16+i:02d}",
            "close": 1000.0, # Huge future crash
            "open": 1000.0, "high": 1005.0, "low": 995.0, "volume": 1000000
        })

    regime, breadth, audit = rule.determine_regime(future_bars, as_of=as_of)
    # The close price recorded in audit MUST be the close on as_of, NOT the future crash!
    assert audit.details["close"] == bars_past[-1]["close"], (
        f"Lookahead bias detected! Used future close {audit.details['close']} instead of as_of close {bars_past[-1]['close']}"
    )


def test_reproduce_problem2_partial_fill_overfill_and_repeat_execution():
    """
    P0-2 Reproduction:
    Order for 100 shares.
    1st partial fill: 60 shares -> remaining 40 shares.
    2nd partial fill request: 60 shares -> must be capped at 40 shares or rejected.
    Total filled shares across all fills MUST NEVER exceed 100 shares.
    FILLED order must NOT execute again.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "overfill_test.sqlite")
        ledger = SQLiteAccountLedger(db_path)
        account_id = "balanced"
        ledger.init_account(account_id, "balanced", 100000.0)

        broker = SimulatedBrokerAdapter(ledger=ledger, slippage=0.0)
        om = OrderManager()

        order = om.create_order(
            account_id=account_id,
            symbol="sh600000",
            side=OrderSide.BUY,
            shares=100,
            price=10.0,
            client_order_id="C_OVERFILL_01"
        )
        om.submit_order(order)
        ledger.save_order(order)

        # 1st execution: 60% partial fill
        fill1 = broker.execute_order(order, execution_price=10.0, partial_ratio=0.6)
        assert fill1.shares == 60
        assert order.filled_shares == 60
        assert order.remaining_shares == 40
        assert order.status == OrderStatus.PARTIALLY_FILLED

        # 2nd execution: requesting 60% partial fill again.
        # It must NOT fill 60 shares (which would make total 120 > 100).
        # It must cap at remaining 40 shares.
        fill2 = broker.execute_order(order, execution_price=10.0, partial_ratio=0.6)
        assert fill2.shares == 40, f"Expected capped at 40 shares, got {fill2.shares}"
        assert order.filled_shares == 100, f"Expected 100 filled shares, got {order.filled_shares}"

        # Order should now be FILLED
        assert order.status == OrderStatus.FILLED
        assert order.remaining_shares == 0

        # 3rd execution on FILLED order must be rejected and must NOT generate another fill!
        with pytest.raises((OrderStateError, Exception)):
            broker.execute_order(order, execution_price=10.0, partial_ratio=1.0)


def test_sqlite_transaction_rejects_overfilling_fill_id():
    """
    P0-2 SQLite Transaction Boundary:
    Different fill_ids for the same order attempting to exceed total order shares
    must be rejected inside the SQLite transaction with OrderValidationError,
    and the transaction must roll back cleanly.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "tx_overfill.sqlite")
        ledger = SQLiteAccountLedger(db_path)
        account_id = "balanced"
        ledger.init_account(account_id, "balanced", 100000.0)

        order = Order(
            order_id="ORD_TX_01",
            client_order_id="C_TX_01",
            account_id=account_id,
            symbol="sh600000",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            price=10.0,
            shares=100,
            status=OrderStatus.SUBMITTED
        )
        ledger.save_order(order)

        # Fill 1: 70 shares
        f1 = TradeFill(
            fill_id="FILL_TX_01",
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            account_id=account_id,
            symbol="sh600000",
            name="浦发银行",
            side=OrderSide.BUY,
            price=10.0,
            shares=70,
            amount=700.0,
            commission=5.0,
            stamp_tax=0.0,
            transfer_fee=0.01,
            slippage=0.0,
            realized_pnl=0.0,
            timestamp="2026-11-16T10:00:00"
        )
        ok1 = ledger.apply_fill(f1)
        assert ok1 is True

        # Fill 2: Attempting 50 shares (70 + 50 = 120 > 100). Must raise OrderValidationError!
        f2 = TradeFill(
            fill_id="FILL_TX_02",
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            account_id=account_id,
            symbol="sh600000",
            name="浦发银行",
            side=OrderSide.BUY,
            price=10.0,
            shares=50,
            amount=500.0,
            commission=5.0,
            stamp_tax=0.0,
            transfer_fee=0.01,
            slippage=0.0,
            realized_pnl=0.0,
            timestamp="2026-11-16T10:01:00"
        )
        with pytest.raises(OrderValidationError):
            ledger.apply_fill(f2)

        # Verify database state has strictly 70 shares, NOT 120
        trades = ledger.get_trades(account_id)
        assert len(trades) == 1
        pos = ledger.get_positions(account_id)
        assert pos["sh600000"].shares == 70


def test_reproduce_problem3_risk_off_entry_gate_strictness():
    """
    P1-3 Reproduction:
    1. Turnover amount must not default to 50M. If amount is missing or 0, must reject.
    2. Missing top_sectors (empty list) must NOT automatically pass sector entry.
    3. Stale signals (signal_asof != as_of) must be rejected.
    """
    rule = MarketRiskRule()
    as_of = "2026-11-16"
    bars = make_bench_bars(as_of, count=150, trend="BULL")

    # 1. Missing turnover amount (amount=0) must be rejected
    res_zero_amt = rule.check_risk_off_entry_gate(
        symbol="sh600000",
        price=15.0,
        bars=bars,
        sector="银行",
        top_sectors=["银行"],
        ret20=5.0,
        ret60=10.0,
        amount=0.0  # Missing / 0 turnover
    )
    assert res_zero_amt.passed is False, "CRITICAL BUG: Passed RISK_OFF entry gate with 0 turnover amount!"

    # 2. Empty top_sectors list must NOT pass
    res_empty_top = rule.check_risk_off_entry_gate(
        symbol="sh600000",
        price=15.0,
        bars=bars,
        sector="银行",
        top_sectors=[],  # Empty / missing top sectors
        ret20=5.0,
        ret60=10.0,
        amount=50_000_000.0
    )
    assert res_empty_top.passed is False, "CRITICAL BUG: Passed RISK_OFF entry gate when top_sectors was empty!"

    # 3. Stale signal (signal_asof != current_asof) must be rejected
    res_stale = rule.check_risk_off_entry_gate(
        symbol="sh600000",
        price=15.0,
        bars=bars,
        sector="银行",
        top_sectors=["银行"],
        ret20=5.0,
        ret60=10.0,
        amount=50_000_000.0,
        signal_asof="2026-11-15",
        current_asof="2026-11-16"
    )
    assert res_stale.passed is False
    assert "does not match" in res_stale.message


def test_missing_essential_market_data_blocks_new_buys_in_runtime():
    """
    P0-1 & P1-3: When essential market data (benchmark bars < 120) is missing in TradingRuntime,
    the runtime must freeze new buy allocations and record a risk audit event.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "missing_bench.sqlite")
        runtime = TradingRuntime(db_path=db_path, mode="ACTIVE", initial_capital=100000.0)

        as_of = "2026-11-16"
        quotes = {
            "sh600000": {"price": 10.0, "amount": 50_000_000.0, "name": "浦发银行", "sector": "银行"}
        }
        # market_data with insufficient benchmark bars (<120)
        market_data = {
            "csi300_bars": make_bench_bars(as_of, count=10, trend="BULL"),
            "candidates": [{"symbol": "sh600000", "score": 8.0, "price": 10.0, "amount": 50_000_000.0, "sector": "银行", "ret20": 5.0, "ret60": 10.0}],
            "top_sectors": ["银行"]
        }

        res = runtime.step(as_of=as_of, quotes=quotes, market_data=market_data)
        assert res["regime"] == "RISK_OFF"
        assert res["balanced"]["executed_orders"] == 0, (
            "New buys were executed despite missing essential market benchmark data!"
        )


def test_reproduce_problem4_daily_audit_must_require_verified_data_sync():
    """
    P1-4 Reproduction:
    Daily audit recorder must NOT forge sync time with current time when no verified sync exists.
    Unverified sync completion must fail daily acceptance validation.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "audit_repro.json")
        recorder = DailyAuditRecorder(log_path=log_path)

        # Record with missing/unverified market sync completion
        unverified_rec = {
            "trade_date": "2026-11-16",
            "market_sync_completed_at": "UNVERIFIED", # Unverified!
            "market_regime": "RISK_ON",
            "balanced_account": {"equity": 100000.0, "cash": 80000.0},
            "aggressive_account": {"equity": 100000.0, "cash": 60000.0},
            "sqlite_reconciliation": "MATCH (Discrepancy: 0.00)",
            "error_summary": "None"
        }

        valid, reasons = validate_acceptance_record(unverified_rec)
        assert valid is False
        assert any("sync" in r.lower() for r in reasons)
        assert is_valid_acceptance_record(unverified_rec) is False

        # Record with verified market sync completion
        verified_rec = dict(unverified_rec)
        verified_rec["market_sync_completed_at"] = "16:35:10"
        valid_v, reasons_v = validate_acceptance_record(verified_rec)
        assert valid_v is True
        assert len(reasons_v) == 0
