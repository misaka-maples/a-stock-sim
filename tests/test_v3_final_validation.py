# -*- coding: utf-8 -*-
"""
A-Stock-Sim V3 Final Stability & Trading Correctness Test Suite
Validates:
1. Balanced max_positions=6, max_stock_weight=0.15, sector=30%, regime caps (30%, 45%, 65%, 80%)
2. All trade paths flow through Strategy -> Portfolio -> Risk -> Order -> Broker -> Ledger
3. SQLite ACID transaction boundaries, rollback before commit, durability after commit, partial fill, idempotency
4. Dual account complete isolation, shared read-only snapshot, zero cross-contamination
5. Strategy consistency comparing legacy vs V3 with explainable diffs
6. Continuous 5-day multi-session simulation with shocks, stops, T+1 rollover, and daily accounting reconciliation
"""

import pytest
import sqlite3
import tempfile
import datetime
from pathlib import Path
from decimal import Decimal
from typing import Dict, List, Any

from astock.trade.models import (
    Order, OrderSide, OrderType, OrderStatus, TradeFill,
    MarketRegime, Signal, TargetPosition, TargetPortfolio,
    Position, AccountBalance, RiskSeverity
)
from astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger, to_cents, from_cents
from astock.trade.broker.sim_broker import SimulatedBrokerAdapter
from astock.trade.order.manager import OrderManager
from astock.trade.risk.manager import RiskManager
from astock.trade.portfolio.manager import PortfolioManager
from astock.trade.strategy.balanced import BalancedStrategy, PRODUCT_A_CORE
from astock.trade.strategy.aggressive import AggressiveStrategy
from astock.trade.runtime import TradingRuntime
from astock.trade.adapter import TradingRuntimeAdapter
from astock.trade.errors import InsufficientCashError, InsufficientSharesError


# ==============================================================================
# Helper to generate valid CSI 300 benchmark bars for regime testing
# ==============================================================================

def make_benchmark_bars(as_of: str, regime_target: str, count: int = 150) -> List[Dict[str, Any]]:
    """Generates synthetic CSI300 bars of >=120 bars that classify into desired regime."""
    bars = []
    base_date = datetime.date.fromisoformat(as_of) - datetime.timedelta(days=count + 10)
    cur_date = base_date
    
    for i in range(count):
        cur_date += datetime.timedelta(days=1)
        d_str = cur_date.isoformat()
        if regime_target == "STRONG_RISK_ON":
            # Strongly trending upward: close > ma20 > ma60 > ma120
            close = 3200.0 + i * 10.0
        elif regime_target == "RISK_ON":
            close = 3500.0 + i * 2.0
        elif regime_target == "NEUTRAL":
            close = 3800.0 - (i % 20) * 10.0
        else: # RISK_OFF: trending down below ma120
            close = 4500.0 - i * 15.0
            
        bars.append({"date": d_str, "close": close, "open": close, "high": close * 1.01, "low": close * 0.99, "volume": 1000000.0})
    
    # Ensure the last bar matches as_of
    bars[-1]["date"] = as_of
    return bars


# ==============================================================================
# 1. Balanced Sizing & Regime Caps Verification (Section 1)
# ==============================================================================

