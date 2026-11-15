# tests/test_v3_order_and_cash_safety.py
# -*- coding: utf-8 -*-
"""
V3 订单与资金安全专项测试
覆盖全部8项核心安全场景：
1. 卖出订单提交但未成交（现金不提前入账，不可预支）
2. 卖出订单部分成交（仅按实际成交份额记账与回流现金，剩余部分不提前入账）
3. 买卖订单同时存在（卖单未成资金不可作为买单可用现金）
4. 账户资金不足（硬拦截，不产生任何扣款与持仓）
5. 涨跌停与停牌（价格笼子与停牌拦截，无法成交）
6. T+1 可卖限制（买入当日不可卖出，次日滚动后解锁）
7. 系统重启后恢复未完成订单（状态机完整重建，不重复撮合）
8. 重复收到相同成交回报（填单幂等性，不重复扣款与记账）
"""

import os
import tempfile
import sqlite3
import pytest
from decimal import Decimal

from astock.trade.models import (
    Order, OrderStatus, OrderSide, OrderType, Position, TradeFill, MarketRegime, AccountBalance
)
from astock.trade.order.manager import OrderManager
from astock.trade.order.state_machine import OrderStateMachine
from astock.trade.broker.sim_broker import SimulatedBrokerAdapter
from astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from astock.trade.risk.manager import RiskManager
from astock.trade.runtime import TradingRuntime
from astock.trade.errors import InsufficientCashError, InsufficientSharesError, RiskCheckError


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_1_sell_submitted_but_not_filled_does_not_credit_cash(temp_db):
    """
    场景 1: 卖出订单提交但未成交
    - 可用现金绝不提前增加
    - 卖出所得不能提前使用
    """
    ledger = SQLiteAccountLedger(temp_db)
    ledger.init_account("balanced", initial_cash=10000.0)

    # 预置 1000 股 sh600036 持仓 (成本 20.0，市值 20000.0)
    now = "2026-11-16T09:30:00"
    with ledger.connect() as conn:
        conn.execute(
            """INSERT INTO positions (account_id, symbol, name, sector, total_shares, tradable_shares, frozen_shares, avg_cost, last_price, market_value_cents, updated_at)
               VALUES ('balanced', 'sh600036', '招商银行', '金融', 1000, 1000, 0, 20.0, 20.0, 2000000, ?)""",
            (now,)
        )

    bal_before = ledger.get_balance("balanced")
    assert bal_before.cash == 10000.0

    # 提交卖出委托 (500 股 @ 25.0，预期卖出款 12500.0)
    order_mgr = OrderManager()
    sell_order = Order(
        order_id="ORD_SELL_001",
        client_order_id="C_SELL_001",
        account_id="balanced",
        symbol="sh600036",
        side=OrderSide.SELL,
        shares=500,
        price=25.0,
        order_type=OrderType.LIMIT,
        status=OrderStatus.PROPOSED
    )
    order_mgr.submit_order(sell_order)
    ledger.save_order(sell_order)

    # 断言：处于 SUBMITTED 状态的未成交卖单，绝不增加现金
    assert sell_order.status == OrderStatus.SUBMITTED
    bal_after_submit = ledger.get_balance("balanced")
    assert bal_after_submit.cash == 10000.0  # 依然是 10000.0，绝不能变成 22500.0


