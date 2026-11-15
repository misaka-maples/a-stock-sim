# tests/test_v3_dual_state_reproduction.py
# -*- coding: utf-8 -*-
"""
P0-1, P0-2, P1-3 问题复现测试套件
在独立测试数据库中预置真实历史状态，验证当前代码中的缺陷：
1. 双重账户状态缺陷：Broker 重新初始化 10 万元虚假可用资金，与 SQLite 账本脱节。
2. 历史已有持仓无法在重启后通过 Broker 正常卖出（因 Broker 内存持仓字典为空）。
3. 订单恢复缺陷：重启后 OrderManager 丢失未完成订单与 client_order_id。
4. 冻结资金权益双重计算缺陷：frozen_cash 在总资产计算中被重复叠加。
"""

import os
import tempfile
import sqlite3
import pytest
from pathlib import Path

from astock.trade.runtime import TradingRuntime
from astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger, to_cents
from astock.trade.models import Order, OrderStatus, OrderSide, OrderType, TradeFill
from astock.trade.errors import InsufficientSharesError


@pytest.fixture
def populated_test_db():
    """创建一个预置了正式账户历史状态的测试数据库副本"""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)

    ledger = SQLiteAccountLedger(path)
    # 初始化 Balanced: 现金 85122.41 元
    ledger.init_account("balanced", "balanced", initial_cash=85122.41)
    
    # 预置两只持仓: sh600261 (1300股), sh603955 (100股)
    now_iso = "2026-11-15T15:00:00"
    with ledger.connect() as conn:
        conn.execute(
            """INSERT INTO positions
               (account_id, symbol, name, sector, total_shares, tradable_shares, frozen_shares, avg_cost, last_price, market_value_cents, updated_at)
               VALUES ('balanced', 'sh600261', '阳光照明', '电子', 1300, 1300, 0, 6.78, 6.78, ?, ?)""",
            (to_cents(1300 * 6.78), now_iso)
        )
        conn.execute(
            """INSERT INTO positions
               (account_id, symbol, name, sector, total_shares, tradable_shares, frozen_shares, avg_cost, last_price, market_value_cents, updated_at)
               VALUES ('balanced', 'sh603955', '大千生态', '公用', 100, 100, 0, 20.30, 20.30, ?, ?)""",
            (to_cents(100 * 20.30), now_iso)
        )
        # 预置历史成交记录
        conn.execute(
            """INSERT INTO orders
               (order_id, client_order_id, account_id, symbol, side, order_type, price, shares, filled_shares, status, reason, created_at, updated_at)
               VALUES ('ORD_HIST_01', 'C_HIST_01', 'balanced', 'sh600261', 'BUY', 'LIMIT', 6.78, 1300, 1300, 'FILLED', 'History', ?, ?)""",
            (now_iso, now_iso)
        )
        conn.execute(
            """INSERT INTO fills
               (fill_id, order_id, account_id, symbol, side, fill_price, fill_shares, amount_cents, fees_cents, slippage_cents, trade_date, created_at)
               VALUES ('FILL_HIST_01', 'ORD_HIST_01', 'balanced', 'sh600261', 'BUY', 6.78, 1300, ?, 500, 0, '2026-11-15', ?)""",
            (to_cents(1300 * 6.78), now_iso)
        )
        # 预置一条未完成挂单
        conn.execute(
            """INSERT INTO orders
               (order_id, client_order_id, account_id, symbol, side, order_type, price, shares, filled_shares, status, reason, created_at, updated_at)
               VALUES ('ORD_ACTIVE_OPEN', 'C_ACTIVE_OPEN', 'balanced', 'sh600036', 'BUY', 'LIMIT', 30.0, 500, 0, 'SUBMITTED', 'PendingOpen', ?, ?)""",
            (now_iso, now_iso)
        )

    yield path

    if os.path.exists(path):
        os.remove(path)