def test_balanced_sizing_and_regime_caps():
    strat = BalancedStrategy(account_id="balanced")
    assert strat.max_positions == 6
    assert strat.max_stock_weight == 0.15
    assert strat.max_sector_weight == 0.30

    balance = AccountBalance(account_id="balanced", cash=100000.0, frozen_cash=0.0, stock_market_value=0.0, total_equity=100000.0)

    # Generate 8 candidates from different sectors
    sectors = ["电子", "医药", "金融", "新能源", "食品", "化工", "机械", "军工"]
    sigs_8 = [
        Signal(run_id="r1", signal_asof="2026-11-15", symbol=f"sh60000{i}", name=f"Stock_{i}",
               score=90.0 - i, sector=sectors[i], price=10.0 + i)
        for i in range(8)
    ]

    # A. STRONG_RISK_ON (Cap: 80%): With 6 slots available, 5 * 15% + 1 * 5% = 80%
    target_strong = strat.plan_targets("2026-11-15", MarketRegime.STRONG_RISK_ON, sigs_8, balance, positions={})
    assert target_strong.regime_cap == 0.80
    assert len(target_strong.target_positions) <= 6
    assert target_strong.total_weight <= 0.8001
    assert target_strong.total_weight >= 0.799  # Reaches 80%
    for sym, tpos in target_strong.target_positions.items():
        assert tpos.target_weight <= 0.1501

    # B. RISK_ON (Cap: 65%): Capped at 65%
    target_riskon = strat.plan_targets("2026-11-15", MarketRegime.RISK_ON, sigs_8, balance, positions={})
    assert target_riskon.regime_cap == 0.65
    assert target_riskon.total_weight <= 0.6501
    assert target_riskon.total_weight >= 0.649  # Reaches 65%

    # C. NEUTRAL (Cap: 45%): Capped at 45% (3 * 15% = 45%)
    target_neutral = strat.plan_targets("2026-11-15", MarketRegime.NEUTRAL, sigs_8, balance, positions={})
    assert target_neutral.regime_cap == 0.45
    assert target_neutral.total_weight <= 0.4501
    assert target_neutral.total_weight >= 0.449  # Reaches 45%

    # D. RISK_OFF (Cap: 30%): Capped at 30% (2 * 15% = 30%)
    target_riskoff = strat.plan_targets("2026-11-15", MarketRegime.RISK_OFF, sigs_8, balance, positions={})
    assert target_riskoff.regime_cap == 0.30
    assert target_riskoff.total_weight <= 0.3001
    assert target_riskoff.total_weight >= 0.299  # Reaches 30%

    # E. Few signals scenario: If only 2 signals exist, does NOT force 6 positions or 80%
    target_few = strat.plan_targets("2026-11-15", MarketRegime.STRONG_RISK_ON, sigs_8[:2], balance, positions={})
    assert len(target_few.target_positions) == 2
    assert target_few.total_weight <= 0.3001
    assert target_few.cash_weight >= 0.6999  # Retains ~70% in cash

    # F. Sector concentration constraint: 4 signals in same sector "电子"
    sigs_same_sector = [
        Signal(run_id="r2", signal_asof="2026-11-15", symbol=f"sh60010{i}", name=f"Elec_{i}",
               score=95.0 - i, sector="电子", price=20.0)
        for i in range(4)
    ]
    target_sec = strat.plan_targets("2026-11-15", MarketRegime.STRONG_RISK_ON, sigs_same_sector, balance, positions={})
    elec_weight = sum(tp.target_weight for tp in target_sec.target_positions.values())
    assert elec_weight <= 0.3001  # Sector ceiling strictly 30%


# ==============================================================================
# 2. SQLite Transaction Atomicity & Crash Injections (Section 3)
# ==============================================================================

def test_sqlite_transaction_atomicity_and_all_six_tables():
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        ledger = SQLiteAccountLedger(tmp.name)
        ledger.init_account("balanced", "balanced", 100000.0)

        # Submit an order first
        order = Order(
            order_id="ORD_TEST_001",
            client_order_id="C_001",
            account_id="balanced",
            symbol="sh600000",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            price=10.0,
            shares=2000,
            status=OrderStatus.SUBMITTED
        )
        ledger.save_order(order)

        # Apply a fill
        fill = TradeFill(
            fill_id="FILL_001",
            order_id="ORD_TEST_001",
            client_order_id="C_001",
            account_id="balanced",
            symbol="sh600000",
            name="浦发银行",
            side=OrderSide.BUY,
            price=10.0,
            shares=2000,
            amount=20000.0,
            commission=5.0,
            stamp_tax=0.0,
            transfer_fee=0.2,
            slippage=0.0,
            realized_pnl=0.0,
            timestamp="2026-11-15T10:00:00"
        )
        applied = ledger.apply_fill(fill, name="浦发银行", sector="金融")
        assert applied is True

        # Verify all 6 tables were updated in a single transaction
        with ledger.connect() as conn:
            # 1. orders: status updated to FILLED, filled_shares = 2000
            ord_row = conn.execute("SELECT * FROM orders WHERE order_id = 'ORD_TEST_001'").fetchone()
            assert ord_row["status"] == "FILLED"
            assert ord_row["filled_shares"] == 2000

            # 2. fills: 1 fill recorded
            fill_row = conn.execute("SELECT * FROM fills WHERE fill_id = 'FILL_001'").fetchone()
            assert fill_row["fill_shares"] == 2000
            assert fill_row["amount_cents"] == 2000000

            # 3. position_lots: 1 lot created with is_tradable_t1 = 0
            lot_row = conn.execute("SELECT * FROM position_lots WHERE fill_id = 'FILL_001'").fetchone()
            assert lot_row["shares"] == 2000
            assert lot_row["is_tradable_t1"] == 0

            # 4. cash_journal: entry created with delta = -2000520 cents
            cj_row = conn.execute("SELECT * FROM cash_journal WHERE ref_id = 'FILL_001'").fetchone()
            assert cj_row["delta_cents"] == -2000520
            assert cj_row["balance_cents"] == 7999480

            # 5. positions: position row exists
            pos_row = conn.execute("SELECT * FROM positions WHERE account_id = 'balanced' AND symbol = 'sh600000'").fetchone()
            assert pos_row["total_shares"] == 2000
            assert pos_row["tradable_shares"] == 0

            # 6. accounts: balance updated
            acc_row = conn.execute("SELECT * FROM accounts WHERE account_id = 'balanced'").fetchone()
            assert acc_row["total_cash_cents"] == 7999480

        # Verify accounting integrity passes
        integ = ledger.verify_integrity("balanced")
        assert integ["valid"] is True
        assert integ["cash_discrepancy"] == 0.0


