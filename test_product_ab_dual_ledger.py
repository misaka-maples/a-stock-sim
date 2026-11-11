"""
Unit tests for Product AB Dual Ledger Architecture, State Isolation, Idempotency & Diagnostics.
"""

import copy
import pytest
from sim_trader import SimAccount
from product_ab_strategy import ProductABHybridStrategy, FROZEN_AB_WEIGHTS
from ab_diagnostic import build_diagnostic_report, format_diagnostic_markdown
from notifier import NotificationManager


def test_dual_ledger_initialization_and_isolation():
    """验证双账本初始化独立性与物理状态隔离"""
    primary_acc = SimAccount(initial_cash=95424.63)
    primary_acc.cash = 78816.63
    primary_acc.positions = {
        "sh600261": {"symbol": "sh600261", "name": "阳光照明", "total_shares": 1300, "cost_price": 5.21, "market_value": 6916.0},
        "sh603955": {"symbol": "sh603955", "name": "大千生态", "total_shares": 100, "cost_price": 32.07, "market_value": 3420.0},
        "sz000635": {"symbol": "sz000635", "name": "英 力 特", "total_shares": 700, "cost_price": 9.78, "market_value": 6272.0}
    }

    strategy = ProductABHybridStrategy(primary_acc, ["sh600519", "sz000001"],
                                       sandbox_account=SimAccount(initial_cash=100000.0, save_path="/tmp/test_sandbox_ab_isolated.json"))

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

    strategy = ProductABHybridStrategy(primary_acc, ["sh600519"],
                                       sandbox_account=SimAccount(initial_cash=100000.0, save_path="/tmp/test_sb_legacy.json"))
    # 模拟 NEUTRAL 状态 (max 35%)
    strategy.get_current_regime_and_weights = lambda as_of: ("NEUTRAL", 1.0, 0.0)
    strategy.latest_candidates = [
        {"symbol": "sh600519", "name": "贵州茅台", "score": 95, "industry": "白酒", "volatility": 0.015}
    ]

    strategy._plan(as_of="2026-11-09", execution_day="2026-11-10")

    pending = strategy.state.get("pending_entries", {})
    # 由于旧仓已占 34%，剩余配额仅 1% (< 2% 开仓门槛)，禁止开出茅台新订单
    assert len(pending) == 0, "旧仓未完全退出前，剩余配额不足，不得超额建立新仓"


def test_dual_ledger_idempotency_barrier():
    """验证同一交易日内订单执行幂等性，严防重复撮合与双重扣款"""
    primary_acc = SimAccount(initial_cash=100000.0)
    strategy = ProductABHybridStrategy(primary_acc, ["sh600519"],
                                       sandbox_account=SimAccount(initial_cash=100000.0, save_path="/tmp/test_sb_idem.json"))

    # 设置当日已执行标记
    strategy.state["executed_orders_today"] = {"2026-11-09_sh600519_BUY": True}
    strategy.sandbox_state["executed_orders_today"] = {"2026-11-09_sh600519_BUY": True}

    # 尝试再次撮合
    quotes = {"sh600519": {"symbol": "sh600519", "current": 1500.0, "price": 1500.0}}

    strategy._tick_sandbox(quotes)
    # Sandbox 未挂单，持仓数应保持为 0
    assert len(strategy.sandbox_account.positions) == 0


def test_diagnostic_report_integrity():
    """验证每日三轨收盘自动诊断报告的生成完备性与四维归因拆解"""
    primary_acc = SimAccount(initial_cash=95424.63)
    primary_acc.cash = 78816.63
    primary_acc.positions = {
        "sh600261": {"symbol": "sh600261", "name": "阳光照明", "total_shares": 1300, "cost_price": 5.21, "market_value": 6916.0, "unrealized_pnl": 143.0, "pnl_pct": 2.11},
        "sh603955": {"symbol": "sh603955", "name": "大千生态", "total_shares": 100, "cost_price": 32.07, "market_value": 3420.0, "unrealized_pnl": 213.0, "pnl_pct": 6.64},
        "sz000635": {"symbol": "sz000635", "name": "英 力 特", "total_shares": 700, "cost_price": 9.78, "market_value": 6272.0, "unrealized_pnl": -574.0, "pnl_pct": -8.38}
    }

    strategy = ProductABHybridStrategy(primary_acc, ["sh600519"],
                                       sandbox_account=SimAccount(initial_cash=100000.0, save_path="/tmp/test_sb_diag.json"))
    strategy.state["shadow_ledger"] = {
        "nav_a": 1.0025,
        "nav_b": 1.0068,
        "nav_ab_theoretical": 1.0042,
        "nav_actual": 1.0005,
        "tracking_error_pct": -0.37,
        "current_regime": "NEUTRAL",
        "weight_a": 1.0,
        "weight_b": 0.0,
        "target_holdings_a": {"sh600519": 0.35},
        "target_holdings_b": {},
        "last_settle_date": "2026-11-09",
        "history": []
    }

    report = build_diagnostic_report(strategy, as_of="2026-11-09")
    md = format_diagnostic_markdown(report)

    # 1. 结构完整性校验
    assert report["report_date"] == "2026-11-09"
    assert "primary_account" in report
    assert "sandbox_account" in report
    assert "theoretical_benchmark" in report
    assert "attribution" in report

    # 2. 四维归因完整性校验
    attr = report["attribution"]
    assert "legacy_drag" in attr
    assert "signal_consistency" in attr
    assert "execution_drag" in attr
    assert "cash_overlay_delta" in attr

    # 验证旧仓市值统计精准
    assert attr["legacy_drag"]["legacy_market_value"] == 16608.0
    assert attr["legacy_drag"]["legacy_weight_pct"] == 17.4

    assert "Product AB 每日收盘自动诊断报告" in md


