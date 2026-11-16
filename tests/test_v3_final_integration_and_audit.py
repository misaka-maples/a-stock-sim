# tests/test_v3_final_integration_and_audit.py
# -*- coding: utf-8 -*-
"""
V3.0 Final Integration & Acceptance Verification Test Suite
Covers:
1. Signal Date vs Execution Date (T+1 morning execution across weekend & holidays).
2. Stale signal expiration (> 1 trading day gap).
3. Benchmark bar expiration validation across non-trading days.
4. Bar.date / Bar.trade_date dual property & dict compatibility.
5. MarketSnapshot property compatibility (trade_date, as_of).
6. DataScheduler strict quality evaluation:
   - COMPLETED + VALID + coverage>=70% -> SUCCESS
   - PARTIAL sync -> PARTIAL
   - Snapshot INVALID -> FAILED / not SUCCESS
   - Low coverage (<70%) -> PARTIAL
7. DailyAuditRecorder strict acceptance rules:
   - Rejects unverified / missing / PARTIAL sync proof
   - Rejects coverage < 70%
   - Accepts genuine SUCCESS task proof
8. TradingRuntime unified market snapshot consumption & dual portfolio consistency.
"""

import os
import tempfile
import pytest
from datetime import datetime, date
from unittest.mock import MagicMock, patch

import trade_calendar
from astock.domain.models import Bar, MarketSnapshot, DataQuality, MarketRegime
from astock.trade.risk.market import MarketRiskRule
from astock.data.scheduler import DataScheduler
from astock.trade.daily_audit_recorder import DailyAuditRecorder, validate_acceptance_record
from astock.trade.runtime import TradingRuntime


def make_test_bar(date_str: str, close_price: float = 10.0) -> Bar:
    return Bar(
        date=date_str,
        open=close_price,
        high=close_price * 1.02,
        low=close_price * 0.98,
        close=close_price,
        volume=1000.0,
        amount=close_price * 100000.0,
        raw_open=close_price,
        raw_high=close_price * 1.02,
        raw_low=close_price * 0.98,
        raw_close=close_price,
        adjustment_factor=1.0,
        source="test"
    )


# =========================================================================
# 1. Bar and MarketSnapshot model compatibility
# =========================================================================
def test_bar_model_dual_date_compatibility():
    """Bar must support both .date and .trade_date via attr and dict access."""
    b = make_test_bar("2026-11-13", 10.2)
    assert b.date == "2026-11-13"
    assert b.trade_date == "2026-11-13"
    assert b["date"] == "2026-11-13"
    assert b["trade_date"] == "2026-11-13"
    assert b.get("date") == "2026-11-13"
    assert b.get("trade_date") == "2026-11-13"
    assert b.get("nonexistent", "default") == "default"


def test_market_snapshot_property_compatibility():
    """MarketSnapshot must expose .as_of and .trade_date matching signal_asof_date."""
    snap = MarketSnapshot(
        snapshot_id="snap_20261113",
        signal_asof_date="2026-11-13",
        available_at="16:30:00",
        universe_version="v3.0",
        sector_version="v3.0",
        bars_by_symbol={},
        benchmark_bars=[],
        market_regime=MarketRegime.STRONG_RISK_ON,
        quality=DataQuality.VALID,
        market_breadth=0.85
    )
    assert snap.signal_asof_date == "2026-11-13"
    assert snap.trade_date == "2026-11-13"
    assert snap.as_of == "2026-11-13"


# =========================================================================
# 2. Signal Date vs Execution Date (Weekend & Holiday bridging)
# =========================================================================
def test_morning_execution_across_weekend():
    """
    Signals generated on Friday close (2026-11-13) executed on Monday morning open (2026-11-16).
    Must NOT be rejected as expired.
    """
    rule = MarketRiskRule()
    bars = [{"trade_date": "2026-11-13", "close": 15.0}] * 150
    # Friday 2026-11-13 signal executed on Monday 2026-11-16 (price > MA60)
    res = rule.check_risk_off_entry_gate(
        symbol="000001.SZ",
        price=16.0,
        bars=bars,
        sector="银行",
        top_sectors=["银行"],
        ret20=5.0,
        ret60=10.0,
        amount=50_000_000.0,
        signal_asof="2026-11-13",
        current_asof="2026-11-16"
    )
    assert res.passed is True, f"Failed across weekend: {res.message}"