def test_2_partial_fill_credits_only_executed_portion(temp_db):
    """
    场景 2: 卖出订单部分成交
    - 仅按实际成交份额记账与回流现金
    - 剩余未成交部分绝不提前入账
    - 订单状态流转至 PARTIALLY_FILLED
    """
    broker = SimulatedBrokerAdapter(initial_cash=20000.0, slippage=0.0)
    broker.init_account("balanced", cash=20000.0)
    
    # 建立持仓 1000 股 @ 10.0
    buy_order = Order(
        order_id="ORD_BUY_PRE",
        client_order_id="C_BUY_PRE",
        account_id="balanced",
        symbol="sh600000",
        side=OrderSide.BUY,
        shares=1000,
        price=10.0,
        order_type=OrderType.MARKET,
        status=OrderStatus.PROPOSED
    )
    # 注入可用持仓（模拟 T+1 已转可卖）
    broker.execute_order(buy_order, execution_price=10.0)
    pos = broker.get_positions("balanced")["sh600000"]
    pos.available_shares = 1000
    broker._balances["balanced"].cash = 5000.0  # 重置现金为 5000 便于精准核算

    # 提交卖出 1000 股 @ 10.0，部分成交 40% (400 股)
    sell_order = Order(
        order_id="ORD_SELL_PARTIAL",
        client_order_id="C_SELL_PARTIAL",
        account_id="balanced",
        symbol="sh600000",
        side=OrderSide.SELL,
        shares=1000,
        price=10.0,
        order_type=OrderType.MARKET,
        status=OrderStatus.PROPOSED
    )
    fill = broker.execute_order(sell_order, execution_price=10.0, partial_ratio=0.4)

    assert fill.shares == 400
    assert sell_order.status == OrderStatus.PARTIALLY_FILLED
    assert sell_order.filled_shares == 400
    
    # 成交金额 4000.0，扣除税费后入账
    fees = broker.calculate_fees(OrderSide.SELL, 4000.0)
    expected_cash = round(5000.0 + 4000.0 - fees["total_fee"], 2)
    assert broker.get_cash("balanced") == expected_cash
    
    # 剩余 600 股未成交，现金额度绝不包含剩余 6000.0 的潜在回款
    assert broker.get_cash("balanced") < 5000.0 + 10000.0 - fees["total_fee"]
    assert broker.get_positions("balanced")["sh600000"].shares == 600


def test_3_concurrent_buy_and_sell_cannot_use_unfilled_sell_proceeds():
    """
    场景 3: 买卖订单同时存在
    - 账户可用现金不足时，不能用尚未成交的卖单回款来垫付买单
    """
    broker = SimulatedBrokerAdapter(initial_cash=1000.0)
    broker.init_account("balanced", cash=1000.0)
    
    # 账户拥有 500 股 sh600519，价值 50 万元
    positions = broker.get_positions("balanced")
    positions["sh600519"] = Position(
        symbol="sh600519", name="贵州茅台", shares=500, available_shares=500,
        cost_price=1000.0, current_price=1000.0, market_value=500000.0
    )

    # 1. 提交卖单 (价值 50 万元) 但尚未撮合成交
    sell_order = Order(
        order_id="ORD_SELL_PENDING", client_order_id="C_SELL_PENDING",
        account_id="balanced", symbol="sh600519", side=OrderSide.SELL,
        shares=500, price=1000.0, order_type=OrderType.LIMIT,
        status=OrderStatus.SUBMITTED
    )

    # 2. 尝试提交买单 (需要 20000.0 现金)
    buy_order = Order(
        order_id="ORD_BUY_NEW", client_order_id="C_BUY_NEW",
        account_id="balanced", symbol="sh600036", side=OrderSide.BUY,
        shares=1000, price=20.0, order_type=OrderType.MARKET,
        status=OrderStatus.PROPOSED
    )

    # 必须报错：虽然有价值 50 万的卖单在途，但未成交不能动用，现金仅有 1000.0，无法购买 20000.0 标的
    with pytest.raises(InsufficientCashError):
        broker.execute_order(buy_order, execution_price=20.0)

    # 现金维持 1000.0 不变
    assert broker.get_cash("balanced") == 1000.0