def test_risk_off_no_new_buy_orders():
    """验证 RISK_OFF 弱市状态下新开仓上限严格为 0%，绝不生成任何新增买入订单"""
    primary_acc = SimAccount(initial_cash=95424.63, save_path="/tmp/test_p_risk_off.json")
    primary_acc.cash = 85122.41
    primary_acc.positions = {
        "sh600261": {"symbol": "sh600261", "name": "阳光照明", "total_shares": 1300, "cost_price": 5.21, "market_value": 6916.0},
        "sh603955": {"symbol": "sh603955", "name": "大千生态", "total_shares": 100, "cost_price": 32.07, "market_value": 3420.0}
    }
    strategy = ProductABHybridStrategy(primary_acc, ["sh600519", "sz002086", "sh605488"],
                                       sandbox_account=SimAccount(initial_cash=100000.0, save_path="/tmp/test_sb_risk_off.json"))
    cands = [
        {"symbol": "sz002086", "name": "中航电测", "score": 92.5, "signal_price": 65.0, "atr_pct": 0.04, "volatility": 0.02},
        {"symbol": "sh605488", "name": "福莱新材", "score": 88.0, "signal_price": 18.5, "atr_pct": 0.05, "volatility": 0.025},
        {"symbol": "sz002242", "name": "九阳股份", "score": 85.0, "signal_price": 12.3, "atr_pct": 0.035, "volatility": 0.018}
    ]
    strategy._ranked_signals = lambda as_of: cands
    strategy.get_current_regime_and_weights = lambda as_of: ("RISK_OFF", 1.0, 0.0)

    strategy._plan(as_of="2026-11-11", execution_day="2026-11-12")

    pending = strategy.state.get("pending_entries", {})
    assert len(pending) == 0, f"RISK_OFF 下禁止任何新增股票买入，实际得到 {len(pending)}"

    sb_pending = strategy.sandbox_state.get("pending_entries", {})
    assert len(sb_pending) == 0, f"RISK_OFF 下 Sandbox 同样禁止买入，实际得到 {len(sb_pending)}"

    alloc = strategy.allocation_status()
    assert alloc["allow_new_entries"] is False
    assert alloc["pending_entry_count"] == 0
    assert alloc["refill_enabled"] is False
    assert alloc["can_refill"] is False
    assert "0.0%" in alloc["no_entry_reason"]
    assert "RISK_OFF" in alloc["no_entry_reason"]


def test_two_positions_does_not_force_refill_third():
    """验证当主账户仅有 2 只持仓时，max_positions=3 代表最多持有 3 只，绝不为了凑齐 3 只而主动强制买入"""
    primary_acc = SimAccount(initial_cash=96902.41, save_path="/tmp/test_p_two_pos.json")
    primary_acc.cash = 85122.41
    primary_acc.positions = {
        "sh600261": {"symbol": "sh600261", "name": "阳光照明", "total_shares": 1300, "cost_price": 5.21, "market_value": 6916.0},
        "sh603955": {"symbol": "sh603955", "name": "大千生态", "total_shares": 100, "cost_price": 32.07, "market_value": 3420.0}
    }
    strategy = ProductABHybridStrategy(primary_acc, ["sh600519", "sz002086", "sh605488"],
                                       sandbox_account=SimAccount(initial_cash=100000.0, save_path="/tmp/test_sb_two_pos.json"))
    # 非调仓日 (距离上次调仓未达到周期)
    strategy.state["last_rebalance"] = "2026-11-11"
    strategy.state["rebalance_as_of"] = "2026-11-11"
    cands = [
        {"symbol": "sz002086", "name": "中航电测", "score": 92.5, "signal_price": 65.0, "atr_pct": 0.04, "volatility": 0.02}
    ]
    strategy._ranked_signals = lambda as_of: cands
    strategy.get_current_regime_and_weights = lambda as_of: ("NEUTRAL", 1.0, 0.0)

    # 处于非调仓日，且 refill_vacancies 冻结为 False
    strategy._plan(as_of="2026-11-11", execution_day="2026-11-12")

    alloc = strategy.allocation_status()
    assert alloc["vacant_slots"] == 1
    assert alloc["primary_positions_count"] == 2
    assert alloc["primary_max_positions"] == 3
    assert alloc["refill_enabled"] is False
    assert alloc["can_refill"] is False
    assert "不强制补齐" in alloc["wait_reason"]


