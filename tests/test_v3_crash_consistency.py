# tests/test_v3_crash_consistency.py
# -*- coding: utf-8 -*-
"""
Verification of cross-module execution consistency under failure conditions:
1. Broker generates fill -> SQLite transaction fails before commit -> restart -> zero ghost fills, zero double deduction.
2. Atomicity of SQLite transactions across cash_journal, orders, fills, positions, and accounts.
3. Idempotent deduplication of trade fills.
"""

import os
import tempfile
import pytest
from unittest.mock import patch
from astock.trade.runtime import TradingRuntime
from astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from astock.trade.models import Order, OrderSide, OrderType, OrderStatus, TradeFill
from astock.trade.errors import InsufficientCashError


@pytest.fixture
def clean_runtime():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "crash_test.sqlite")
        rt = TradingRuntime(db_path=db_path, mode="ACTIVE", initial_capital=100000.0)
        yield rt, db_path


def test_crash_before_sqlite_commit_leaves_zero_ghost_fills(clean_runtime):
    """
    P0-3 Test Case:
    Broker generates TradeFill, but an exception or simulated crash occurs before
    the SQLite transaction commits.
    Upon system restart, account state must remain 100% identical to pre-attempt state.
    """
    runtime, db_path = clean_runtime
    account_id = "balanced"

    bal_pre = runtime.ledger.get_balance(account_id)
    pos_pre = runtime.ledger.get_positions(account_id)
    assert bal_pre.cash == 100000.0
    assert len(pos_pre) == 0

    order = runtime.order_manager.create_order(
        account_id=account_id,
        symbol="sh600000",
        side=OrderSide.BUY,
        shares=1000,
        price=10.0,
        client_order_id="C_CRASH_TEST_01"
    )
    runtime.ledger.save_order(order)

    # Simulate fill generated, but SQLite transaction fails during apply_fill (e.g. database error / abort)
    fill = TradeFill(
        fill_id="FILL_GHOST_TEST",
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        account_id=account_id,
        symbol=order.symbol,
        name="浦发银行",
        side=order.side,
        price=10.0,
        shares=1000,
        amount=10000.0,
        commission=5.0,
        stamp_tax=0.0,
        transfer_fee=0.2,
        slippage=0.0,
        realized_pnl=0.0,
        timestamp="2026-11-16T10:00:00"
    )

    # Simulate fill generated, but SQLite transaction fails midway (e.g. before updating accounts)
    orig_connect = runtime.ledger.connect

    class FailingConnection:
        def __init__(self, real_conn):
            self.real_conn = real_conn

        def execute(self, sql, params=()):
            if "UPDATE accounts" in sql:
                raise RuntimeError("Simulated OS / Database Crash during transaction!")
            return self.real_conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.real_conn, name)

    from contextlib import contextmanager

    @contextmanager
    def mock_connect():
        with orig_connect() as conn:
            yield FailingConnection(conn)

    runtime.ledger.connect = mock_connect

    with pytest.raises(RuntimeError):
        runtime.ledger.apply_fill(fill)

    # Restore connect method
    runtime.ledger.connect = orig_connect

    # --- SIMULATE PROCESS RESTART ---
    # Instantiate a completely fresh TradingRuntime pointing to the same SQLite database
    restarted_runtime = TradingRuntime(db_path=db_path, mode="ACTIVE")

    bal_post = restarted_runtime.ledger.get_balance(account_id)
    pos_post = restarted_runtime.ledger.get_positions(account_id)
    trades_post = restarted_runtime.ledger.get_trades(account_id)

    # Verify 100% consistency:
    assert bal_post.cash == 100000.0, f"Cash deducted on aborted fill! cash={bal_post.cash}"
    assert bal_post.available_cash == 100000.0
    assert bal_post.frozen_cash == 0.0
    assert bal_post.stock_market_value == 0.0
    assert bal_post.total_equity == 100000.0
    assert len(pos_post) == 0, f"Ghost positions created! {pos_post}"
    assert len(trades_post) == 0, f"Ghost fills recorded! {trades_post}"


def test_fill_idempotency_prevents_duplicate_deduction(clean_runtime):
    """
    P0-3 Test Case:
    Identical TradeFill received multiple times must be deduplicated idempotently.
    Cash and shares must only be updated once.
    """
    runtime, _ = clean_runtime
    account_id = "balanced"

    order = runtime.order_manager.create_order(
        account_id=account_id,
        symbol="sh600000",
        side=OrderSide.BUY,
        shares=1000,
        price=10.0,
        client_order_id="C_IDEMPOTENT_01"
    )
    runtime.ledger.save_order(order)

    fill = TradeFill(
        fill_id="FILL_IDEMPOTENT_01",
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        account_id=account_id,
        symbol=order.symbol,
        name="浦发银行",
        side=order.side,
        price=10.0,
        shares=1000,
        amount=10000.0,
        commission=5.0,
        stamp_tax=0.0,
        transfer_fee=0.2,
        slippage=0.0,
        realized_pnl=0.0,
        timestamp="2026-11-16T10:00:00"
    )

    # First execution succeeds
    res1 = runtime.ledger.apply_fill(fill)
    assert res1 is True

    bal1 = runtime.ledger.get_balance(account_id)
    pos1 = runtime.ledger.get_positions(account_id)
    assert pos1["sh600000"].shares == 1000
    expected_cash = round(100000.0 - 10000.0 - 5.2, 2)
    assert bal1.cash == expected_cash

    # Second execution of duplicate fill returns False and alters NOTHING
    res2 = runtime.ledger.apply_fill(fill)
    assert res2 is False

    bal2 = runtime.ledger.get_balance(account_id)
    pos2 = runtime.ledger.get_positions(account_id)
    assert bal2.cash == expected_cash
    assert pos2["sh600000"].shares == 1000
    assert len(runtime.ledger.get_trades(account_id)) == 1