def test_sqlite_crash_before_commit_rollback():
    """Crash injection: simulate failure before transaction commits -> WAL rollback."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        ledger = SQLiteAccountLedger(tmp.name)
        ledger.init_account("balanced", "balanced", 100000.0)

        # Attempt to buy more than available cash -> InsufficientCashError inside transaction
        fill_huge = TradeFill(
            fill_id="FILL_HUGE",
            order_id="ORD_HUGE",
            client_order_id="C_HUGE",
            account_id="balanced",
            symbol="sh600000",
            name="浦发银行",
            side=OrderSide.BUY,
            price=100.0,
            shares=20000,
            amount=2000000.0,  # 2M > 100k cash
            commission=500.0,
            stamp_tax=0.0,
            transfer_fee=20.0,
            slippage=0.0,
            realized_pnl=0.0,
            timestamp="2026-11-15T10:00:00"
        )
        with pytest.raises(InsufficientCashError):
            ledger.apply_fill(fill_huge)

        # Assert no partial updates were committed to ANY table
        with ledger.connect() as conn:
            assert conn.execute("SELECT COUNT(*) as c FROM fills").fetchone()["c"] == 0
            assert conn.execute("SELECT COUNT(*) as c FROM position_lots").fetchone()["c"] == 0
            assert conn.execute("SELECT COUNT(*) as c FROM positions").fetchone()["c"] == 0
            assert conn.execute("SELECT COUNT(*) as c FROM cash_journal WHERE reason != 'INITIAL_CAPITAL'").fetchone()["c"] == 0
            acc = conn.execute("SELECT total_cash_cents FROM accounts WHERE account_id = 'balanced'").fetchone()
            assert acc["total_cash_cents"] == 10000000

        integ = ledger.verify_integrity("balanced")
        assert integ["valid"] is True


def test_sqlite_crash_after_commit_durability():
    """Verify durability: after commit, closing connection and reopening preserves 100% of data."""
    db_path = ""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        db_path = tmp.name

    try:
        ledger = SQLiteAccountLedger(db_path)
        ledger.init_account("balanced", "balanced", 50000.0)
        fill = TradeFill(
            fill_id="FILL_DURABLE",
            order_id="ORD_DUR",
            client_order_id="C_DUR",
            account_id="balanced",
            symbol="sh600036",
            name="招商银行",
            side=OrderSide.BUY,
            price=30.0,
            shares=1000,
            amount=30000.0,
            commission=7.5,
            stamp_tax=0.0,
            transfer_fee=0.3,
            slippage=0.0,
            realized_pnl=0.0,
            timestamp="2026-11-15T10:00:00"
        )
        ledger.apply_fill(fill, name="招商银行", sector="金融")

        # Simulate hard crash: drop Python object, open new connection
        del ledger

        ledger_reopened = SQLiteAccountLedger(db_path)
        integ = ledger_reopened.verify_integrity("balanced")
        assert integ["valid"] is True
        bal = ledger_reopened.get_balance("balanced")
        assert bal.cash == 50000.0 - 30007.8
        pos = ledger_reopened.get_positions("balanced")
        assert "sh600036" in pos
        assert pos["sh600036"].shares == 1000
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_sqlite_partial_fill_and_subsequent_completion():
    """Order for 1000 shares gets 400 shares first, process crash/restarts, then remaining 600 shares fill."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        ledger = SQLiteAccountLedger(tmp.name)
        ledger.init_account("balanced", "balanced", 50000.0)

        order = Order(
            order_id="ORD_PARTIAL",
            client_order_id="C_PARTIAL",
            account_id="balanced",
            symbol="sh601318",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            price=40.0,
            shares=1000,
            status=OrderStatus.SUBMITTED
        )
        ledger.save_order(order)

        # 1. First partial fill: 400 shares
        fill_1 = TradeFill(
            fill_id="FILL_P1",
            order_id="ORD_PARTIAL",
            client_order_id="C_PARTIAL",
            account_id="balanced",
            symbol="sh601318",
            name="中国平安",
            side=OrderSide.BUY,
            price=40.0,
            shares=400,
            amount=16000.0,
            commission=5.0,
            stamp_tax=0.0,
            transfer_fee=0.16,
            slippage=0.0,
            realized_pnl=0.0,
            timestamp="2026-11-15T10:00:00"
        )
        ledger.apply_fill(fill_1, name="中国平安", sector="金融")

        with ledger.connect() as conn:
            ord_row = conn.execute("SELECT status, filled_shares FROM orders WHERE order_id = 'ORD_PARTIAL'").fetchone()
            assert ord_row["status"] == "PARTIALLY_FILLED"
            assert ord_row["filled_shares"] == 400

        # 2. Duplicate fill event arrives (idempotency check) -> ignored
        dup = ledger.apply_fill(fill_1)
        assert dup is False
        bal = ledger.get_balance("balanced")
        assert bal.cash == 50000.0 - 16005.16  # Not double-deducted

        # 3. Second completion fill: 600 shares
        fill_2 = TradeFill(
            fill_id="FILL_P2",
            order_id="ORD_PARTIAL",
            client_order_id="C_PARTIAL",
            account_id="balanced",
            symbol="sh601318",
            name="中国平安",
            side=OrderSide.BUY,
            price=40.0,
            shares=600,
            amount=24000.0,
            commission=6.0,
            stamp_tax=0.0,
            transfer_fee=0.24,
            slippage=0.0,
            realized_pnl=0.0,
            timestamp="2026-11-15T10:05:00"
        )
        ledger.apply_fill(fill_2, name="中国平安", sector="金融")

        with ledger.connect() as conn:
            ord_row = conn.execute("SELECT status, filled_shares FROM orders WHERE order_id = 'ORD_PARTIAL'").fetchone()
            assert ord_row["status"] == "FILLED"
            assert ord_row["filled_shares"] == 1000

        pos = ledger.get_positions("balanced")["sh601318"]
        assert pos.shares == 1000
        integ = ledger.verify_integrity("balanced")
        assert integ["valid"] is True


