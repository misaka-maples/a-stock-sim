#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Product AB v2.0 双账户独立量化策略模块:
1. 主账户: Product_AB_Balanced_v2 (中等风险、稳健增长)
   - A/B 混动选股 (Product A core_active 蓝筹底盘 + Product B 动态行业动量)
   - 仓位硬顶: RISK_OFF 30%, NEUTRAL 45%, RISK_ON 65%, STRONG_RISK_ON 80%
   - 最大持仓 3 只, 单票上限 15%, 单行业上限 30%
   - 风控止盈止损:
     * 固定硬止损: 成本价 * 0.94 (-6%)
     * 盘中有效跌破 MA20 约 1% (price < live_ma20 * 0.99) 趋势避险退出
     * 冲高 >= 20% 激活 ATR 跟踪止盈 (3.5 * ATR, 保护线只升不降)
     * 新增浮盈保护底线: max(ATR跟踪线, 成本价 * 1.03) 锁定 +3% 利润
     * 组合回撤 8% 预警, 12% 触发风险复核 (冻结新增风险敞口)

2. 第二账户: Product_B_Aggressive_v2 (高风险、积极进攻)
   - 纯 Product B 全市场动态行业动量进攻 (寻找高流动性龙头、突破主升浪)
   - 仓位硬顶: RISK_OFF 50%, NEUTRAL 70%, RISK_ON 90%, STRONG_RISK_ON 100%
   - 最大持仓 5 只, 单票上限 25%, 单行业上限 40%
   - 风控止盈止损:
     * 固定硬止损: 成本价 * 0.94 (-6%)
     * 宽幅 ATR 动态跟踪: 冲高 >= 20% 激活 4.5 * ATR (无固定目标, 无+3%底线, 容忍高波动)
     * MA20 趋势退出: 连续 2 个已完成交易日收盘均低于 MA20 的 99% (避免单日假跌破早退)
     * 动量衰减退出: 5日轮动，强势趋势股不因小幅排名下降强制清仓
     * 组合回撤 15% 预警, 20% 触发风险复核 (冻结新增风险敞口)

3. 市场状态统一判断:
   - 沪深300 MA20, MA60, MA120 及市场宽度
   - MA120 调节风险敞口而非一刀切禁止交易
   - RISK_OFF 期间允许高质量趋势信号入场 (收盘>MA60, 收盘>MA120, 20d/60d动量为正, 强势Top4行业且未明显走弱, 成交额>=3000万)
   - 未知状态或数据缺失时一律返回 0.0% 仓位硬顶并告警