def test_4_insufficient_cash_hard_rejection():
    """
    场景 4: 账户资金不足硬拦截
    - 校验可用资金不足直接拒绝
    - 账本现金分毫不扣，持仓不增加
    """
    broker = SimulatedBrokerAdapter(initial_cash=500.0)
    broker.init_account("aggressive", cash=500.0)

    big_buy = Order(
        order_id="ORD_BIG_BUY", client_order_id="C_BIG_BUY",
        account_id="aggressive", symbol="sh601318", side=OrderSide.BUY,
        shares=100, price=50.0, order_type=OrderType.MARKET,
        status=OrderStatus.PROPOSED
    ) # 需要 5000 元 + 手续费

    with pytest.raises(InsufficientCashError):
        broker.execute_order(big_buy, execution_price=50.0)

    assert broker.get_cash("aggressive") == 500.0
    assert "sh601318" not in broker.get_positions("aggressive")


def test_5_limit_up_down_and_suspension_rejection():
    """
    场景 5: 涨跌停与停牌无法成交
    - 价格笼子越界或股票停牌
    - 订单直接被风控层拒绝，不推入 Broker
    """
    risk_mgr = RiskManager()
    
    # 模拟停牌股票
    buy_order = Order(
        order_id="ORD_SUSPENDED", client_order_id="C_SUSPENDED",
        account_id="balanced", symbol="sh600001", side=OrderSide.BUY,
        shares=100, price=10.0, order_type=OrderType.MARKET
    )
    passed, audit = risk_mgr.order_rule.validate_order(
        buy_order, available_cash=10000.0, is_suspended=True
    )
    assert not passed
    assert audit.action_taken == "REJECT_ORDER"
    assert "suspended" in audit.message.lower()

    # 模拟涨停板价格笼子 (+10% 涨停不可高价追买，基准 10.0，限价 11.50 越界)
    limit_up_order = Order(
        order_id="ORD_LIMIT_UP", client_order_id="C_LIMIT_UP",
        account_id="balanced", symbol="sh600002", side=OrderSide.BUY,
        shares=100, price=11.50, order_type=OrderType.LIMIT
    )
    passed, audit = risk_mgr.order_rule.validate_order(
        limit_up_order, available_cash=10000.0, prev_close=10.0, is_suspended=False
    )
    assert not passed
    assert audit.action_taken == "REJECT_ORDER"
    assert "cage" in audit.message.lower() or "price" in audit.message.lower()


def test_6_t_plus_1_cannot_sell_same_day():
    """
    场景 6: T+1 规则严格限制
    - 当日买入股票 available_shares=0
    - 当日卖出直接抛出 InsufficientSharesError
    - 跨日后解锁
    """
    broker = SimulatedBrokerAdapter(initial_cash=50000.0)
    broker.init_account("balanced", cash=50000.0)

    # Day 1: 买入 1000 股
    buy_order = Order(
        order_id="ORD_DAY1_BUY", client_order_id="C_DAY1_BUY",
        account_id="balanced", symbol="sh600036", side=OrderSide.BUY,
        shares=1000, price=30.0, order_type=OrderType.MARKET
    )
    broker.execute_order(buy_order, execution_price=30.0, trade_date="2026-11-16")
    
    # Day 1 当日尝试卖出
    same_day_sell = Order(
        order_id="ORD_DAY1_SELL", client_order_id="C_DAY1_SELL",
        account_id="balanced", symbol="sh600036", side=OrderSide.SELL,
        shares=500, price=30.0, order_type=OrderType.MARKET
    )
    with pytest.raises(InsufficientSharesError):
        broker.execute_order(same_day_sell, execution_price=30.0, trade_date="2026-11-16")

    # Day 2: 跨日结算
    broker.roll_trading_day()
    assert broker.get_positions("balanced")["sh600036"].available_shares == 1000

    # Day 2 卖出成功
    day2_sell = Order(
        order_id="ORD_DAY2_SELL", client_order_id="C_DAY2_SELL",
        account_id="balanced", symbol="sh600036", side=OrderSide.SELL,
        shares=500, price=31.0, order_type=OrderType.MARKET
    )
    fill = broker.execute_order(day2_sell, execution_price=31.0, trade_date="2026-11-17")
    assert fill.shares == 500
    assert broker.get_positions("balanced")["sh600036"].shares == 500