def test_sandbox_empty_does_not_force_buy_three_stocks_in_risk_off():
    """验证 Sandbox 在空仓状态下，处于 RISK_OFF 弱市时严格保持空仓现金防御，绝不强制建仓买入 3 只股票"""
    primary_acc = SimAccount(initial_cash=100000.0, save_path="/tmp/test_p_empty.json")
    sb_acc = SimAccount(initial_cash=100000.0, save_path="/tmp/test_sb_empty.json")
    strategy = ProductABHybridStrategy(primary_acc, ["sh600519", "sz002086", "sh605488"],
                                       sandbox_account=sb_acc)
    cands = [
        {"symbol": "sz002086", "name": "中航电测", "score": 92.5, "signal_price": 65.0, "atr_pct": 0.04, "volatility": 0.02},
        {"symbol": "sh605488", "name": "福莱新材", "score": 88.0, "signal_price": 18.5, "atr_pct": 0.05, "volatility": 0.025},
        {"symbol": "sz002242", "name": "九阳股份", "score": 85.0, "signal_price": 12.3, "atr_pct": 0.035, "volatility": 0.018}
    ]
    strategy._ranked_signals = lambda as_of: cands
    strategy.get_current_regime_and_weights = lambda as_of: ("RISK_OFF", 1.0, 0.0)

    strategy._plan(as_of="2026-11-11", execution_day="2026-11-12")

    sb_pending = strategy.sandbox_state.get("pending_entries", {})
    assert len(sb_pending) == 0, f"Sandbox 在 RISK_OFF 下必须保持空仓防御，实际得到 {len(sb_pending)}"
    assert len(strategy.sandbox_account.positions) == 0


def test_market_regime_capital_constraints():
    """验证 Product AB v1.0 各市场状态下的仓位上限与资金分配约束严格一致"""
    primary_acc = SimAccount(initial_cash=100000.0, save_path="/tmp/test_regime_cap.json")
    strategy = ProductABHybridStrategy(primary_acc, ["sh600519"],
                                       sandbox_account=SimAccount(initial_cash=100000.0, save_path="/tmp/test_sb_regime_cap.json"))

    # 1. 验证冻结 A/B 资金权重映射
    assert FROZEN_AB_WEIGHTS["RISK_OFF"] == (1.00, 0.00)
    assert FROZEN_AB_WEIGHTS["NEUTRAL"] == (1.00, 0.00)
    assert FROZEN_AB_WEIGHTS["RISK_ON"] == (0.20, 0.80)
    assert FROZEN_AB_WEIGHTS["STRONG_RISK_ON"] == (0.50, 0.50)

    # 2. 验证各状态总仓位硬顶上限
    regime_limits = {
        "RISK_OFF": 0.0,
        "NEUTRAL": 0.35,
        "RISK_ON": 0.70,
        "STRONG_RISK_ON": 0.90
    }
    for reg, expected_limit in regime_limits.items():
        strategy.get_current_regime_and_weights = lambda as_of, r=reg: (r, FROZEN_AB_WEIGHTS[r][0], FROZEN_AB_WEIGHTS[r][1])
        alloc = strategy.allocation_status()
        assert alloc["max_regime_total_wt_num"] == expected_limit
        if reg == "RISK_OFF":
            assert alloc["allow_new_entries"] is False