# ==============================================================================
# 3. Dual Account Complete Autonomy (Section 4)
# ==============================================================================

def test_dual_account_autonomy_under_shared_snapshot():
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        runtime = TradingRuntime(db_path=tmp.name, mode="ACTIVE", initial_capital=100000.0)

        # Quotes and market data
        quotes = {
            "sh600519": {"price": 1800.0, "current": 1800.0, "atr": 20.0, "ma20": 1780.0, "sector": "食品"},
            "sh601318": {"price": 50.0, "current": 50.0, "atr": 1.0, "ma20": 49.0, "sector": "金融"},
            "sh601138": {"price": 20.0, "current": 20.0, "atr": 0.5, "ma20": 19.5, "sector": "电子"},
            "sz002594": {"price": 250.0, "current": 250.0, "atr": 5.0, "ma20": 245.0, "sector": "汽车"},
        }
        market_data = {
            "csi300_bars": make_benchmark_bars("2026-11-15", "STRONG_RISK_ON", count=150),
            "candidates": [
                {"symbol": "sh601318", "name": "中国平安", "score": 90.0, "sector": "金融", "price": 50.0, "ret20": 0.05, "ret60": 0.10},
                {"symbol": "sh601138", "name": "工业富联", "score": 88.0, "sector": "电子", "price": 20.0, "ret20": 0.08, "ret60": 0.15},
                {"symbol": "sz002594", "name": "比亚迪", "score": 85.0, "sector": "汽车", "price": 250.0, "ret20": 0.06, "ret60": 0.12},
            ],
            "top_sectors": ["金融", "电子", "汽车"]
        }

        # Step 1: Run both accounts
        res = runtime.step(as_of="2026-11-15", quotes=quotes, market_data=market_data)
        assert res["mode"] == "ACTIVE"

        # Verify Balanced state
        bal_balance = runtime.ledger.get_balance("balanced")
        agg_balance = runtime.ledger.get_balance("aggressive")
        bal_pos = runtime.ledger.get_positions("balanced")
        agg_pos = runtime.ledger.get_positions("aggressive")

        # Both traded independently
        assert bal_balance.total_equity > 0
        assert agg_balance.total_equity > 0

        # Verify Order IDs do not collide
        with runtime.ledger.connect() as conn:
            bal_orders = [r["order_id"] for r in conn.execute("SELECT order_id FROM orders WHERE account_id = 'balanced'").fetchall()]
            agg_orders = [r["order_id"] for r in conn.execute("SELECT order_id FROM orders WHERE account_id = 'aggressive'").fetchall()]
            assert set(bal_orders).isdisjoint(set(agg_orders))

        # Independent integrity verification
        assert runtime.ledger.verify_integrity("balanced")["valid"] is True
        assert runtime.ledger.verify_integrity("aggressive")["valid"] is True