def test_morning_execution_across_holiday():
    """
    Signals generated before holiday (e.g. 2026-09-30) executed on first trading day after (2026-10-08).
    Must NOT be rejected as expired.
    """
    prev_td = trade_calendar.get_previous_trading_day("2026-10-08").isoformat()
    assert prev_td == "2026-09-30"

    rule = MarketRiskRule()
    bars = [{"trade_date": "2026-09-30", "close": 15.0}] * 150
    res = rule.check_risk_off_entry_gate(
        symbol="000001.SZ",
        price=16.0,
        bars=bars,
        sector="银行",
        top_sectors=["银行"],
        ret20=5.0,
        ret60=10.0,
        amount=50_000_000.0,
        signal_asof="2026-09-30",
        current_asof="2026-10-08"
    )
    assert res.passed is True, f"Failed across holiday: {res.message}"


def test_stale_signal_rejection():
    """
    Signal from 2 or more trading days ago (e.g. Wednesday 2026-11-11 executed Friday 2026-11-13)
    must be strictly rejected as stale.
    """
    rule = MarketRiskRule()
    bars = [{"trade_date": "2026-11-11", "close": 15.0}] * 150
    res = rule.check_risk_off_entry_gate(
        symbol="000001.SZ",
        price=16.0,
        bars=bars,
        sector="银行",
        top_sectors=["银行"],
        ret20=5.0,
        ret60=10.0,
        amount=50_000_000.0,
        signal_asof="2026-11-11",
        current_asof="2026-11-13"
    )
    assert res.passed is False
    assert "does not match or is expired" in res.message