def test_existing_positions_risk_management_continues():
    """验证已有持仓的 ATR 追踪止损与 MA20 纪律守护正常运行，不因禁止新开仓而直接无条件清仓"""
    primary_acc = SimAccount(initial_cash=95424.63, save_path="/tmp/test_risk_mgmt.json")
    primary_acc.positions = {
        "sh600261": {
            "symbol": "sh600261", "name": "阳光照明", "total_shares": 1300,
            "cost_price": 5.21, "market_value": 8125.0,
            "buy_lots": [{"date": "2026-11-05", "shares": 1300, "price": 5.21}]
        }
    }
    strategy = ProductABHybridStrategy(primary_acc, ["sh600261"],
                                       sandbox_account=SimAccount(initial_cash=100000.0, save_path="/tmp/test_sb_risk_mgmt.json"))

    # 模拟正常 K 线行情 (价格高于止损线且高于 MA20)
    strategy.history_cache = {
        "sh600261": [
            {"date": "2026-11-10", "close": 6.20, "raw_close": 6.20, "open": 6.10, "high": 6.30, "low": 6.00, "volume": 1000, "adjustment_factor": 1.0},
            {"date": "2026-11-11", "close": 6.25, "raw_close": 6.25, "open": 6.20, "high": 6.35, "low": 6.15, "volume": 1000, "adjustment_factor": 1.0}
        ]
    }
    strategy.get_current_regime_and_weights = lambda as_of: ("RISK_OFF", 1.0, 0.0)

    # 执行 plan: 未触发止损时，持仓不生成 pending_exits，绝不无条件清仓
    strategy._plan(as_of="2026-11-11", execution_day="2026-11-12")
    exits = strategy.state.get("pending_exits", {})
    assert "sh600261" not in exits, "未破止损线与MA20的股票不应被无故清仓"
    assert "sh600261" in strategy.account.positions, "持仓继续持有"


def test_wechat_notification_explains_no_open_reason():
    """验证微信收盘推送准确包含持仓配额、未开仓具体原因、待执行订单数与双账本收益指标"""
    summary = {
        "total_equity": 95788.63,
        "cash": 85452.63,
        "market_value": 10336.0,
        "today_pnl": 212.0,
        "today_pnl_pct": 0.22,
        "total_pnl": 364.0,
        "total_pnl_pct": 0.38
    }
    positions = {
        "sh600261": {"name": "阳光照明", "total_shares": 1300, "cost_price": 5.21, "last_price": 6.25, "unrealized_pnl": 1352.0, "pnl_pct": 19.96},
        "sh603955": {"name": "大千生态", "total_shares": 100, "cost_price": 32.07, "last_price": 34.20, "unrealized_pnl": 213.0, "pnl_pct": 6.64}
    }
    sb_summary = {
        "total_equity": 100000.0,
        "initial_cash": 100000.0,
        "cash": 100000.0,
        "market_value": 0.0,
        "today_pnl": 0.0,
        "today_pnl_pct": 0.0
    }
    strat_status = {
        "strategy_id": "Product_AB",
        "strategy_display_name": "Product AB 自适应混动系统",
        "regime": "RISK_OFF",
        "weight_a": 1.0,
        "weight_b": 0.0,
        "allow_new_entries": False,
        "no_entry_reason": "当前市场处于 RISK_OFF 弱市状态，新买入上限为 0.0%，全额现金防御；绝不为了凑齐3只股票而主动买入",
        "wait_reason": "当前处于 RISK_OFF 弱市状态，股票仓位上限为 0%，严格禁止任何新增开仓；已有持仓正常执行风控，空位保留现金防御",
        "primary_positions_count": 2,
        "primary_max_positions": 3,
        "sandbox_positions_count": 0,
        "sandbox_max_positions": 3,
        "pending_entries": {},
        "pending_exits": {},
        "service_status": "正常运行 (openarmx-quant.service)",
        "data_status_str": "正常 (3,185 只股票增量更新已就绪)",
        "order_execution_status": "正常 (0 笔待执行买单)"
    }

    notifier = NotificationManager()
    captured_messages = []
    notifier.dispatch_message = lambda title, content, summary: captured_messages.append({"title": title, "content": content, "summary": summary})

    notifier.notify_daily_summary(
        summary=summary,
        positions=positions,
        force=True,
        date_str="2026-11-11",
        sandbox_summary=sb_summary,
        sandbox_positions={},
        strategy_status=strat_status
    )

    assert len(captured_messages) == 1
    content = captured_messages[0]["content"]

    # 验证关键指标与说明字段均完整呈现在推送内容中
    assert "2 只" in content and "3 只" in content
    assert "Sandbox" in content
    assert "允许新开仓" in content
    assert "RISK_OFF 弱市状态，新买入上限为 0.0%" in content
    assert "待执行买单" in content
    assert "A底盘蓝筹" in content and "100%" in content
    assert "接管以来" in content
    assert "系统服务状态" in content or "系统运行与服务健康监控" in content
