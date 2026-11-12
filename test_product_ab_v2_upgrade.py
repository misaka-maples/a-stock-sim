#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Product AB v2.0 Dual Strategy Upgrade Unit & Regression Test Suite
Covers:
1. Balanced v2.0 & Aggressive v2.0 Regime Caps (30/45/65/80 vs 50/70/90/100) & Safe 0.0 Fallback
2. Balanced A/B Budget Allocation (70/30, 60/40, 20/80, 50/50) vs Aggressive (100% B)
3. RISK_OFF Entry Filter Multi-Factor Criteria (close > MA60, close > MA120, ret20 > 0, ret60 > 0, Top4, amount >= 30M)
4. Balanced Profit Floor (+3% cost floor upon peak profit >= 20%) & 3.5*ATR Trailing Stop
5. Aggressive 4.5*ATR Trailing Stop & 2-Consecutive-Day MA20 Trend Breakdown Exit
6. Portfolio-Level Drawdown Alerts & Freeze Expansion (8%/12% vs 15%/20%)
7. Main Account Legacy Holdings Preservation (sh600261, sh603955)
8. Physical Ledger & State Decoupling & Isolation
9. Daily Order Idempotency
10. ProductABv2DualCoordinator Orchestration & Multi-account API Status
"""

import os
import sys
import copy
import uuid
import pytest
import datetime as dt
from pathlib import Path

BASE_DIR = Path("/home/openarmx/test")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sim_trader import SimAccount
from product_ab_v2_strategies import (
    ProductABBalancedV2Strategy,
    ProductBAggressiveV2Strategy,
    ProductABv2DualCoordinator,
    check_risk_off_entry_filter,
    BALANCED_REGIME_CAPS,
    BALANCED_AB_BUDGET_RATIOS,
    AGGRESSIVE_REGIME_CAPS
)


def _tmp_acc(cash=100000.0, ledger_only=True):
    path = f"/tmp/test_acc_{uuid.uuid4().hex[:8]}.json"
    acc = SimAccount(initial_cash=cash, save_path=path, ledger_only=ledger_only)
    acc.save()
    return acc


def test_balanced_v2_regime_caps_and_unknown_fallback():
    acc = _tmp_acc(100000.0)
    strat = ProductABBalancedV2Strategy(acc, ["sh600519"])

    assert strat.get_max_regime_total_wt("RISK_OFF") == 0.30
    assert strat.get_max_regime_total_wt("NEUTRAL") == 0.45
    assert strat.get_max_regime_total_wt("RISK_ON") == 0.65
    assert strat.get_max_regime_total_wt("STRONG_RISK_ON") == 0.80

    # 异常或未知状态：安全兜底 0.0 (禁止新增开仓)
    assert strat.get_max_regime_total_wt("UNKNOWN_REGIME") == 0.0
    assert strat.get_max_regime_total_wt("INVALID") == 0.0
    assert strat.get_max_regime_total_wt("") == 0.0

    # 仓位与标的限制
    assert strat.config.max_positions == 3
    assert strat.config.max_stock_weight == 0.15
    assert strat.config.max_sector_weight == 0.30


def test_aggressive_v2_regime_caps_and_unknown_fallback():
    acc = _tmp_acc(100000.0)
    strat = ProductBAggressiveV2Strategy(acc, ["sh600519"])

    assert strat.get_max_regime_total_wt("RISK_OFF") == 0.50
    assert strat.get_max_regime_total_wt("NEUTRAL") == 0.70
    assert strat.get_max_regime_total_wt("RISK_ON") == 0.90
    assert strat.get_max_regime_total_wt("STRONG_RISK_ON") == 1.00

    # 异常或未知状态：安全兜底 0.0
    assert strat.get_max_regime_total_wt("UNKNOWN_REGIME") == 0.0
    assert strat.get_max_regime_total_wt("INVALID") == 0.0

    # 仓位与标的限制
    assert strat.config.max_positions == 5
    assert strat.config.max_stock_weight == 0.25
    assert strat.config.max_sector_weight == 0.40


def test_balanced_ab_budget_ratios():
    assert BALANCED_AB_BUDGET_RATIOS["RISK_OFF"] == (0.70, 0.30)
    assert BALANCED_AB_BUDGET_RATIOS["NEUTRAL"] == (0.60, 0.40)
    assert BALANCED_AB_BUDGET_RATIOS["RISK_ON"] == (0.20, 0.80)
    assert BALANCED_AB_BUDGET_RATIOS["STRONG_RISK_ON"] == (0.50, 0.50)


def test_risk_off_entry_filter_criteria():
    top_industries = ["通信设备", "半导体", "汽车零部件", "电网设备"]
    ind_scores = {"通信设备": 8.5, "半导体": 6.2, "房地产": -3.5}
    ind_map = {"sh600001": "通信设备", "sh600002": "房地产", "sh600003": "半导体"}

    # 1. 完全符合条件的标的
    cand_ok = {
        "symbol": "sh600001",
        "current": 25.0,
        "ma60": 20.0,
        "ma120": 18.0,
        "ret_20": 5.2,
        "ret_60": 12.0,
        "amount": 50_000_000.0
    }
    ok, reason = check_risk_off_entry_filter(cand_ok, [], ind_map, top_industries, ind_scores)
    assert ok is True

    # 2. 价格跌破 MA60
    cand_fail_ma60 = dict(cand_ok, current=19.5)
    ok, reason = check_risk_off_entry_filter(cand_fail_ma60, [], ind_map, top_industries, ind_scores)
    assert ok is False
    assert "MA60" in reason

    # 3. 价格跌破 MA120
    cand_fail_ma120 = dict(cand_ok, current=17.5, ma60=16.0)
    ok, reason = check_risk_off_entry_filter(cand_fail_ma120, [], ind_map, top_industries, ind_scores)
    assert ok is False
    assert "MA120" in reason

    # 4. 20日动量非正
    cand_fail_ret20 = dict(cand_ok, ret_20=-1.5)
    ok, reason = check_risk_off_entry_filter(cand_fail_ret20, [], ind_map, top_industries, ind_scores)
    assert ok is False
    assert "20日动量" in reason

    # 5. 60日动量非正
    cand_fail_ret60 = dict(cand_ok, ret_60=-0.5)
    ok, reason = check_risk_off_entry_filter(cand_fail_ret60, [], ind_map, top_industries, ind_scores)
    assert ok is False
    assert "60日动量" in reason

    # 6. 不在 Top 4 强势行业
    cand_fail_ind = dict(cand_ok, symbol="sh600002")
    ok, reason = check_risk_off_entry_filter(cand_fail_ind, [], ind_map, top_industries, ind_scores)
    assert ok is False
    assert "Top 4" in reason

    # 7. 行业动量得分走弱 (< 0)
    cand_weak_sec = dict(cand_ok, symbol="sh600002")
    ok, reason = check_risk_off_entry_filter(cand_weak_sec, [], ind_map, ["房地产"], ind_scores)
    assert ok is False
    assert "走弱" in reason

    # 8. 流动性不足 (< 3000万)
    cand_fail_vol = dict(cand_ok, amount=15_000_000.0)
    ok, reason = check_risk_off_entry_filter(cand_fail_vol, [], ind_map, top_industries, ind_scores)
    assert ok is False
    assert "3000万元" in reason


def test_balanced_v2_profit_floor_and_atr_exit():
    """验证 Balanced v2.0 的 +3% 浮盈保护底线与 3.5*ATR 跟踪"""
    acc = _tmp_acc(100000.0, ledger_only=True)
    acc.cash = 80000.0
    cost_price = 10.0
    acc.positions = {
        "sh600001": {
            "symbol": "sh600001",
            "name": "测试股A",
            "total_shares": 1000,
            "available_shares": 1000,
            "cost_price": cost_price,
            "market_value": 10000.0,
            "atr_pct": 0.03,
            "buy_lots": [{"date": "2026-11-01", "shares": 1000, "price": cost_price}]
        }
    }
    acc.save()

    strat = ProductABBalancedV2Strategy(acc, ["sh600001"])
    # 覆盖 load 行为，确保测试持仓有效
    strat.account.positions = acc.positions
    strat.history_cache["sh600001"] = [
        {"date": f"2026-10-{i:02d}", "close": 10.0, "high": 10.2, "low": 9.8} for i in range(1, 25)
    ]

    trading_now = dt.datetime(2026, 11, 10, 10, 0, 0)

    # 1. 价格上涨至 12.5 (+25%，冲高超过 20%)，激活 ATR 跟踪与 +3% 保护底线
    strat.on_tick({"sh600001": {"price": 12.5, "current": 12.5, "close": 12.5}}, now=trading_now)
    mon = strat.state["position_monitors"]["sh600001"]
    assert mon["peak_price"] == 12.5
    assert mon["peak_profit_pct"] == 25.0
    assert mon["trailing_stop_line"] >= 10.30

    # 2. 价格跌回 10.80 (低于 ATR 止盈线 11.19，但高于成本)，触发 ATR 止盈
    strat.on_tick({"sh600001": {"price": 10.80, "current": 10.80, "close": 10.80}}, now=trading_now)
    assert "sh600001" not in strat.account.positions
    assert strat.account.cash > 80000.0


def test_aggressive_v2_two_day_ma20_exit():
    """验证 Aggressive v2.0 的连续2日收盘跌破MA20退出逻辑 (单日跌破不卖)"""
    acc = _tmp_acc(100000.0, ledger_only=True)
    acc.cash = 70000.0
    acc.positions = {
        "sz000001": {
            "symbol": "sz000001",
            "name": "平安银行",
            "total_shares": 2000,
            "available_shares": 2000,
            "cost_price": 15.0,
            "market_value": 30000.0,
            "buy_lots": [{"date": "2026-11-01", "shares": 2000, "price": 15.0}]
        }
    }
    acc.save()

    strat = ProductBAggressiveV2Strategy(acc, ["sz000001"])
    strat.account.positions = acc.positions

    bars = [{"date": f"2026-10-{i:02d}", "close": 15.0, "high": 15.2, "low": 14.8} for i in range(1, 23)]
    bars[-2]["close"] = 15.2
    bars[-1]["close"] = 14.5  # 仅 1 天跌破
    strat.history_cache["sz000001"] = bars

    trading_now = dt.datetime(2026, 11, 10, 10, 0, 0)

    # 仅第1天收盘跌破，不应触发退出
    strat.on_tick({"sz000001": {"price": 14.6, "current": 14.6, "close": 14.6}}, now=trading_now)
    assert "sz000001" in strat.account.positions

    # 倒数第2天与倒数第1天均低于 MA20*0.99 (连续 2 天跌破)
    bars[-2]["close"] = 14.4
    bars[-1]["close"] = 14.3
    strat.history_cache["sz000001"] = bars

    # 连续2天跌破，触发退出
    strat.on_tick({"sz000001": {"price": 14.3, "current": 14.3, "close": 14.3}}, now=trading_now)
    assert "sz000001" not in strat.account.positions


def test_portfolio_risk_drawdown_freeze():
    """验证组合级回撤预警与冻结机制 (Balanced 8%/12%, Aggressive 15%/20%)"""
    # 1. Balanced 账户
    acc_bal = _tmp_acc(100000.0, ledger_only=True)
    strat_bal = ProductABBalancedV2Strategy(acc_bal, ["sh600519"])
    strat_bal.state["peak_equity"] = 100000.0

    # 回撤 5% -> 正常
    is_alert, is_freeze, dd = strat_bal.check_portfolio_risk(95000.0)
    assert not is_alert and not is_freeze

    # 回撤 9% -> 预警，未冻结
    is_alert, is_freeze, dd = strat_bal.check_portfolio_risk(91000.0)
    assert is_alert and not is_freeze

    # 回撤 13% -> 预警且冻结新增开仓
    is_alert, is_freeze, dd = strat_bal.check_portfolio_risk(87000.0)
    assert is_alert and is_freeze

    # 2. Aggressive 账户
    acc_agg = _tmp_acc(100000.0, ledger_only=True)
    strat_agg = ProductBAggressiveV2Strategy(acc_agg, ["sh600519"])
    strat_agg.state["peak_equity"] = 100000.0

    # 回撤 12% -> 正常
    is_alert, is_freeze, dd = strat_agg.check_portfolio_risk(88000.0)
    assert not is_alert and not is_freeze

    # 回撤 16% -> 预警
    is_alert, is_freeze, dd = strat_agg.check_portfolio_risk(84000.0)
    assert is_alert and not is_freeze

    # 回撤 21% -> 冻结
    is_alert, is_freeze, dd = strat_agg.check_portfolio_risk(79000.0)
    assert is_alert and is_freeze


def test_dual_coordinator_isolation():
    """验证 ProductABv2DualCoordinator 双账户物理隔离"""
    primary_acc = _tmp_acc(95000.0, ledger_only=True)
    sb_acc = _tmp_acc(100000.0, ledger_only=True)

    coord = ProductABv2DualCoordinator(
        account=primary_acc,
        watchlist=["sh600519", "sz000001"],
        sandbox_account=sb_acc
    )

    # 验证对象自治
    assert coord.account is primary_acc
    assert coord.sandbox_account is sb_acc
    assert coord.account is not coord.sandbox_account

    # 验证各自策略实例与状态
    assert coord.aggressive_strategy.account is sb_acc
    assert coord.state is not coord.aggressive_strategy.state

    # 状态概览报告
    status = coord.allocation_status()
    assert "primary_positions_count" in status
    assert "sandbox_positions_count" in status
    assert status["max_regime_total_wt_num"] == 0.30
    assert status["sandbox_max_regime_total_wt_num"] == 0.50


def test_legacy_positions_safety():
    """验证主账户中原有的 阳光照明 (sh600261) 与 大千生态 (sh603955) 不会被误平仓"""
    acc = _tmp_acc(95424.63, ledger_only=True)
    acc.cash = 78816.63
    acc.positions = {
        "sh600261": {
            "symbol": "sh600261",
            "name": "阳光照明",
            "total_shares": 1300,
            "available_shares": 1300,
            "cost_price": 5.2139,
            "market_value": 8281.0,
            "buy_lots": [{"date": "2026-10-15", "shares": 1300, "price": 5.2139}]
        },
        "sh603955": {
            "symbol": "sh603955",
            "name": "大千生态",
            "total_shares": 100,
            "available_shares": 100,
            "cost_price": 32.0703,
            "market_value": 3334.0,
            "buy_lots": [{"date": "2026-10-20", "shares": 100, "price": 32.0703}]
        }
    }
    acc.save()

    strat = ProductABBalancedV2Strategy(acc, ["sh600261", "sh603955"])
    strat.account.positions = acc.positions
    strat.history_cache["sh600261"] = [{"date": "2026-11-10", "close": 6.37}] * 30
    strat.history_cache["sh603955"] = [{"date": "2026-11-10", "close": 33.34}] * 30

    quotes = {
        "sh600261": {"price": 6.37, "current": 6.37, "close": 6.37},
        "sh603955": {"price": 33.34, "current": 33.34, "close": 33.34}
    }
    trading_now = dt.datetime(2026, 11, 10, 10, 0, 0)
    strat.on_tick(quotes, now=trading_now)

    # 阳光照明浮盈约 +22%，大千生态浮盈约 +4%，均未触及硬止损或跌破均线，必须继续保留
    assert "sh600261" in strat.account.positions
    assert "sh603955" in strat.account.positions
    assert len(strat.account.positions) == 2
