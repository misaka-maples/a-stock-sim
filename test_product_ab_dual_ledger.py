"""
Unit tests for Product AB Dual Ledger Architecture, State Isolation, Idempotency & Diagnostics.
"""

import copy
import pytest
from sim_trader import SimAccount
from product_ab_strategy import ProductABHybridStrategy
from ab_diagnostic import build_diagnostic_report, format_diagnostic_markdown


def test_dual_ledger_initialization_and_isolation():
    """验证双账本初始化独立性与物理状态隔离"""
    primary_acc = SimAccount(initial_cash=95424.63)
    primary_acc.cash = 78816.63
    primary_acc.positions = {
        "sh600261": {"symbol": "sh600261", "name": "阳光照明", "total_shares": 1300, "cost_price": 5.21, "market_value": 6916.0},
        "sh603955": {"symbol": "sh603955", "name": "大千生态", "total_shares": 100, "cost_price": 32.07, "market_value": 3420.0},
        "sz000635": {"symbol": "sz000635", "name": "英 力 特", "total_shares": 700, "cost_price": 9.78, "market_value": 6272.0}
    }

    strategy = ProductABHybridStrategy(primary_acc, ["sh600519", "sz000001"])

    # 1. 验证主账户与测试账本资金独立
    assert strategy.account.cash == 78816.63
    assert len(strategy.account.positions) == 3
    assert hasattr(strategy, "sandbox_account")
    assert strategy.sandbox_account.initial_cash == 100000.0
    assert strategy.sandbox_account.cash == 100000.0
    assert len(strategy.sandbox_account.positions) == 0

    # 2. 验证风控状态完全自治隔离
    assert "position_monitors" in strategy.state
    assert "position_monitors" in strategy.sandbox_state
    assert strategy.state["position_monitors"] is not strategy.sandbox_state["position_monitors"]

    # 3. 验证幂等性集合自治隔离
    assert "executed_orders_today" in strategy.state
    assert "executed_orders_today" in strategy.sandbox_state
    assert strategy.state["executed_orders_today"] is not strategy.sandbox_state["executed_orders_today"]


def test_legacy_position_risk_exposure_limit():
    """验证历史持仓市值硬性计入仓位限额与行业配额，防止新仓穿透"""
    primary_acc = SimAccount(initial_cash=100000.0)
    primary_acc.cash = 66000.0
    # 假设旧仓已占 34% (34,000 元)
    primary_acc.positions = {
        "sh600261": {"symbol": "sh600261", "total_shares": 5000, "cost_price": 6.80, "market_value": 34000.0}
    }

    strategy = ProductABHybridStrategy(primary_acc, ["sh600519"])
    # 模拟 NEUTRAL 状态 (max 35%)
    strategy.get_current_regime_and_weights = lambda as_of: ("NEUTRAL", 1.0, 0.0)
    strategy.latest_candidates = [
        {"symbol": "sh600519", "name": "贵州茅台", "score": 95, "industry": "白酒", "volatility": 0.015}
    ]

    strategy._plan(as_of="2026-11-09", execution_day="2026-11-10")

    pending = strategy.state.get("pending_entries", {})
    # 因为总股票已有 34%，NEUTRAL 目标顶格 35%，剩余额度仅 1% (< 2% 最小开仓门槛)
    # 因此严禁新开仓！
    assert len(pending) == 0, "旧仓未退且接近上限时，应严格阻止新仓穿透！"


def test_idempotency_order_guard():
    """验证当日幂等性标识能够拦截重复下单"""
    acc = SimAccount(initial_cash=100000.0)
    strategy = ProductABHybridStrategy(acc, ["sh600519"])

    execution_day = "2026-11-09"
    sym = "sh600519"
    idem_key = f"{execution_day}_{sym}_BUY"

    strategy.sandbox_state["pending_entries"] = {
        sym: {"signal": {"name": "贵州茅台"}, "target_weight": 0.20, "entry_reason": "test"}
    }
    # 标记当日已执行
    strategy.sandbox_state["executed_orders_today"][idem_key] = True

    quotes = {sym: {"price": 1800.0, "open": 1800.0, "pre_close": 1790.0}}
    import datetime as dt
    from trade_calendar import BEIJING
    sim_time = dt.datetime(2026, 11, 9, 9, 35, 0, tzinfo=BEIJING)

    strategy._tick_sandbox(quotes, now=sim_time)

    # 验证未发生实际买入 (持仓仍为0)
    assert sym not in strategy.sandbox_account.positions
    assert len(strategy.sandbox_account.trades) == 0


def test_diagnostic_report_integrity():
    """验证三轨收盘自动诊断报告的完整性与归因计算"""
    primary_acc = SimAccount(initial_cash=95424.63)
    primary_acc.cash = 78816.63
    primary_acc.positions = {
        "sh600261": {"symbol": "sh600261", "name": "阳光照明", "total_shares": 1300, "cost_price": 5.21, "market_value": 6916.0, "unrealized_pnl": 143.0, "pnl_pct": 2.11},
        "sh603955": {"symbol": "sh603955", "name": "大千生态", "total_shares": 100, "cost_price": 32.07, "market_value": 3420.0, "unrealized_pnl": 213.0, "pnl_pct": 6.64},
        "sz000635": {"symbol": "sz000635", "name": "英 力 特", "total_shares": 700, "cost_price": 9.78, "market_value": 6272.0, "unrealized_pnl": -574.0, "pnl_pct": -8.38}
    }
    strategy = ProductABHybridStrategy(primary_acc, ["sh600519"])

    diag = strategy.generate_diagnostic_report(as_of="2026-11-09")
    report = diag["report"]
    md = diag["markdown"]

    assert report["report_date"] == "2026-11-09"
    assert "metrics_table" in report
    assert len(report["metrics_table"]) >= 8

    # 验证四维归因存在
    attr = report["attribution"]
    assert "legacy_drag" in attr
    assert "signal_consistency" in attr
    assert "execution_drag" in attr
    assert "cash_overlay_delta" in attr

    # 验证旧仓市值统计精准
    assert attr["legacy_drag"]["legacy_market_value"] == 16608.0
    assert attr["legacy_drag"]["legacy_weight_pct"] == 17.4

    assert "Product AB 每日收盘自动诊断报告" in md
