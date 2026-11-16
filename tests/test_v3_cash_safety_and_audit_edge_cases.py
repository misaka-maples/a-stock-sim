# tests/test_v3_cash_safety_and_audit_edge_cases.py
# -*- coding: utf-8 -*-
"""
Tests for:
1. P1-1: Daily acceptance recorder real A-share trading calendar, continuity, health check, idempotency.
2. P1-2: Environment isolation (ASTOCK_ENV, ASTOCK_DB_PATH, ASTOCK_AUDIT_PATH).
3. P1-3: Cash freeze, partial fill, unfreeze, cancel refund, and equity mathematical identity.
"""

import os
import tempfile
import json
import pytest
from pathlib import Path
from astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from astock.trade.daily_audit_recorder import DailyAuditRecorder, is_valid_acceptance_record, compute_consecutive_valid_trading_days
from astock.trade.models import Order, OrderSide, OrderType, OrderStatus, TradeFill
from astock.trade.config import get_db_path, get_audit_path, is_production, is_test
from astock.trade.errors import InsufficientCashError


def test_p1_3_cash_freeze_partial_fill_and_cancellation_refund():
    """
    P1-3: Verify cash freezing on buy order submission, partial fill deduction,
    and unfreezing/refund upon order cancellation.
    Mathematical invariant: total_cash = available_cash + frozen_cash
    total_equity = total_cash + stock_market_value (NEVER double count frozen_cash)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "cash_safety.sqlite")
        ledger = SQLiteAccountLedger(db_path)
        account_id = "balanced"
        ledger.init_account(account_id, "balanced", 100000.0)

        # Baseline check
        b0 = ledger.get_balance(account_id)
        assert b0.cash == 100000.0
        assert b0.available_cash == 100000.0
        assert b0.frozen_cash == 0.0
        assert b0.total_equity == 100000.0

        # Step 1: Place buy order for 1,000 shares @ 10.0 => 10,000 + 30 fee = 10,030.0
        order = Order(
            order_id="ORD_FREEZE_01",
            client_order_id="C_FREEZE_01",
            account_id=account_id,
            symbol="sh600000",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            price=10.0,
            shares=1000,
            status=OrderStatus.SUBMITTED
        )
        ledger.save_order(order)

        # Freeze 10,030 cash
        freeze_amt = 10030.0
        ok = ledger.freeze_cash(account_id, freeze_amt, order_id=order.order_id)
        assert ok is True

        b1 = ledger.get_balance(account_id)
        assert b1.cash == 100000.0
        assert b1.frozen_cash == freeze_amt
        assert b1.available_cash == round(100000.0 - freeze_amt, 2)
        # CRUCIAL: total_equity must NOT double count frozen cash!
        assert b1.total_equity == 100000.0

        # Attempting to freeze more than available cash must raise InsufficientCashError
        with pytest.raises(InsufficientCashError):
            ledger.freeze_cash(account_id, 95000.0)

        # Step 2: Partial Fill: 400 shares @ 10.0 = 4,000 + 12 fee = 4,012.0
        fill_partial = TradeFill(
            fill_id="FILL_PARTIAL_01",
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            account_id=account_id,
            symbol=order.symbol,
            name="浦发银行",
            side=order.side,
            price=10.0,
            shares=400,
            amount=4000.0,
            commission=5.0,
            stamp_tax=0.0,
            transfer_fee=0.08,
            slippage=0.0,
            realized_pnl=0.0,
            timestamp="2026-11-16T10:30:00"
        )
        ledger.apply_fill(fill_partial, name="浦发银行", sector="银行")

        b2 = ledger.get_balance(account_id)
        # Total fee = 5.08, debit = 4005.08
        actual_debit = 4005.08
        assert b2.cash == round(100000.0 - actual_debit, 2)
        assert b2.frozen_cash == round(freeze_amt - actual_debit, 2)
        assert b2.stock_market_value == 4000.0
        # total_equity = (100000 - 4005.08) + 4000 = 99994.92 (loss only 5.08 fee)
        assert b2.total_equity == round(b2.cash + b2.stock_market_value, 2)
        assert abs(b2.total_equity - 99994.92) < 0.01

        # Step 3: Cancel remaining 600 shares -> Unfreeze remaining frozen cash
        remaining_frozen = b2.frozen_cash
        ok_unfreeze = ledger.unfreeze_cash(account_id, remaining_frozen, order_id=order.order_id)
        assert ok_unfreeze is True

        b3 = ledger.get_balance(account_id)
        assert b3.frozen_cash == 0.0
        assert b3.available_cash == b3.cash
        assert b3.stock_market_value == 4000.0
        assert b3.total_equity == round(b3.cash + b3.stock_market_value, 2)


def test_p1_1_daily_acceptance_calendar_continuity_and_rejection():
    """
    P1-1: Tests for calendar day continuity, weekend rejection, and health validation.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "test_audit.json"
        recorder = DailyAuditRecorder(log_path=json_path)

        # 1. Non-trading day (e.g. 2026-11-21 Saturday) should NOT be counted as a valid trading day
        rec_weekend = {
            "trade_date": "2026-11-21",
            "sqlite_reconciliation": "MATCH (Discrepancy: 0.00)",
            "balanced_account": {"equity": 100000.0, "cash": 80000.0},
            "aggressive_account": {"equity": 100000.0, "cash": 60000.0},
            "error_summary": "None"
        }
        assert is_valid_acceptance_record(rec_weekend) is False

        # 2. Record with Discrepancy should fail validation
        rec_bad_recon = {
            "trade_date": "2026-11-16",
            "sqlite_reconciliation": "DISCREPANCY (B: 100.0, A: 0.0)",
            "balanced_account": {"equity": 100000.0, "cash": 80000.0},
            "aggressive_account": {"equity": 100000.0, "cash": 60000.0},
            "error_summary": "None"
        }
        assert is_valid_acceptance_record(rec_bad_recon) is False

        # 3. Test non-consecutive trading days: 2026-11-16 (Mon) and 2026-11-18 (Wed), missing Tue
        records = [
            {
                "trade_date": "2026-11-16",
                "market_sync_completed_at": "16:35:10",
                "sqlite_reconciliation": "MATCH",
                "balanced_account": {"equity": 100000.0, "cash": 80000.0},
                "aggressive_account": {"equity": 100000.0, "cash": 60000.0},
                "error_summary": "None"
            },
            {
                "trade_date": "2026-11-18", # skipped 2026-11-17
                "market_sync_completed_at": "16:35:10",
                "sqlite_reconciliation": "MATCH",
                "balanced_account": {"equity": 100000.0, "cash": 80000.0},
                "aggressive_account": {"equity": 100000.0, "cash": 60000.0},
                "error_summary": "None"
            }
        ]
        consec_count, streak = compute_consecutive_valid_trading_days(records)
        # Because Tuesday was skipped, streak is broken and resets to 1 (2026-11-18)
        assert consec_count == 1
        assert streak == ["2026-11-18"]

        # 4. Test 5 consecutive real trading days: 2026-11-16 to 2026-11-20
        good_5_days = []
        for i in range(1, 6):
            d = f"2026-11-{15+i:02d}"
            good_5_days.append({
                "trade_date": d,
                "market_sync_completed_at": "16:35:10",
                "sqlite_reconciliation": "MATCH (Discrepancy: 0.00)",
                "balanced_account": {"equity": 100000.0, "cash": 80000.0},
                "aggressive_account": {"equity": 100000.0, "cash": 60000.0},
                "error_summary": "None"
            })
        consec_count5, streak5 = compute_consecutive_valid_trading_days(good_5_days)
        assert consec_count5 == 5
        assert len(streak5) == 5

        # 5. Idempotent update on same trade_date does not increment count
        recorder.record_day(good_5_days[0])
        s1 = recorder.get_status()
        assert s1["recorded_trading_days"] == 1

        # Re-record same day with updated metric
        dup_rec = dict(good_5_days[0])
        dup_rec["peak_memory_mb"] = 500.0
        recorder.record_day(dup_rec)
        s2 = recorder.get_status()
        assert s2["recorded_trading_days"] == 1


def test_p1_2_environment_isolation(monkeypatch):
    """
    P1-2: Tests that test environment does NOT use production paths data/trading_v3.sqlite
    or data/audit/daily_acceptance_records.json.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = os.path.join(tmpdir, "isolated.sqlite")
        test_audit = os.path.join(tmpdir, "isolated_audit.json")

        monkeypatch.setenv("ASTOCK_ENV", "test")
        monkeypatch.setenv("ASTOCK_DB_PATH", test_db)
        monkeypatch.setenv("ASTOCK_AUDIT_PATH", test_audit)

        assert is_test() is True
        assert is_production() is False
        assert str(get_db_path()) == test_db
        assert str(get_audit_path()) == test_audit

        # Ensure production files are NEVER touched
        prod_db = Path("data/trading_v3.sqlite")
        prod_audit = Path("data/audit/daily_acceptance_records.json")

        assert get_db_path() != prod_db
        assert get_audit_path() != prod_audit