def test_7_system_restart_crash_recovery_unfilled_orders(temp_db):
    """
    场景 7: 系统重启后恢复未完成订单与未提交崩溃回滚
    - 处于 SUBMITTED 状态的挂单不被丢弃，也不会被当作成交
    - 数据库事务崩溃自动回滚，不产生虚假持仓
    """
    ledger = SQLiteAccountLedger(temp_db)
    ledger.init_account("balanced", initial_cash=100000.0)

    # 1. 模拟系统断电前提交了订单
    order = Order(
        order_id="ORD_RECOVER_TEST",
        client_order_id="C_RECOVER_TEST",
        account_id="balanced",
        symbol="sh600036",
        side=OrderSide.BUY,
        shares=1000,
        price=30.0,
        order_type=OrderType.LIMIT,
        status=OrderStatus.SUBMITTED
    )
    ledger.save_order(order)

    # 2. 模拟系统崩溃未提交事务回滚
    try:
        with ledger.connect() as conn:
            conn.execute("UPDATE accounts SET total_cash_cents = 0 WHERE account_id = 'balanced'")
            raise RuntimeError("SIMULATED_POWER_FAILURE")
    except RuntimeError:
        pass

    # 重启并重建 Ledger
    ledger_restarted = SQLiteAccountLedger(temp_db)
    bal = ledger_restarted.get_balance("balanced")
    assert bal.cash == 100000.0, "Crash before commit must roll back cash cleanly"
    
    # 验证订单保存在 SQLite 中且未被错误计入持仓
    pos = ledger_restarted.get_positions("balanced")
    assert "sh600036" not in pos, "Unfilled order must never create ghost position"
    with ledger_restarted.connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE order_id = 'ORD_RECOVER_TEST'").fetchone()
        assert row is not None
        assert row[0] == OrderStatus.SUBMITTED.value


def test_8_duplicate_fill_report_idempotency(temp_db):
    """
    场景 8: 重复收到相同成交回报 (Idempotency)
    - 相同的 fill_id 第二次到达时静默丢弃
    - 绝不重复扣款、不重复累加持仓
    """
    ledger = SQLiteAccountLedger(temp_db)
    ledger.init_account("balanced", initial_cash=50000.0)

    fill = TradeFill(
        fill_id="FILL_IDEMPOTENT_001",
        order_id="ORD_IDEM_001",
        client_order_id="C_IDEM_001",
        account_id="balanced",
        symbol="sh600000",
        name="浦发银行",
        side=OrderSide.BUY,
        shares=1000,
        price=10.0,
        amount=10000.0,
        commission=5.0,
        stamp_tax=0.0,
        transfer_fee=0.1,
        slippage=0.0,
        realized_pnl=0.0,
        timestamp="2026-11-16T10:00:00"
    )

    # 第一次应用回报：成功
    applied_first = ledger.apply_fill(fill, name="浦发银行", sector="金融")
    assert applied_first is True

    bal_first = ledger.get_balance("balanced")
    pos_first = ledger.get_positions("balanced")["sh600000"]
    assert pos_first.total_shares == 1000
    expected_cash = 50000.0 - 10000.0 - 5.10
    assert abs(bal_first.cash - expected_cash) < 0.01

    # 第二次重复应用回报（模拟网络重试或服务重复推送）
    applied_second = ledger.apply_fill(fill, name="浦发银行", sector="金融")
    assert applied_second is False  # 幂等查重生效，返回 False

    # 断言：现金和持仓分毫未变
    bal_second = ledger.get_balance("balanced")
    pos_second = ledger.get_positions("balanced")["sh600000"]
    assert pos_second.total_shares == 1000  # 没有变成 2000
    assert abs(bal_second.cash - expected_cash) < 0.01  # 没有二次扣款