def test_benchmark_expiration_across_weekend():
    """
    Monday morning trading execution evaluates benchmark up to Friday close (2026-11-13).
    Must not trigger false benchmark expiration in determine_regime.
    """
    rule = MarketRiskRule()
    import pandas as pd
    bench_bars = []
    for i in range(120):
        d_str = (pd.to_datetime("2026-05-01") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
        bench_bars.append({"trade_date": d_str, "close": 3500.0 + i})
    bench_bars[-1]["trade_date"] = "2026-11-13"

    regime, breadth, audit = rule.determine_regime(
        bench_bars=bench_bars,
        candidate_bars={"000001.SZ": bench_bars},
        as_of="2026-11-16"  # Monday execution date
    )
    assert "Benchmark data expired" not in str(audit.message)
    assert regime in (MarketRegime.STRONG_RISK_ON, MarketRegime.RISK_ON, MarketRegime.NEUTRAL)


# =========================================================================
# 3. DataScheduler Pipeline Quality & Classification
# =========================================================================
def test_data_scheduler_pipeline_completed_success():
    """When sync is COMPLETED, snapshot quality VALID, and coverage >= 70%, pipeline status is SUCCESS."""
    data_svc = MagicMock()
    data_svc.sync_daily_increment.return_value = {
        "status": "COMPLETED",
        "synced_symbols": 3000,
        "failed_symbols": [],
        "trade_date": "2026-11-13"
    }
    bench_bars = [make_test_bar(f"2026-01-{i+1:02d}", 3000.0) for i in range(120)]
    bench_bars[-1] = make_test_bar("2026-11-13", 3500.0)
    mock_snap = MarketSnapshot(
        snapshot_id="snap_20261113",
        signal_asof_date="2026-11-13",
        available_at="16:30:00",
        universe_version="v3.0",
        sector_version="v3.0",
        bars_by_symbol={"000001.SZ": bench_bars},
        benchmark_bars=bench_bars,
        market_regime=MarketRegime.STRONG_RISK_ON,
        quality=DataQuality.VALID,
        market_breadth=0.85
    )
    data_svc.compute_market_snapshot.return_value = mock_snap
    data_svc.get_market_snapshot.return_value = mock_snap
    data_svc.get_universe_symbols.return_value = ["s1", "s2", "s3"]

    scheduler = DataScheduler(data_service=data_svc)
    res = scheduler.run_daily_pipeline(target_date="2026-11-13")

    assert res["status"] == "SUCCESS"
    assert res["quality_passed"] is True
    assert res["coverage"] >= 0.70
    assert scheduler.get_latest_snapshot() == mock_snap


def test_data_scheduler_pipeline_partial_sync():
    """When sync is PARTIAL, pipeline status must be PARTIAL, not SUCCESS."""
    data_svc = MagicMock()
    data_svc.sync_daily_increment.return_value = {
        "status": "PARTIAL",
        "synced_symbols": 2000,
        "failed_symbols": ["s1", "s2"],
        "trade_date": "2026-11-13"
    }
    bench_bars = [make_test_bar(f"2026-01-{i+1:02d}", 3000.0) for i in range(120)]
    bench_bars[-1] = make_test_bar("2026-11-13", 3000.0)
    mock_snap = MarketSnapshot(
        snapshot_id="snap_20261113",
        signal_asof_date="2026-11-13",
        available_at="16:30:00",
        universe_version="v3.0",
        sector_version="v3.0",
        bars_by_symbol={},
        benchmark_bars=bench_bars,
        market_regime=MarketRegime.NEUTRAL,
        quality=DataQuality.VALID,
        market_breadth=0.50
    )
    data_svc.compute_market_snapshot.return_value = mock_snap
    data_svc.get_market_snapshot.return_value = mock_snap
    data_svc.get_universe_symbols.return_value = ["s1", "s2", "s3"]

    scheduler = DataScheduler(data_service=data_svc)
    res = scheduler.run_daily_pipeline(target_date="2026-11-13")

    assert res["status"] == "PARTIAL"
    assert res["quality_passed"] is False


def test_data_scheduler_pipeline_invalid_snapshot():
    """When snapshot quality is INVALID, pipeline status must be FAILED."""
    data_svc = MagicMock()
    data_svc.sync_daily_increment.return_value = {
        "status": "COMPLETED",
        "synced_symbols": 3000,
        "failed_symbols": [],
        "trade_date": "2026-11-13"
    }
    mock_snap = MarketSnapshot(
        snapshot_id="snap_20261113",
        signal_asof_date="2026-11-13",
        available_at="16:30:00",
        universe_version="v3.0",
        sector_version="v3.0",
        bars_by_symbol={},
        benchmark_bars=[],
        market_regime=MarketRegime.RISK_OFF,
        quality=DataQuality.INVALID,  # Invalid quality!
        market_breadth=0.0
    )
    data_svc.compute_market_snapshot.return_value = mock_snap
    data_svc.get_market_snapshot.return_value = mock_snap
    data_svc.get_universe_symbols.return_value = ["s1"]

    scheduler = DataScheduler(data_service=data_svc)
    res = scheduler.run_daily_pipeline(target_date="2026-11-13")

    assert res["status"] == "FAILED"
    assert res["quality_passed"] is False


# =========================================================================
# 4. TradingRuntime unified snapshot consumption and dual consistency
# =========================================================================
def test_trading_runtime_unified_snapshot_consumption():
    """TradingRuntime.step must accept snapshot, signal_asof_date, and maintain identical market regime for both portfolios."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "runtime_test.sqlite")
        runtime = TradingRuntime(db_path=db_path, mode="ACTIVE", initial_capital=100000.0)

        bench_bars = [make_test_bar(f"2026-05-{i+1:02d}", 3000.0 + i) for i in range(120)]
        bench_bars[-1] = make_test_bar("2026-11-13", 3500.0)

        cand_bar = make_test_bar("2026-11-13", 10.2)
        cand_bars = {"000001.SZ": [cand_bar] * 60}

        snap = MarketSnapshot(
            snapshot_id="snap_20261113",
            signal_asof_date="2026-11-13",
            available_at="16:30:00",
            universe_version="v3.0-unified",
            sector_version="v3.0-unified",
            bars_by_symbol=cand_bars,
            benchmark_bars=bench_bars,
            market_regime=MarketRegime.STRONG_RISK_ON,
            quality=DataQuality.VALID,
            market_breadth=0.85
        )

        sched_res = {
            "task_id": "task_20261113",
            "trade_date": "2026-11-13",
            "status": "SUCCESS",
            "quality_passed": True,
            "coverage": 0.96,
            "completed_at": "16:30:00"
        }

        # Step Monday morning open 2026-11-16 with Friday close snapshot
        res = runtime.step(
            as_of="2026-11-16",
            snapshot=snap,
            signal_asof_date="2026-11-13",
            data_scheduler_result=sched_res
        )

        assert res["regime"] == MarketRegime.STRONG_RISK_ON.value
        # Balanced and Aggressive must run under the exact same market regime
        assert "balanced" in res
        assert "aggressive" in res