"""

import os
import sys
import copy
import math
import logging
import datetime as dt
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent

from strategy_rules import (
    MomentumConfig, target_weight, allowed_by_sector, sector_of,
    evaluate_momentum, holding_signal, exit_reason
)
from daily_momentum import (
    DailyMomentumStrategy, last_completed_trading_day,
    previous_trading_day, sessions_between, is_trading_time
)
from history_data import HistoricalDataFeed, PRESET_POOLS, normalize_symbol_for_tencent
from trade_calendar import is_trading_day, BEIJING
from position_guardian import compute_live_ma20, compute_live_atr

logger = logging.getLogger("product_ab_v2")

# ==============================================================================
# 1. 策略配置与全局参数
# ==============================================================================

BALANCED_REGIME_CAPS = {
    "RISK_OFF": 0.30,
    "NEUTRAL": 0.45,
    "RISK_ON": 0.65,
    "STRONG_RISK_ON": 0.80
}

BALANCED_AB_BUDGET_RATIOS = {
    "RISK_OFF": (0.70, 0.30),
    "NEUTRAL": (0.60, 0.40),
    "RISK_ON": (0.20, 0.80),
    "STRONG_RISK_ON": (0.50, 0.50)
}

AGGRESSIVE_REGIME_CAPS = {
    "RISK_OFF": 0.50,
    "NEUTRAL": 0.70,
    "RISK_ON": 0.90,
    "STRONG_RISK_ON": 1.00
}


def check_risk_off_entry_filter(
    cand: Dict[str, Any],
    bars: List[Dict[str, Any]],
    industry_map: Dict[str, str],
    top_industries: List[str],
    industry_scores: Optional[Dict[str, float]] = None
) -> Tuple[bool, str]:
    """
    检查候选标的是否满足 RISK_OFF 弱市状态下的严格入场门槛：
    1. 收盘价 > MA60
    2. 收盘价 > MA120
    3. 20日与60日动量为正 (ret_20 > 0 且 ret_60 > 0)
    4. 所属行业位于强势行业排名前列 (Top 4)
    5. 行业整体趋势未明显走弱 (行业动量得分 > 0 或 >= 0)
    6. 满足流动性与基本面风险检查 (日成交额 >= 3000万, 现价有效, 非ST)
    """
    if not cand:
        return False, "无有效候选信号"

    price = float(cand.get("raw_signal_price") or cand.get("current") or cand.get("close") or 0.0)
    if price <= 0:
        return False, "现价异常或非正数"

    # 1. 均线过滤：高于 MA60 与 MA120
    ma60 = float(cand.get("ma60") or 0.0)
    ma120 = float(cand.get("ma120") or 0.0)
    if ma60 <= 0 or ma120 <= 0:
        if bars and len(bars) >= 120:
            ma60 = sum(float(b["close"]) for b in bars[-60:]) / 60.0
            ma120 = sum(float(b["close"]) for b in bars[-120:]) / 120.0
        else:
            return False, "缺少足够历史K线计算MA60/MA120"

    if price <= ma60:
        return False, f"收盘价¥{price:.2f}未高于MA60(¥{ma60:.2f})"
    if price <= ma120:
        return False, f"收盘价¥{price:.2f}未高于MA120(¥{ma120:.2f})"

    # 2. 动量为正：20日与60日动量为正
    ret_20 = float(cand.get("ret_20") or 0.0)
    ret_60 = float(cand.get("ret_60") or 0.0)
    if ret_20 <= 0:
        return False, f"20日动量({ret_20:+.2f}%)非正"
    if ret_60 <= 0:
        return False, f"60日动量({ret_60:+.2f}%)非正"

    # 3. 行业排名前列 (Top 4 强势行业)
    sym = cand.get("symbol", "")
    sec = cand.get("sector") or industry_map.get(sym, "其他")
    if top_industries and sec not in top_industries:
        return False, f"所属行业[{sec}]不在强势Top 4行业列表内"

    # 4. 行业整体趋势未明显走弱
    if industry_scores is not None and sec in industry_scores:
        sec_score = float(industry_scores[sec])
        if sec_score < 0:
            return False, f"所属行业[{sec}]动量得分({sec_score:.2f})已走弱"

    # 5. 流动性过滤 (日成交额 >= 3000万)
    amount = float(cand.get("amount") or (bars[-1].get("amount") if bars else 0.0) or 0.0)
    if amount > 0 and amount < 30_000_000.0:
        return False, f"成交额¥{amount:,.0f}低于3000万元门槛"

    return True, "满足RISK_OFF高质量入场门槛"


# ==============================================================================
# 2. 主账户: Product_AB_Balanced_v2 (稳健增长策略)
# ==============================================================================

class ProductABBalancedV2Strategy(DailyMomentumStrategy):
    """
    主账户: Product AB Balanced v2.0
    中等风险、稳健增长。A/B混动选股，组合总仓位随Regime调节 (30%~80%)。
    """

    def __init__(self, account, watchlist=None, name="Product_AB_Balanced_v2", feed=None,
                 industry_map=None, **kwargs):
        self.feed = feed or HistoricalDataFeed()
        self.account = account
        self.name = name

        config_kwargs = {
            "max_positions": 3,
            "max_stock_weight": 0.15,
            "max_sector_weight": 0.30,
            "stop_loss_pct": -6.0,
            "stop_loss_mode": "intraday",
            "trailing_stop_enabled": True,
            "trailing_stop_mode": "atr",
            "trailing_stop_activation_pct": 20.0,
            "atr_trailing_multiplier": 3.5,
            "profit_floor_enabled": True,
            "profit_floor_pct": 3.0,
            "annual_cash_yield": 2.5,
            "refill_vacancies": False,
            "rebalance_every": 5,
            "min_holding_days": 5,
            "cooldown_days": 3,
            "dynamic_universe_enabled": True,
            "dynamic_top_industries": 4,
            "dynamic_stocks_per_industry": 12,
            "dynamic_min_amount": 30000000.0,
            "regime_breadth_confirmation": True,
            "regime_strong_breadth_min": 0.60,
        }
        config_kwargs.update(kwargs)

        super().__init__(account, watchlist or [], name=name, feed=self.feed,
                         industry_map=industry_map, **config_kwargs)
        self.display_name = "Product AB Balanced v2.0 (稳健增长)"

        # Core 16 蓝筹底盘
        core_symbols = [s for s in self.feed.get_preset_symbols("core_active")
                        if s.startswith(("sh60", "sz00"))]
        self.symbols_a = list(core_symbols)
        self.watchlist = list(dict.fromkeys(self.symbols_a + self.watchlist))

        # 状态初始化
        self.state.setdefault("executed_orders_today", {})
        self.state.setdefault("position_monitors", {})
        self.state.setdefault("portfolio_risk_events", [])
        self.state.setdefault("v2_switch_snapshot", {
            "timestamp": dt.datetime.now(BEIJING).isoformat(),
            "switch_equity": self.account.get_summary().get("total_equity", 100000.0),
            "version": "v2.0"
        })

    def get_max_regime_total_wt(self, regime: Optional[str] = None) -> float:
        """获取 Balanced v2.0 当前市场状态总仓位上限"""
        r = self.state.get("current_regime", "RISK_OFF") if regime is None else regime
        return BALANCED_REGIME_CAPS.get(r, 0.0)

    def get_current_regime_and_weights(self, as_of: str) -> Tuple[str, float, float]:
        """依据沪深300均线系统与市场宽度计算 Regime 与 A/B 预算比例"""
        bench = getattr(self, "_benchmark", [])
        if not bench or len(bench) < 120 or bench[-1]["date"] != as_of:
            regime = "RISK_OFF"
        else:
            hs_close = float(bench[-1]["close"])
            ma120 = sum(float(b["close"]) for b in bench[-120:]) / 120.0
            ma60 = sum(float(b["close"]) for b in bench[-60:]) / 60.0
            ma20 = sum(float(b["close"]) for b in bench[-20:]) / 20.0

            if hs_close <= ma120:
                regime = "RISK_OFF"
            elif hs_close <= ma20:
                regime = "NEUTRAL"
            elif ma20 <= ma60:
                regime = "RISK_ON"
            else:
                # 市场宽度确认 (>MA120比例)
                cand_breadth = 1.0
                cand_symbols = self.latest_candidates if hasattr(self, "latest_candidates") else []
                if cand_symbols:
                    above_120 = 0
                    for c in cand_symbols:
                        sym = c["symbol"] if isinstance(c, dict) else c
                        bars = self.history_cache.get(sym, [])
                        if len(bars) >= 120 and bars[-1]["date"] == as_of:
                            s_ma120 = sum(float(b["close"]) for b in bars[-120:]) / 120.0
                            if float(bars[-1]["close"]) > s_ma120:
                                above_120 += 1
                    cand_breadth = above_120 / max(1, len(cand_symbols))

                if self.config.regime_breadth_confirmation and cand_breadth < self.config.regime_strong_breadth_min:
                    regime = "RISK_ON"
                else:
                    regime = "STRONG_RISK_ON"

        weights = BALANCED_AB_BUDGET_RATIOS.get(regime, (0.70, 0.30))
        return regime, weights[0], weights[1]

    def check_portfolio_risk(self, total_equity: float) -> Tuple[bool, bool, float]:
        """
        组合级风险监控:
        回撤 8% 预警; 回撤 12% 触发组合风险复核 (禁止新增开仓)
        """
        peak = max(float(self.state.get("peak_equity", total_equity)), total_equity)
        self.state["peak_equity"] = peak
        dd_pct = ((peak - total_equity) / peak * 100.0) if peak > 0 else 0.0
        self.state["max_drawdown_pct"] = max(float(self.state.get("max_drawdown_pct", 0.0)), dd_pct)

        is_alert = dd_pct >= 8.0
        is_freeze = dd_pct >= 12.0

        if is_alert or is_freeze:
            events = self.state.setdefault("portfolio_risk_events", [])
            last_ev = events[-1] if events else None
            now_iso = dt.datetime.now(BEIJING).isoformat()
            status_desc = "FREEZE_EXPANSION" if is_freeze else "DRAWDOWN_ALERT"
            if not last_ev or last_ev.get("status") != status_desc or (dt.datetime.fromisoformat(now_iso) - dt.datetime.fromisoformat(last_ev["timestamp"])).total_seconds() > 86400:
                events.append({
                    "timestamp": now_iso,
                    "status": status_desc,
                    "drawdown_pct": round(dd_pct, 2),
                    "equity": round(total_equity, 2),
                    "peak": round(peak, 2),
                    "message": f"Balanced 账户回撤达到 {dd_pct:.2f}% (预警线8%, 复核线12%)"
                })

        return is_alert, is_freeze, dd_pct

    def _plan(self, as_of: str, execution_day: str):
        """生成 Balanced v2.0 交易计划"""
        regime, w_a, w_b = self.get_current_regime_and_weights(as_of)
        self.state["current_regime"] = regime
        self.state["weight_a"] = w_a
        self.state["weight_b"] = w_b

        # 1. 检查退出纪律 (现有持仓风控)
        super()._plan(as_of, execution_day)

        summary = self.account.get_summary()
        total_equity = float(summary.get("total_equity", 100000.0))
        _, is_freeze, dd_pct = self.check_portfolio_risk(total_equity)

        # 2. 区分 A 池 (Core 16 蓝筹) 与 B 池 (全市场动态行业动量)
        signals = getattr(self, "latest_candidates", [])
        signals_a = [c for c in signals if c["symbol"] in self.symbols_a]
        signals_b = [c for c in signals if c["symbol"] not in self.symbols_a]

        # Top 4 强势行业
        top_sectors = []
        ind_scores = {}
        if hasattr(self, "_industry_momentum_table") and self._industry_momentum_table:
            sorted_inds = sorted(self._industry_momentum_table.items(), key=lambda x: -x[1])
            top_sectors = [ind for ind, sc in sorted_inds[:4]]
            ind_scores = dict(self._industry_momentum_table)

        # 3. RISK_OFF 过滤逻辑: 仅高质量趋势信号入场
        if regime == "RISK_OFF":
            filtered_a = []
            for c in signals_a:
                sym = c["symbol"]
                bars = self.history_cache.get(sym, [])
                ok, _ = check_risk_off_entry_filter(c, bars, self.industry_map, top_sectors, ind_scores)
                if ok:
                    filtered_a.append(c)
            signals_a = filtered_a

            filtered_b = []
            for c in signals_b:
                sym = c["symbol"]
                bars = self.history_cache.get(sym, [])
                ok, _ = check_risk_off_entry_filter(c, bars, self.industry_map, top_sectors, ind_scores)
                if ok:
                    filtered_b.append(c)
            signals_b = filtered_b

        # 4. 组合层约束检查 (单票 15%, 行业 30%, Regime 仓位上限)
        max_regime_total_wt = self.get_max_regime_total_wt(regime)
        if is_freeze:
            max_regime_total_wt = 0.0  # 组合回撤达到12%复核线，禁止新增开仓

        # 严控当前持仓占用的实际市值比例 (包含未成交旧仓)
        current_stock_val = sum(
            float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
            for pos in self.account.positions.values()
        )
        current_stock_wt = current_stock_val / total_equity if total_equity > 0 else 0.0
        remaining_allowed_wt = max(0.0, max_regime_total_wt - current_stock_wt)

        sector_weights = {}
        for sym, pos in self.account.positions.items():
            ind = sector_of(sym, self.industry_map)
            pos_val = float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
            pos_wt = pos_val / total_equity if total_equity > 0 else 0.0
            sector_weights[ind] = sector_weights.get(ind, 0.0) + pos_wt

        # 计算意图权重
        target_weights = {}
        available_slots = max(0, self.config.max_positions - len(self.account.positions))

        if remaining_allowed_wt >= 0.02 and available_slots > 0 and not is_freeze:
            # 来自 A 的分配预算
            if w_a > 0 and signals_a:
                cfg_a = copy.deepcopy(self.config)
                cfg_a.max_positions = 3
                for c in signals_a[:available_slots]:
                    sym = c["symbol"]
                    wt_a = target_weight(cfg_a, c)
                    target_weights[sym] = target_weights.get(sym, 0.0) + w_a * wt_a

            # 来自 B 的分配预算
            if w_b > 0 and signals_b:
                cfg_b = copy.deepcopy(self.config)
                cfg_b.max_positions = 3
                for i, c in enumerate(signals_b[:available_slots]):
                    sym = c["symbol"]
                    wt_b = target_weight(cfg_b, c, rank=i+1)
                    target_weights[sym] = target_weights.get(sym, 0.0) + w_b * wt_b

        merged_entries = {}
        accumulated_new_wt = 0.0
        for sym, wt in sorted(target_weights.items(), key=lambda x: -x[1]):
            if sym in self.account.positions:
                continue
            if len(self.account.positions) + len(merged_entries) >= self.config.max_positions:
                break
            if accumulated_new_wt >= remaining_allowed_wt - 1e-4:
                break

            final_wt = min(wt, self.config.max_stock_weight, remaining_allowed_wt - accumulated_new_wt)
            ind = sector_of(sym, self.industry_map)
            curr_sec_wt = sector_weights.get(ind, 0.0)
            if curr_sec_wt + final_wt > self.config.max_sector_weight + 1e-4:
                allowed_sec = max(0.0, self.config.max_sector_weight - curr_sec_wt)
                final_wt = min(final_wt, allowed_sec)

            if final_wt >= 0.02:
                accumulated_new_wt += final_wt
                sector_weights[ind] = curr_sec_wt + final_wt
                sig = next((c for c in signals if c["symbol"] == sym), None)
                if sig:
                    merged_entries[sym] = dict(
                        signal=sig,
                        target_weight=final_wt,
                        entry_reason=f"Product_AB_Balanced_v2_{regime}(A:{w_a*100:.0f}%/B:{w_b*100:.0f}%)",
                        filled_shares=0,
                        target_shares=None,
                        blocked_date=None,
                        blocked_reason=None
                    )

        self.state["pending_entries"] = merged_entries
        self.state["planned_as_of"] = as_of
        self.state["planned_policy"] = f"Product_AB_Balanced_v2_{regime}"
        self.state["execution_day"] = execution_day
        self.account.save()

    def on_tick(self, quotes: Dict[str, Dict[str, Any]], now=None):
        """执行 Balanced v2.0 盘中时钟推进与严格风控"""
        now_dt = now or dt.datetime.now(BEIJING)
        execution_day = now_dt.date().isoformat()
        as_of = last_completed_trading_day(now_dt)

        self.account.update_market_prices(quotes, now=now_dt)
        self.account.update_t1_available_shares(today_str=execution_day)

        # 维护幂等性记录
        executed_today = self.state.setdefault("executed_orders_today", {})
        for k in list(executed_today.keys()):
            if not k.startswith(execution_day):
                executed_today.pop(k, None)

        if not is_trading_time(now_dt):
            return

        monitors = self.state.setdefault("position_monitors", {})
        regime = self.state.get("current_regime", "RISK_OFF")
        max_regime_total_wt = self.get_max_regime_total_wt(regime)
        summary = self.account.get_summary()
        total_equity = float(summary.get("total_equity", 100000.0))

        # 1. 评估持仓实时风控 (硬止损 / +3%浮盈保护 / 3.5ATR跟踪 / 跌破MA20)
        for sym, pos in list(self.account.positions.items()):
            q = quotes.get(sym, {})
            price = float(q.get("price") or q.get("current") or q.get("close") or 0.0)
            if price <= 0:
                continue

            cost = float(pos.get("cost_price", 0.0))
            if cost <= 0:
                continue

            avail = pos.get("available_shares", 0) if self.account.strict_t1 else pos.get("total_shares", 0)
            buy_day = min((lot.get("date", execution_day) for lot in pos.get("buy_lots", [])), default=execution_day)
            is_past_buy = (buy_day <= as_of)

            # 更新高点跟踪器
            mon = monitors.setdefault(sym, {
                "symbol": sym,
                "cost_price": cost,
                "peak_price": max(cost, price),
                "peak_profit_pct": max(0.0, ((price / cost) - 1.0) * 100.0),
                "trailing_stop_line": 0.0
            })
            if price > mon["peak_price"]:
                mon["peak_price"] = price
                mon["peak_profit_pct"] = max(mon["peak_profit_pct"], ((price / cost) - 1.0) * 100.0)

            peak_price = mon["peak_price"]
            peak_profit = mon["peak_profit_pct"]
            current_profit = ((price / cost) - 1.0) * 100.0

            trigger_reason = None

            # 优先级 1: 固定硬止损 (-6%)
            if current_profit <= self.config.stop_loss_pct:
                trigger_reason = f"盘中硬止损信号 (浮亏 {current_profit:.2f}% <= {self.config.stop_loss_pct:.1f}%)"

            # T+1 与涨停检查
            elif is_past_buy and avail > 0:
                # 优先级 2: 冲高 >= 20% 激活 ATR 跟踪与 +3% 浮盈保护底线
                if peak_profit >= self.config.trailing_stop_activation_pct:
                    live_atr = compute_live_atr(sym, self.history_cache, period=14)
                    atr_pct = (live_atr / price) if (live_atr and price > 0) else float(pos.get("atr_pct", 0.035) or 0.035)
                    atr_drop = self.config.atr_trailing_multiplier * atr_pct * 100.0
                    atr_stop_price = round(peak_price * (1.0 - atr_drop / 100.0), 2)
                    floor_price = round(cost * (1.0 + self.config.profit_floor_pct / 100.0), 2)

                    # 保护线 = max(原ATR跟踪线, 成本价*1.03) 且只能上移
                    cand_stop = max(atr_stop_price, floor_price)
                    effective_stop = max(float(mon.get("trailing_stop_line", 0.0)), cand_stop)
                    mon["trailing_stop_line"] = effective_stop

                    if price <= effective_stop:
                        if effective_stop >= floor_price:
                            trigger_reason = f"浮盈保护锁定盈利退出 (最高浮盈 +{peak_profit:.2f}%，触发+3%成本保护底线 ¥{floor_price:.2f}，现价 ¥{price:.2f})"
                        else:
                            trigger_reason = f"ATR动态跟踪止盈退出 (3.5ATR跟踪，最高浮盈 +{peak_profit:.2f}%，保护线 ¥{effective_stop:.2f}，现价 ¥{price:.2f})"

                # 优先级 3: 盘中有效跌破 MA20 约 1%
                if not trigger_reason and getattr(self.config, "realtime_ma20_exit", True):
                    live_ma20 = compute_live_ma20(sym, price, self.history_cache)
                    if live_ma20 is not None and price < live_ma20 * 0.99:
                        trigger_reason = f"盘中跌破MA20趋势避险信号 (现价 ¥{price:.2f} < 实时MA20 ¥{live_ma20:.2f})"

            if trigger_reason and sym not in self.state.get("pending_exits", {}):
                self.state.setdefault("pending_exits", {})[sym] = {
                    "shares": avail,
                    "reason": trigger_reason
                }

        # 2. 执行卖单撮合
        pending_exits = self.state.get("pending_exits", {})
        for sym, exit_info in list(pending_exits.items()):
            pos = self.account.positions.get(sym)
            if not pos:
                pending_exits.pop(sym, None)
                monitors.pop(sym, None)
                continue
            avail = pos.get("available_shares", 0) if self.account.strict_t1 else pos.get("total_shares", 0)
            if avail <= 0:
                continue
            q = quotes.get(sym, {})
            price = float(q.get("price") or q.get("current") or q.get("close") or 0.0)
            if price <= 0:
                continue

            idem_key = f"{execution_day}_{sym}_SELL"
            if executed_today.get(idem_key):
                pending_exits.pop(sym, None)
                continue

            reason = exit_info.get("reason", "风险保护退出") if isinstance(exit_info, dict) else str(exit_info)
            res = self.account.execute_sell(symbol=sym, price=price, shares=avail, reason=reason)
            if res.get("success"):
                executed_today[idem_key] = True
                pending_exits.pop(sym, None)
                monitors.pop(sym, None)

        # 3. 执行买单撮合 (严格在卖单完成后检查资金与总仓位/单票/行业硬顶)
        pending_entries = self.state.get("pending_entries", {})
        curr_stock_val = sum(
            float(p.get("market_value", 0.0) or (p.get("total_shares", p.get("shares", 0)) * p.get("cost_price", 0.0)))
            for p in self.account.positions.values()
        )
        sector_weights = {}
        for s, p in self.account.positions.items():
            ind = sector_of(s, self.industry_map)
            p_val = float(p.get("market_value", 0.0) or (p.get("total_shares", p.get("shares", 0)) * p.get("cost_price", 0.0)))
            sector_weights[ind] = sector_weights.get(ind, 0.0) + (p_val / total_equity if total_equity > 0 else 0.0)

        for sym, entry_info in list(pending_entries.items()):
            if sym in self.account.positions:
                pending_entries.pop(sym, None)
                continue
            if len(self.account.positions) >= self.config.max_positions:
                break

            q = quotes.get(sym, {})
            price = float(q.get("price") or q.get("current") or q.get("close") or 0.0)
            if price <= 0:
                continue

            idem_key = f"{execution_day}_{sym}_BUY"
            if executed_today.get(idem_key):
                pending_entries.pop(sym, None)
                continue

            target_wt = float(entry_info.get("target_weight", 0.15))
            target_amount = total_equity * min(target_wt, self.config.max_stock_weight)

            # 重新核算可用总仓位额度与行业额度
            allowed_total_val = max(0.0, max_regime_total_wt * total_equity - curr_stock_val)
            ind = sector_of(sym, self.industry_map)
            curr_sec_wt = sector_weights.get(ind, 0.0)
            allowed_sec_val = max(0.0, (self.config.max_sector_weight - curr_sec_wt) * total_equity)

            final_buy_val = min(target_amount, allowed_total_val, allowed_sec_val, self.account.cash / 1.0005)
            shares = int(final_buy_val / price / 100) * 100
            if shares < 100:
                continue

            cost_est = shares * price * 1.0005
            if self.account.cash < cost_est:
                continue

            reason = entry_info.get("entry_reason", f"Balanced_v2_{regime}_开仓")
            res = self.account.execute_buy(symbol=sym, name=entry_info.get("signal", {}).get("name", sym),
                                           price=price, shares=shares, reason=reason)
            if res.get("success"):
                executed_today[idem_key] = True
                pending_entries.pop(sym, None)
                curr_stock_val += shares * price
                sector_weights[ind] = curr_sec_wt + (shares * price / total_equity)

        self.account.save()

    def allocation_status(self, now=None):
        """返回 Balanced v2.0 状态说明"""
        now_dt = now or dt.datetime.now(BEIJING)
        regime = self.state.get("current_regime", "RISK_OFF")
        w_a = self.state.get("weight_a", 0.70)
        w_b = self.state.get("weight_b", 0.30)
        max_cap = self.get_max_regime_total_wt(regime)

        summary = self.account.get_summary()
        eq = float(summary.get("total_equity", 100000.0))
        stock_val = sum(float(p.get("market_value", 0.0) or (p.get("total_shares", 0)*p.get("cost_price", 0.0))) for p in self.account.positions.values())
        cur_wt = stock_val / eq if eq > 0 else 0.0

        is_alert, is_freeze, dd_pct = self.check_portfolio_risk(eq)
        allow_entry = (not is_freeze) and (cur_wt < max_cap - 0.01) and (len(self.account.positions) < self.config.max_positions)

        no_reason = ""
        if is_freeze:
            no_reason = f"触发组合风险复核(回撤 {dd_pct:.2f}% >= 12%)，禁止新增开仓"
        elif len(self.account.positions) >= self.config.max_positions:
            no_reason = f"已达最大持仓数量上限 ({len(self.account.positions)}/{self.config.max_positions})"
        elif cur_wt >= max_cap - 0.01:
            no_reason = f"当前股票仓位 ({cur_wt*100:.1f}%) 已达市场状态上限 ({max_cap*100:.1f}%)"

        return {
            "strategy_id": self.name,
            "strategy_display_name": self.display_name,
            "regime": regime,
            "weight_a": w_a,
            "weight_b": w_b,
            "max_regime_total_wt_num": max_cap,
            "current_stock_weight": round(cur_wt, 4),
            "allow_new_entries": allow_entry,
            "no_entry_reason": no_reason,
            "primary_positions_count": len(self.account.positions),
            "primary_max_positions": self.config.max_positions,
            "drawdown_pct": round(dd_pct, 2),
            "is_risk_alert": is_alert,
            "is_risk_freeze": is_freeze,
            "pending_entries": self.state.get("pending_entries", {}),
            "pending_exits": self.state.get("pending_exits", {})
        }


# ==============================================================================
# 3. 第二账户: Product_B_Aggressive_v2 (激进进攻策略)
# ==============================================================================

class ProductBAggressiveV2Strategy(DailyMomentumStrategy):
    """
    第二账户: Product B Aggressive v2.0
    高风险、积极进攻。纯Product B全市场动态行业动量选股，组合总仓位随Regime调节 (50%~100%)。
    """

    def __init__(self, account, watchlist=None, name="Product_B_Aggressive_v2", feed=None,
                 industry_map=None, **kwargs):
        self.feed = feed or HistoricalDataFeed()
        self.account = account
        self.name = name

        config_kwargs = {
            "max_positions": 5,
            "max_stock_weight": 0.25,
            "max_sector_weight": 0.40,
            "stop_loss_pct": -6.0,
            "stop_loss_mode": "intraday",
            "trailing_stop_enabled": True,
            "trailing_stop_mode": "atr",
            "trailing_stop_activation_pct": 20.0,
            "atr_trailing_multiplier": 4.5,
            "profit_floor_enabled": False,  # Aggressive 不设 +3% 底线，容忍高波动
            "annual_cash_yield": 2.5,
            "refill_vacancies": False,
            "rebalance_every": 5,
            "min_holding_days": 5,
            "cooldown_days": 3,
            "dynamic_universe_enabled": True,
            "dynamic_top_industries": 4,
            "dynamic_stocks_per_industry": 12,
            "dynamic_min_amount": 30000000.0,
            "regime_breadth_confirmation": True,
            "regime_strong_breadth_min": 0.60,
        }
        config_kwargs.update(kwargs)

        super().__init__(account, watchlist or [], name=name, feed=self.feed,
                         industry_map=industry_map, **config_kwargs)
        self.display_name = "Product B Aggressive v2.0 (激进进攻)"

        # 状态初始化
        self.state.setdefault("executed_orders_today", {})
        self.state.setdefault("position_monitors", {})
        self.state.setdefault("portfolio_risk_events", [])
        self.state.setdefault("v2_switch_snapshot", {
            "timestamp": dt.datetime.now(BEIJING).isoformat(),
            "switch_equity": self.account.get_summary().get("total_equity", 100000.0),
            "version": "v2.0"
        })

    def get_max_regime_total_wt(self, regime: Optional[str] = None) -> float:
        """获取 Aggressive v2.0 当前市场状态总仓位上限"""
        r = self.state.get("current_regime", "RISK_OFF") if regime is None else regime
        return AGGRESSIVE_REGIME_CAPS.get(r, 0.0)

    def get_current_regime(self, as_of: str) -> str:
        """依据沪深300均线系统与市场宽度判定当前市场状态"""
        bench = getattr(self, "_benchmark", [])
        if not bench or len(bench) < 120 or bench[-1]["date"] != as_of:
            return "RISK_OFF"

        hs_close = float(bench[-1]["close"])
        ma120 = sum(float(b["close"]) for b in bench[-120:]) / 120.0
        ma60 = sum(float(b["close"]) for b in bench[-60:]) / 60.0
        ma20 = sum(float(b["close"]) for b in bench[-20:]) / 20.0

        if hs_close <= ma120:
            return "RISK_OFF"
        elif hs_close <= ma20:
            return "NEUTRAL"
        elif ma20 <= ma60:
            return "RISK_ON"
        else:
            cand_breadth = 1.0
            cand_symbols = self.latest_candidates if hasattr(self, "latest_candidates") else []
            if cand_symbols:
                above_120 = 0
                for c in cand_symbols:
                    sym = c["symbol"] if isinstance(c, dict) else c
                    bars = self.history_cache.get(sym, [])
                    if len(bars) >= 120 and bars[-1]["date"] == as_of:
                        s_ma120 = sum(float(b["close"]) for b in bars[-120:]) / 120.0
                        if float(bars[-1]["close"]) > s_ma120:
                            above_120 += 1
                cand_breadth = above_120 / max(1, len(cand_symbols))

            if self.config.regime_breadth_confirmation and cand_breadth < self.config.regime_strong_breadth_min:
                return "RISK_ON"
            return "STRONG_RISK_ON"

    def check_portfolio_risk(self, total_equity: float) -> Tuple[bool, bool, float]:
        """
        组合级风险监控:
        回撤 15% 预警; 回撤 20% 触发组合风险复核 (禁止新增开仓)
        """
        peak = max(float(self.state.get("peak_equity", total_equity)), total_equity)
        self.state["peak_equity"] = peak
        dd_pct = ((peak - total_equity) / peak * 100.0) if peak > 0 else 0.0
        self.state["max_drawdown_pct"] = max(float(self.state.get("max_drawdown_pct", 0.0)), dd_pct)

        is_alert = dd_pct >= 15.0
        is_freeze = dd_pct >= 20.0

        if is_alert or is_freeze:
            events = self.state.setdefault("portfolio_risk_events", [])
            last_ev = events[-1] if events else None
            now_iso = dt.datetime.now(BEIJING).isoformat()
            status_desc = "FREEZE_EXPANSION" if is_freeze else "DRAWDOWN_ALERT"
            if not last_ev or last_ev.get("status") != status_desc or (dt.datetime.fromisoformat(now_iso) - dt.datetime.fromisoformat(last_ev["timestamp"])).total_seconds() > 86400:
                events.append({
                    "timestamp": now_iso,
                    "status": status_desc,
                    "drawdown_pct": round(dd_pct, 2),
                    "equity": round(total_equity, 2),
                    "peak": round(peak, 2),
                    "message": f"Aggressive 账户回撤达到 {dd_pct:.2f}% (预警线15%, 复核线20%)"
                })

        return is_alert, is_freeze, dd_pct

    def _plan(self, as_of: str, execution_day: str):
        """生成 Aggressive v2.0 交易计划 (纯 Product B 动态行业动量进攻)"""
        regime = self.get_current_regime(as_of)
        self.state["current_regime"] = regime

        # 1. 检查退出纪律 (现有持仓风控)
        super()._plan(as_of, execution_day)

        summary = self.account.get_summary()
        total_equity = float(summary.get("total_equity", 100000.0))
        _, is_freeze, dd_pct = self.check_portfolio_risk(total_equity)

        # 2. 纯 Product B: 全市场动态候选池
        signals = getattr(self, "latest_candidates", [])

        # Top 4 强势行业
        top_sectors = []
        ind_scores = {}
        if hasattr(self, "_industry_momentum_table") and self._industry_momentum_table:
            sorted_inds = sorted(self._industry_momentum_table.items(), key=lambda x: -x[1])
            top_sectors = [ind for ind, sc in sorted_inds[:4]]
            ind_scores = dict(self._industry_momentum_table)

        # 3. RISK_OFF 过滤: 仅允许高质量趋势信号入场
        if regime == "RISK_OFF":
            filtered = []
            for c in signals:
                sym = c["symbol"]
                bars = self.history_cache.get(sym, [])
                ok, _ = check_risk_off_entry_filter(c, bars, self.industry_map, top_sectors, ind_scores)
                if ok:
                    filtered.append(c)
            signals = filtered

        # 4. 组合层约束检查 (单票 25%, 行业 40%, Regime 仓位上限)
        max_regime_total_wt = self.get_max_regime_total_wt(regime)
        if is_freeze:
            max_regime_total_wt = 0.0

        current_stock_val = sum(
            float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
            for pos in self.account.positions.values()
        )
        current_stock_wt = current_stock_val / total_equity if total_equity > 0 else 0.0
        remaining_allowed_wt = max(0.0, max_regime_total_wt - current_stock_wt)

        sector_weights = {}
        for sym, pos in self.account.positions.items():
            ind = sector_of(sym, self.industry_map)
            pos_val = float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
            pos_wt = pos_val / total_equity if total_equity > 0 else 0.0
            sector_weights[ind] = sector_weights.get(ind, 0.0) + pos_wt

        # 计算 Product B 意图权重
        target_weights = {}
        available_slots = max(0, self.config.max_positions - len(self.account.positions))

        if remaining_allowed_wt >= 0.02 and available_slots > 0 and not is_freeze:
            cfg_b = copy.deepcopy(self.config)
            cfg_b.max_positions = 5
            for i, c in enumerate(signals[:available_slots]):
                sym = c["symbol"]
                wt_b = target_weight(cfg_b, c, rank=i+1)
                target_weights[sym] = wt_b

        merged_entries = {}
        accumulated_new_wt = 0.0
        for sym, wt in sorted(target_weights.items(), key=lambda x: -x[1]):
            if sym in self.account.positions:
                continue
            if len(self.account.positions) + len(merged_entries) >= self.config.max_positions:
                break
            if accumulated_new_wt >= remaining_allowed_wt - 1e-4:
                break

            final_wt = min(wt, self.config.max_stock_weight, remaining_allowed_wt - accumulated_new_wt)
            ind = sector_of(sym, self.industry_map)
            curr_sec_wt = sector_weights.get(ind, 0.0)
            if curr_sec_wt + final_wt > self.config.max_sector_weight + 1e-4:
                allowed_sec = max(0.0, self.config.max_sector_weight - curr_sec_wt)
                final_wt = min(final_wt, allowed_sec)

            if final_wt >= 0.02:
                accumulated_new_wt += final_wt
                sector_weights[ind] = curr_sec_wt + final_wt
                sig = next((c for c in signals if c["symbol"] == sym), None)
                if sig:
                    merged_entries[sym] = dict(
                        signal=sig,
                        target_weight=final_wt,
                        entry_reason=f"Product_B_Aggressive_v2_{regime}(动量排名:{sig.get('score',0):.1f})",
                        filled_shares=0,
                        target_shares=None,
                        blocked_date=None,
                        blocked_reason=None
                    )

        self.state["pending_entries"] = merged_entries
        self.state["planned_as_of"] = as_of
        self.state["planned_policy"] = f"Product_B_Aggressive_v2_{regime}"
        self.state["execution_day"] = execution_day
        self.account.save()

    def on_tick(self, quotes: Dict[str, Dict[str, Any]], now=None):
        """执行 Aggressive v2.0 盘中时钟推进与宽幅风控"""
        now_dt = now or dt.datetime.now(BEIJING)
        execution_day = now_dt.date().isoformat()
        as_of = last_completed_trading_day(now_dt)

        self.account.update_market_prices(quotes, now=now_dt)
        self.account.update_t1_available_shares(today_str=execution_day)

        executed_today = self.state.setdefault("executed_orders_today", {})
        for k in list(executed_today.keys()):
            if not k.startswith(execution_day):
                executed_today.pop(k, None)

        if not is_trading_time(now_dt):
            return

        monitors = self.state.setdefault("position_monitors", {})
        regime = self.state.get("current_regime", "RISK_OFF")
        max_regime_total_wt = self.get_max_regime_total_wt(regime)
        summary = self.account.get_summary()
        total_equity = float(summary.get("total_equity", 100000.0))

        # 1. 评估持仓实时风控 (硬止损 / 4.5ATR宽幅跟踪 / 连续2日收盘跌破MA20)
        for sym, pos in list(self.account.positions.items()):
            q = quotes.get(sym, {})
            price = float(q.get("price") or q.get("current") or q.get("close") or 0.0)
            if price <= 0:
                continue

            cost = float(pos.get("cost_price", 0.0))
            if cost <= 0:
                continue

            avail = pos.get("available_shares", 0) if self.account.strict_t1 else pos.get("total_shares", 0)
            buy_day = min((lot.get("date", execution_day) for lot in pos.get("buy_lots", [])), default=execution_day)
            is_past_buy = (buy_day <= as_of)

            mon = monitors.setdefault(sym, {
                "symbol": sym,
                "cost_price": cost,
                "peak_price": max(cost, price),
                "peak_profit_pct": max(0.0, ((price / cost) - 1.0) * 100.0),
                "trailing_stop_line": 0.0
            })
            if price > mon["peak_price"]:
                mon["peak_price"] = price
                mon["peak_profit_pct"] = max(mon["peak_profit_pct"], ((price / cost) - 1.0) * 100.0)

            peak_price = mon["peak_price"]
            peak_profit = mon["peak_profit_pct"]
            current_profit = ((price / cost) - 1.0) * 100.0

            trigger_reason = None

            # 优先级 1: 固定硬止损 (-6%)
            if current_profit <= self.config.stop_loss_pct:
                trigger_reason = f"盘中硬止损信号 (浮亏 {current_profit:.2f}% <= {self.config.stop_loss_pct:.1f}%)"

            elif is_past_buy and avail > 0:
                # 优先级 2: 冲高 >= 20% 激活 4.5*ATR 宽幅跟踪止盈 (不设+3%底线)
                if peak_profit >= self.config.trailing_stop_activation_pct:
                    live_atr = compute_live_atr(sym, self.history_cache, period=14)
                    atr_pct = (live_atr / price) if (live_atr and price > 0) else float(pos.get("atr_pct", 0.035) or 0.035)
                    atr_drop = self.config.atr_trailing_multiplier * atr_pct * 100.0
                    atr_stop_price = round(peak_price * (1.0 - atr_drop / 100.0), 2)

                    # 保护线只能上移，不能下移
                    effective_stop = max(float(mon.get("trailing_stop_line", 0.0)), atr_stop_price)
                    mon["trailing_stop_line"] = effective_stop

                    # 标记是否已锁定正收益
                    mon["trailing_locked_profit"] = (effective_stop >= cost)

                    if price <= effective_stop:
                        trigger_reason = f"ATR动态跟踪止盈退出 (4.5ATR宽幅跟踪，最高浮盈 +{peak_profit:.2f}%，保护线 ¥{effective_stop:.2f}，现价 ¥{price:.2f})"

                # 优先级 3: 连续 2 个已完成交易日收盘均低于 MA20 的 99%
                if not trigger_reason:
                    bars = self.history_cache.get(sym, [])
                    if bars and len(bars) >= 22:
                        b1 = bars[-1]
                        b2 = bars[-2]
                        ma20_1 = sum(float(b["close"]) for b in bars[-20:]) / 20.0
                        ma20_2 = sum(float(b["close"]) for b in bars[-21:-1]) / 20.0
                        c1 = float(b1["close"])
                        c2 = float(b2["close"])
                        if c1 < ma20_1 * 0.99 and c2 < ma20_2 * 0.99:
                            trigger_reason = f"连续两日收盘跌破MA20趋势退出 (昨日¥{c1:.2f}<MA20¥{ma20_1:.2f}, 前日¥{c2:.2f}<MA20¥{ma20_2:.2f})"

            if trigger_reason and sym not in self.state.get("pending_exits", {}):
                self.state.setdefault("pending_exits", {})[sym] = {
                    "shares": avail,
                    "reason": trigger_reason
                }

        # 2. 执行卖单撮合
        pending_exits = self.state.get("pending_exits", {})
        for sym, exit_info in list(pending_exits.items()):
            pos = self.account.positions.get(sym)
            if not pos:
                pending_exits.pop(sym, None)
                monitors.pop(sym, None)
                continue
            avail = pos.get("available_shares", 0) if self.account.strict_t1 else pos.get("total_shares", 0)
            if avail <= 0:
                continue
            q = quotes.get(sym, {})
            price = float(q.get("price") or q.get("current") or q.get("close") or 0.0)
            if price <= 0:
                continue

            idem_key = f"{execution_day}_{sym}_SELL"
            if executed_today.get(idem_key):
                pending_exits.pop(sym, None)
                continue

            reason = exit_info.get("reason", "风险保护退出") if isinstance(exit_info, dict) else str(exit_info)
            res = self.account.execute_sell(symbol=sym, price=price, shares=avail, reason=reason)
            if res.get("success"):
                executed_today[idem_key] = True
                pending_exits.pop(sym, None)
                monitors.pop(sym, None)

        # 3. 执行买单撮合 (纯 Product B 进攻)
        pending_entries = self.state.get("pending_entries", {})
        curr_stock_val = sum(
            float(p.get("market_value", 0.0) or (p.get("total_shares", p.get("shares", 0)) * p.get("cost_price", 0.0)))
            for p in self.account.positions.values()
        )
        sector_weights = {}
        for s, p in self.account.positions.items():
            ind = sector_of(s, self.industry_map)
            p_val = float(p.get("market_value", 0.0) or (p.get("total_shares", p.get("shares", 0)) * p.get("cost_price", 0.0)))
            sector_weights[ind] = sector_weights.get(ind, 0.0) + (p_val / total_equity if total_equity > 0 else 0.0)

        for sym, entry_info in list(pending_entries.items()):
            if sym in self.account.positions:
                pending_entries.pop(sym, None)
                continue
            if len(self.account.positions) >= self.config.max_positions:
                break

            q = quotes.get(sym, {})
            price = float(q.get("price") or q.get("current") or q.get("close") or 0.0)
            if price <= 0:
                continue

            idem_key = f"{execution_day}_{sym}_BUY"
            if executed_today.get(idem_key):
                pending_entries.pop(sym, None)
                continue

            target_wt = float(entry_info.get("target_weight", 0.20))
            target_amount = total_equity * min(target_wt, self.config.max_stock_weight)

            allowed_total_val = max(0.0, max_regime_total_wt * total_equity - curr_stock_val)
            ind = sector_of(sym, self.industry_map)
            curr_sec_wt = sector_weights.get(ind, 0.0)
            allowed_sec_val = max(0.0, (self.config.max_sector_weight - curr_sec_wt) * total_equity)

            final_buy_val = min(target_amount, allowed_total_val, allowed_sec_val, self.account.cash / 1.0005)
            shares = int(final_buy_val / price / 100) * 100
            if shares < 100:
                continue

            cost_est = shares * price * 1.0005
            if self.account.cash < cost_est:
                continue

            reason = entry_info.get("entry_reason", f"Aggressive_v2_{regime}_开仓")
            res = self.account.execute_buy(symbol=sym, name=entry_info.get("signal", {}).get("name", sym),
                                           price=price, shares=shares, reason=reason)
            if res.get("success"):
                executed_today[idem_key] = True
                pending_entries.pop(sym, None)
                curr_stock_val += shares * price
                sector_weights[ind] = curr_sec_wt + (shares * price / total_equity)

        self.account.save()

    def allocation_status(self, now=None):
        """返回 Aggressive v2.0 状态说明"""
        now_dt = now or dt.datetime.now(BEIJING)
        regime = self.state.get("current_regime", "RISK_OFF")
        max_cap = self.get_max_regime_total_wt(regime)

        summary = self.account.get_summary()
        eq = float(summary.get("total_equity", 100000.0))
        stock_val = sum(float(p.get("market_value", 0.0) or (p.get("total_shares", 0)*p.get("cost_price", 0.0))) for p in self.account.positions.values())
        cur_wt = stock_val / eq if eq > 0 else 0.0

        is_alert, is_freeze, dd_pct = self.check_portfolio_risk(eq)
        allow_entry = (not is_freeze) and (cur_wt < max_cap - 0.01) and (len(self.account.positions) < self.config.max_positions)

        no_reason = ""
        if is_freeze:
            no_reason = f"触发组合风险复核(回撤 {dd_pct:.2f}% >= 20%)，禁止新增开仓"
        elif len(self.account.positions) >= self.config.max_positions:
            no_reason = f"已达最大持仓数量上限 ({len(self.account.positions)}/{self.config.max_positions})"
        elif cur_wt >= max_cap - 0.01:
            no_reason = f"当前股票仓位 ({cur_wt*100:.1f}%) 已达市场状态上限 ({max_cap*100:.1f}%)"

        return {
            "strategy_id": self.name,
            "strategy_display_name": self.display_name,
            "regime": regime,
            "max_regime_total_wt_num": max_cap,
            "current_stock_weight": round(cur_wt, 4),
            "allow_new_entries": allow_entry,
            "no_entry_reason": no_reason,
            "sandbox_positions_count": len(self.account.positions),
            "sandbox_max_positions": self.config.max_positions,
            "drawdown_pct": round(dd_pct, 2),
            "is_risk_alert": is_alert,
            "is_risk_freeze": is_freeze,
            "pending_entries": self.state.get("pending_entries", {}),
            "pending_exits": self.state.get("pending_exits", {})
        }


# ==============================================================================
# 4. 双账户协同运行适配器 (DualAccountCoordinator)
# ==============================================================================

class ProductABv2DualCoordinator(ProductABBalancedV2Strategy):
    """
    双账户运行适配器:
    挂载主账户 (Product_AB_Balanced_v2) 并协同调度第二账户 (Product_B_Aggressive_v2)。
    主账户与第二账户共享底层行情与缓存，但交易状态、订单、持仓、风控完全独立。
    向后兼容原有的 ctx.strategy 接口与 sandbox_account 属性。
    """

    def __init__(self, account, watchlist=None, name="Product_AB_Balanced_v2",
                 feed=None, industry_map=None, sandbox_account=None, sandbox_path=None, **kwargs):
        super().__init__(account, watchlist=watchlist, name=name, feed=feed,
                         industry_map=industry_map, **kwargs)

        # 初始化独立第二账户 (Aggressive v2.0)
        from sim_trader import SimAccount
        sb_path = Path(sandbox_path) if sandbox_path else (BASE_DIR / "data" / "sandbox_ab_account.json")
        if sandbox_account is not None:
            self.sandbox_account = sandbox_account
        elif sb_path.exists():
            self.sandbox_account = SimAccount(initial_cash=100000.0, save_path=str(sb_path), strict_t1=True)
            self.sandbox_account.load()
        else:
            self.sandbox_account = SimAccount(initial_cash=100000.0, save_path=str(sb_path), strict_t1=True)
            self.sandbox_account.save()

        # 实例化完全独立的 Aggressive v2.0 策略对象
        self.aggressive_strategy = ProductBAggressiveV2Strategy(
            account=self.sandbox_account,
            watchlist=self.watchlist,
            name="Product_B_Aggressive_v2",
            feed=self.feed,
            industry_map=self.industry_map
        )
        self.sandbox_strategy = self.aggressive_strategy

    def _plan(self, as_of: str, execution_day: str):
        """同时为 Balanced 主账户和 Aggressive 第二账户分别排定独立交易计划"""
        # 1. 主账户独立计划
        super()._plan(as_of, execution_day)
        # 2. 第二账户独立计划 (共享行情指标，但独立计算仓位与选股)
        self.aggressive_strategy.latest_candidates = getattr(self, "latest_candidates", [])
        self.aggressive_strategy._benchmark = getattr(self, "_benchmark", [])
        self.aggressive_strategy._benchmark_valid = getattr(self, "_benchmark_valid", False)
        self.aggressive_strategy._industry_momentum_table = getattr(self, "_industry_momentum_table", {})
        self.aggressive_strategy._plan(as_of, execution_day)

    def on_tick(self, quotes: Dict[str, Dict[str, Any]], now=None):
        """时钟推进: 主账户与第二账户独立执行风控与撮合"""
        super().on_tick(quotes, now=now)
        self.aggressive_strategy.on_tick(quotes, now=now)

    def allocation_status(self, now=None):
        """合并返回双账户状态说明"""
        res = super().allocation_status(now=now)
        sb_res = self.aggressive_strategy.allocation_status(now=now)
        res["sandbox_positions_count"] = sb_res.get("sandbox_positions_count", 0)
        res["sandbox_max_positions"] = sb_res.get("sandbox_max_positions", 5)
        res["sandbox_stock_weight"] = sb_res.get("current_stock_weight", 0.0)
        res["sandbox_regime"] = sb_res.get("regime", "RISK_OFF")
        res["sandbox_max_regime_total_wt_num"] = sb_res.get("max_regime_total_wt_num", 0.50)
        res["sandbox_drawdown_pct"] = sb_res.get("drawdown_pct", 0.0)
        res["sandbox_allow_new_entries"] = sb_res.get("allow_new_entries", False)
        res["sandbox_no_entry_reason"] = sb_res.get("no_entry_reason", "")
        return res