def test_reproduce_p0_1_dual_account_state_and_broker_fake_cash(populated_test_db):
    """
    复现 P0-1：
    当前代码中，TradingRuntime 重建后 Broker 重新创建 10 万元内存虚假资金，
    导致 Broker/Portfolio 与 SQLite 严重脱节！
    """
    runtime = TradingRuntime(db_path=populated_test_db, mode="ACTIVE")

    # 1. 检查 SQLite 中的真实资金
    sqlite_cash = runtime.ledger.get_balance("balanced").cash
    assert sqlite_cash == 85122.41, "SQLite 账本中的现金应为 85122.41"

    # 2. 检查 Broker 中的现金（预期修复后必须严格等于 85122.41，而不是 100000.0）
    broker_cash = runtime.broker.get_cash("balanced")
    
    # 3. 检查 PortfolioManager 获取的现金
    pm_cash = runtime.pm_bal.broker.get_cash("balanced")

    # 核心断言：消除双重状态后，Broker 与 Portfolio 读取的资金必须等于 SQLite 账本
    assert broker_cash == 85122.41, f"Broker 现金脱节: Broker={broker_cash}, SQLite={sqlite_cash}"
    assert pm_cash == 85122.41, f"PortfolioManager 现金脱节: PM={pm_cash}, SQLite={sqlite_cash}"


def test_reproduce_p0_1_broker_cannot_sell_existing_positions_after_restart(populated_test_db):
    """
    复现 P0-1 衍生致命缺陷：
    重启后如果直接使用 Broker 卖出 SQLite 中已有的 1300 股 sh600261，
    旧版代码会因为 Broker 内部字典为空抛出 InsufficientSharesError！
    """
    runtime = TradingRuntime(db_path=populated_test_db, mode="ACTIVE")

    # 构造卖出订单
    sell_order = Order(
        order_id="ORD_SELL_EXISTING_01",
        client_order_id="C_SELL_EXISTING_01",
        account_id="balanced",
        symbol="sh600261",
        side=OrderSide.SELL,
        shares=500,
        price=7.0,
        order_type=OrderType.MARKET
    )

    # 修复后必须能正常卖出，不抛出 InsufficientSharesError
    fill = runtime.broker.execute_order(sell_order, execution_price=7.0)
    assert fill is not None
    assert fill.shares == 500


def test_reproduce_p0_2_order_recovery_on_startup(populated_test_db):
    """
    复现 P0-2：
    重启后 OrderManager 必须恢复 SQLite 中存在的未完成活动订单（ORD_ACTIVE_OPEN / C_ACTIVE_OPEN）
    """
    runtime = TradingRuntime(db_path=populated_test_db, mode="ACTIVE")

    # 检查 OrderManager 是否恢复了未完成订单
    recovered_order = runtime.order_manager.get_order("ORD_ACTIVE_OPEN")
    assert recovered_order is not None, "OrderManager 必须恢复 SQLite 中的未完成订单"
    assert recovered_order.client_order_id == "C_ACTIVE_OPEN"
    assert recovered_order.status == OrderStatus.SUBMITTED

    # 检查按 client_order_id 索引是否可用
    by_client = runtime.order_manager.get_by_client_id("C_ACTIVE_OPEN")
    assert by_client is not None
    assert by_client.order_id == "ORD_ACTIVE_OPEN"


def test_reproduce_p1_3_equity_double_counts_frozen_cash(populated_test_db):
    """
    复现 P1-3：
    当账户存在冻结资金时，总权益计算不能把 frozen_cash 重复叠加到 total_cash 上！
    """
    ledger = SQLiteAccountLedger(populated_test_db)
    # 模拟冻结 10000 元现金
    with ledger.connect() as conn:
        conn.execute(
            """UPDATE accounts
               SET available_cash_cents = available_cash_cents - 1000000,
                   frozen_cash_cents = 1000000
               WHERE account_id = 'balanced'"""
        )

    bal = ledger.get_balance("balanced")
    # 会计恒等式：total_cash = 85122.41 (available: 75122.41, frozen: 10000.00)
    # stock_value = 1300*6.78 + 100*20.30 = 8814.00 + 2030.00 = 10844.00
    # 正确 total_equity 应为 85122.41 + 10844.00 = 95966.41
    # 旧版错误计算: 85122.41 + 10000.00 + 10844.00 = 105966.41 (虚增 10000 元!)
    expected_equity = round(bal.total_cash + bal.stock_market_value, 2)
    assert bal.total_equity == expected_equity, f"总资产重复计算冻结资金: total_equity={bal.total_equity}, expected={expected_equity}"