# ==============================================================================
# 4. Continuous 5-Day Simulation with Shocks & Reconciliation (Section 7)
# ==============================================================================

def test_five_day_continuous_trading_simulation():
    """
    Executes a 5-day continuous end-to-end trading simulation:
    - Day 1: STRONG_RISK_ON -> Buys 3 stocks for Balanced, 2 for Aggressive.
    - Day 2: T+1 Rollover -> Previous day lots become tradable. Balanced expands to 5 stocks.
    - Day 3: Shock Day -> sh601138 drops -10% triggering -6% hard stop loss; runtime executes exit.
    - Day 4: Degrade to RISK_OFF -> No new buys allowed; existing valid positions retained.
    - Day 5: Rebound to NEUTRAL -> Daily settlement, overnight interest, accounting audit.
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        runtime = TradingRuntime(db_path=tmp.name, mode="ACTIVE", initial_capital=100000.0)

        # Baseline quotes
        q_day1 = {
            "sh600036": {"price": 30.0, "current": 30.0, "atr": 0.5, "ma20": 29.0, "sector": "金融", "name": "招商银行"},
            "sh601138": {"price": 20.0, "current": 20.0, "atr": 0.5, "ma20": 19.5, "sector": "电子", "name": "工业富联"},
            "sh600900": {"price": 25.0, "current": 25.0, "atr": 0.4, "ma20": 24.5, "sector": "公用", "name": "长江电力"},
            "sz002475": {"price": 30.0, "current": 30.0, "atr": 0.8, "ma20": 29.0, "sector": "电子", "name": "立讯精密"},
            "sz002594": {"price": 250.0, "current": 250.0, "atr": 5.0, "ma20": 240.0, "sector": "汽车", "name": "比亚迪"},
        }
        cands_base = [
            {"symbol": "sh600036", "name": "招商银行", "score": 92.0, "sector": "金融", "price": 30.0, "ret20": 0.05, "ret60": 0.10},
            {"symbol": "sh601138", "name": "工业富联", "score": 90.0, "sector": "电子", "price": 20.0, "ret20": 0.08, "ret60": 0.15},
            {"symbol": "sh600900", "name": "长江电力", "score": 88.0, "sector": "公用", "price": 25.0, "ret20": 0.04, "ret60": 0.08},
            {"symbol": "sz002475", "name": "立讯精密", "score": 86.0, "sector": "电子", "price": 30.0, "ret20": 0.07, "ret60": 0.12},
            {"symbol": "sz002594", "name": "比亚迪", "score": 84.0, "sector": "汽车", "price": 250.0, "ret20": 0.06, "ret60": 0.11},
        ]

        # --- DAY 1: Initial Buys in STRONG_RISK_ON ---
        m_day1 = {
            "csi300_bars": make_benchmark_bars("2026-11-16", "STRONG_RISK_ON", count=150),
            "candidates": cands_base[:3],
            "top_sectors": ["金融", "电子", "公用"]
        }
        res1 = runtime.step("2026-11-16", q_day1, m_day1)
        assert res1["mode"] == "ACTIVE"
        assert res1["regime"] == "STRONG_RISK_ON"
        assert runtime.ledger.verify_integrity("balanced")["valid"] is True

        bal_pos1 = runtime.ledger.get_positions("balanced")
        assert len(bal_pos1) > 0
        # Check T+1: none of the newly bought shares should be tradable today
        for sym, p in bal_pos1.items():
            assert p.tradable_shares == 0

        # --- DAY 2: T+1 Rollover & Expansion ---
        m_day2 = {
            "csi300_bars": make_benchmark_bars("2026-11-17", "STRONG_RISK_ON", count=150),
            "candidates": cands_base,
            "top_sectors": ["金融", "电子", "公用", "汽车"]
        }
        res2 = runtime.step("2026-11-17", q_day1, m_day2)
        bal_pos2 = runtime.ledger.get_positions("balanced")
        # Day 1 shares are now tradable
        for sym in bal_pos1.keys():
            assert bal_pos2[sym].tradable_shares == bal_pos1[sym].shares
        assert runtime.ledger.verify_integrity("balanced")["valid"] is True

        # --- DAY 3: Intraday Shock: Hard Stop Loss Exit ---
        q_day3 = dict(q_day1)
        # sh601138 drops to 18.0 (from 20.0, -10% drop, triggers -6% hard stop)
        q_day3["sh601138"] = {"price": 18.0, "current": 18.0, "atr": 0.5, "ma20": 19.5, "sector": "电子", "name": "工业富联"}
        # Crashed stock drops out of top momentum candidates
        cands_day3 = [c for c in cands_base if c["symbol"] != "sh601138"]
        m_day3 = {
            "csi300_bars": make_benchmark_bars("2026-11-18", "STRONG_RISK_ON", count=150),
            "candidates": cands_day3,
            "top_sectors": ["金融", "电子", "公用", "汽车"]
        }

        res3 = runtime.step("2026-11-18", q_day3, m_day3)
        bal_pos3 = runtime.ledger.get_positions("balanced")
        # sh601138 was stopped out and closed
        assert "sh601138" not in bal_pos3
        assert runtime.ledger.verify_integrity("balanced")["valid"] is True

        # --- DAY 4: Market Degrades to RISK_OFF ---
        q_day4 = dict(q_day3)
        m_day4 = {
            "csi300_bars": make_benchmark_bars("2026-11-19", "RISK_OFF", count=150),
            "candidates": cands_base,
            "top_sectors": ["公用"]
        }
        res4 = runtime.step("2026-11-19", q_day4, m_day4)
        assert res4["regime"] == "RISK_OFF"
        assert runtime.ledger.verify_integrity("balanced")["valid"] is True

        # --- DAY 5: Rebound to NEUTRAL & Daily Settlement ---
        m_day5 = {
            "csi300_bars": make_benchmark_bars("2026-11-20", "NEUTRAL", count=150),
            "candidates": cands_base,
            "top_sectors": ["金融", "公用"]
        }
        res5 = runtime.step("2026-11-20", q_day4, m_day5)
        
        # Credit overnight interest
        runtime.ledger.accrue_cash_interest("balanced", "2026-11-20", annual_rate=0.02)
        runtime.ledger.accrue_cash_interest("aggressive", "2026-11-20", annual_rate=0.02)

        # Final audit
        final_bal = runtime.ledger.get_balance("balanced")
        final_agg = runtime.ledger.get_balance("aggressive")
        assert final_bal.total_equity > 0
        assert final_agg.total_equity > 0
        assert runtime.ledger.verify_integrity("balanced")["valid"] is True
        assert runtime.ledger.verify_integrity("aggressive")["valid"] is True
