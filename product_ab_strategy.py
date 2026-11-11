"""
Product AB: 自适应混动策略实盘/模拟调度器 (Base Engine + Turbo Engine)
- 冻结规则权重:
    RISK_OFF: 100% A / 0% B (现金防御, 不开新仓)
    NEUTRAL:  100% A / 0% B (蓝筹底盘平稳复利)
    RISK_ON:  20% A / 80% B (动态行业龙头全面进攻)
    STRONG_RISK_ON: 50% A / 50% B (攻防平衡, 防假突破跳空)
- 组合层约束: 组合单票上限 35%, 组合行业上限 35%, 统一现金收益 2.5%
- 双向订单合并与平滑换手 (Order Merging)
- 双账本彻底隔离:
    主账户 (Primary Production Account): 延续历史资金 (¥95,424.63) 与 57 笔流水, 严控旧仓市值并随纪律退出
    测试账本 (Sandbox Testing Account): 独立 10 万元起步, 纯净前向验证 Product AB v1.0
- 独立影子账本 (Shadow Ledger): 实时记录 A理论NAV、B理论NAV、AB合成NAV、实际成交NAV与执行偏差
- 交易幂等性: 严格校验当日订单执行标记, 防重复下单
"""

import os
import datetime as dt
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
import math
import copy
import statistics
from typing import Dict, List, Any, Optional, Tuple

from strategy_rules import (MomentumConfig, target_weight, allowed_by_sector,
                            sector_of, evaluate_momentum, holding_signal, exit_reason)
from daily_momentum import DailyMomentumStrategy, last_completed_trading_day
from history_data import HistoricalDataFeed, PRESET_POOLS, normalize_symbol_for_tencent
from trade_calendar import is_trading_day, BEIJING


FROZEN_AB_WEIGHTS = {
    "RISK_OFF": (1.00, 0.00),
    "NEUTRAL": (1.00, 0.00),
    "RISK_ON": (0.20, 0.80),
    "STRONG_RISK_ON": (0.50, 0.50)
}


class ProductABHybridStrategy(DailyMomentumStrategy):
    """Product AB 自适应混动交易策略 (Base Engine + Turbo Engine)"""

    def __init__(self,
                 account,
                 watchlist: Optional[List[str]] = None,
                 name: str = "Product_AB",
                 feed: Optional[HistoricalDataFeed] = None,
                 industry_map: Optional[Dict[str, str]] = None,
                 **kwargs):
        self.feed = feed or HistoricalDataFeed()
        self.account = account
        self.name = name
        self._custom_sandbox_account = kwargs.pop("sandbox_account", None)
        self._custom_sandbox_path = kwargs.pop("sandbox_path", None)

        # 核心默认配置 (兼顾 A 底盘与 B 爆发)
        config_kwargs = {
            "refill_vacancies": False,
            "regime_sizing_enabled": True,
            "trailing_stop_enabled": True,
            "trailing_stop_mode": "atr",
            "trailing_stop_activation_pct": 20.0,
            "atr_trailing_multiplier": 3.5,
            "annual_cash_yield": 2.5,
            "breakeven_enabled": False,
            "max_stock_weight": 0.35,
            "max_sector_weight": 0.35,
            "dynamic_universe_enabled": True,
            "dynamic_top_industries": 4,
            "dynamic_stocks_per_industry": 12,
            "dynamic_min_amount": 30000000.0,
            "regime_breadth_confirmation": True,
            "regime_strong_breadth_min": 0.60,
            "momentum_tiered_sizing": True,
            "tier_a_multiplier": 1.0,
            "tier_b_multiplier": 0.75,
            "tier_c_multiplier": 0.50,
            "regime_strong_positions": 4,
            "regime_strong_risk": 0.035,
            "regime_strong_weight": 0.35,
        }
        config_kwargs.update(kwargs)

        super().__init__(account, watchlist or [], name=name, feed=self.feed,
                         industry_map=industry_map, **config_kwargs)
        self.display_name = "Product AB 自适应混动系统 (Base Engine + Turbo Engine)"

        # 确保 Core 16 蓝筹底盘标的纳入监控池
        core_symbols = [s for s in self.feed.get_preset_symbols("core_active")
                        if s.startswith(("sh60", "sz00"))]
        self.symbols_a = list(core_symbols)
        all_watch = list(dict.fromkeys(self.symbols_a + self.watchlist))
        self.watchlist = all_watch

        # 幂等性记录初始化 (主账户)
        self.state.setdefault("executed_orders_today", {})
        self.state.setdefault("position_monitors", {})

        # 初始化独立前向测试账本 (Sandbox Testing Account - 严格自治隔离)
        sandbox_account = self._custom_sandbox_account
        sandbox_path = self._custom_sandbox_path
        from sim_trader import SimAccount
        if sandbox_account is not None:
            self.sandbox_account = sandbox_account
        elif getattr(account, "save_path", None) is None and sandbox_path is None:
            self.sandbox_account = SimAccount(
                initial_cash=100000.0,
                strict_t1=True
            )
        else:
            sb_path = Path(sandbox_path) if sandbox_path else (BASE_DIR / "data" / "sandbox_ab_account.json")
            if sb_path.exists():
                self.sandbox_account = SimAccount(
                    initial_cash=100000.0,
                    save_path=str(sb_path),
                    strict_t1=True
                )
                self.sandbox_account.load()
            else:
                self.sandbox_account = SimAccount(
                    initial_cash=100000.0,
                    save_path=str(sb_path),
                    strict_t1=True
                )
                self.sandbox_account.save()

        # 测试账本的自治状态直接挂载在 sandbox_account 内部策略状态中，实现彻底物理隔离
        self.sandbox_state = self.sandbox_account.strategy_state.setdefault("sandbox_state", {
            "pending_entries": {},
            "pending_exits": {},
            "position_monitors": {},
            "executed_orders_today": {},
            "last_cash_yield_date": None
        })

        # 初始化影子账本 (Shadow Ledger)
        self.state.setdefault("shadow_ledger", {
            "nav_a": 1.0,
            "nav_b": 1.0,
            "nav_ab_theoretical": 1.0,
            "nav_actual": 1.0,
            "tracking_error_pct": 0.0,
            "current_regime": "NEUTRAL",
            "weight_a": 1.0,
            "weight_b": 0.0,
            "target_holdings_a": {},
            "target_holdings_b": {},
            "last_settle_date": None,
            "history": []
        })

    def get_current_regime_and_weights(self, as_of: str) -> Tuple[str, float, float]:
        """依据沪深300趋势与候选池市场宽度判定当前市场状态与冻结权重"""
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

        weights = FROZEN_AB_WEIGHTS.get(regime, (0.50, 0.50))
        return regime, weights[0], weights[1]

    def _calc_theoretical_return(self, holdings: Dict[str, float], date_prev: str, date_curr: str) -> float:
        """计算指定理论持仓从 date_prev 到 date_curr 的日度收益率 (含闲置现金计息)"""
        stock_ret_sum = 0.0
        total_stock_weight = 0.0
        for sym, wt in holdings.items():
            total_stock_weight += wt
            bars = self.history_cache.get(sym, [])
            c_prev = next((float(b["close"]) for b in reversed(bars) if b["date"] == date_prev), None)
            c_curr = next((float(b["close"]) for b in reversed(bars) if b["date"] == date_curr), None)
            if c_prev and c_curr and c_prev > 0:
                stock_ret_sum += wt * (c_curr / c_prev - 1.0)

        cash_weight = max(0.0, 1.0 - total_stock_weight)
        cash_ret = cash_weight * (float(self.config.annual_cash_yield) / 100.0 / 252.0)
        return stock_ret_sum + cash_ret

    def _update_shadow_ledger(self, as_of: str, regime: str, w_a: float, w_b: float,
                             today_holdings_a: Dict[str, float], today_holdings_b: Dict[str, float]):
        """结算并更新理论 vs 实际影子账本"""
        ledger = self.state.setdefault("shadow_ledger", {})
        last_date = ledger.get("last_settle_date")
        if last_date == as_of:
            ledger["target_holdings_a"] = today_holdings_a
            ledger["target_holdings_b"] = today_holdings_b
            ledger["current_regime"] = regime
            ledger["weight_a"] = w_a
            ledger["weight_b"] = w_b
            self.account.save()
            return

        initial_cash = getattr(self.account, "initial_cash", 100000.0)
        curr_equity = self.account.get_summary()["total_equity"]
        nav_actual = curr_equity / initial_cash if initial_cash > 0 else 1.0

        nav_a = float(ledger.get("nav_a", 1.0))
        nav_b = float(ledger.get("nav_b", 1.0))
        nav_ab = float(ledger.get("nav_ab_theoretical", 1.0))

        prev_holdings_a = ledger.get("target_holdings_a", {})
        prev_holdings_b = ledger.get("target_holdings_b", {})
        prev_w_a = float(ledger.get("weight_a", 1.0))
        prev_w_b = float(ledger.get("weight_b", 0.0))

        ret_ab = 0.0
        ret_a = 0.0
        ret_b = 0.0
        prev_equity = float(ledger.get("actual_equity", initial_cash))
        actual_daily_ret = (curr_equity / prev_equity - 1.0) if prev_equity > 0 else 0.0

        if last_date is not None:
            ret_a = self._calc_theoretical_return(prev_holdings_a, last_date, as_of)
            ret_b = self._calc_theoretical_return(prev_holdings_b, last_date, as_of)
            ret_ab = prev_w_a * ret_a + prev_w_b * ret_b

            nav_a = nav_a * (1.0 + ret_a)
            nav_b = nav_b * (1.0 + ret_b)
            nav_ab = nav_ab * (1.0 + ret_ab)

        tracking_error_pct = (nav_actual / nav_ab - 1.0) * 100.0 if nav_ab > 0 else 0.0
        active_return = (actual_daily_ret - ret_ab) if last_date is not None else 0.0

        # 现金隔离：分别记录回测假设现金年化 2.5% 产生的收益累积 vs 真实活期利息
        cash_wt_a = max(0.0, 1.0 - sum(prev_holdings_a.values()))
        cash_wt_b = max(0.0, 1.0 - sum(prev_holdings_b.values()))
        blended_cash_wt = prev_w_a * cash_wt_a + prev_w_b * cash_wt_b
        daily_assumed_cash_pnl = blended_cash_wt * (float(self.config.annual_cash_yield) / 100.0 / 252.0) * nav_ab * initial_cash if last_date is not None else 0.0
        cum_assumed_cash = float(ledger.get("cum_assumed_cash_pnl", 0.0)) + daily_assumed_cash_pnl
        cum_real_cash = float(ledger.get("cum_real_cash_interest", 0.0))

        # 硬性告警判定 (单日主动收益偏差 > 0.5% 或 累计净值偏离 > 1.0%)
        alerts = []
        if last_date is not None and abs(active_return) > 0.005:
            alerts.append(f"单日主动收益偏差告警 ({active_return*100:+.2f}% > 0.5%)")
        if abs(tracking_error_pct) > 1.0:
            alerts.append(f"累计净值偏离告警 ({tracking_error_pct:+.2f}% > 1.0%)")

        record = {
            "date": as_of,
            "regime": regime,
            "weight_a": w_a,
            "weight_b": w_b,
            "nav_a": round(nav_a, 4),
            "nav_b": round(nav_b, 4),
            "nav_ab_theoretical": round(nav_ab, 4),
            "nav_actual": round(nav_actual, 4),
            "tracking_error_pct": round(tracking_error_pct, 2),
            "daily_active_return_pct": round(active_return * 100.0, 4),
            "actual_equity": round(curr_equity, 2),
            "holdings_count_a": len(today_holdings_a),
            "holdings_count_b": len(today_holdings_b),
            "cum_assumed_cash_pnl": round(cum_assumed_cash, 2),
            "cum_real_cash_interest": round(cum_real_cash, 2),
            "alerts": alerts
        }

        history = ledger.setdefault("history", [])
        history.append(record)
        if len(history) > 250:
            ledger["history"] = history[-250:]

        ledger["last_settle_date"] = as_of
        ledger["current_regime"] = regime
        ledger["weight_a"] = w_a
        ledger["weight_b"] = w_b
        ledger["nav_a"] = round(nav_a, 4)
        ledger["nav_b"] = round(nav_b, 4)
        ledger["nav_ab_theoretical"] = round(nav_ab, 4)
        ledger["nav_actual"] = round(nav_actual, 4)
        ledger["tracking_error_pct"] = round(tracking_error_pct, 2)
        ledger["daily_active_return_pct"] = round(active_return * 100.0, 4)
        ledger["cum_assumed_cash_pnl"] = round(cum_assumed_cash, 2)
        ledger["cum_real_cash_interest"] = round(cum_real_cash, 2)
        ledger["alerts"] = alerts
        ledger["target_holdings_a"] = today_holdings_a
        ledger["target_holdings_b"] = today_holdings_b
        self.account.save()

    def _plan_policy(self):
        regime = self.state.get("current_regime", "RISK_OFF")
        return f"Product_AB_{regime}"

    def _plan(self, as_of, execution_day):
        """生成 Product AB 自适应混动订单计划 (A+B 双引擎合成)"""
        regime, w_a, w_b = self.get_current_regime_and_weights(as_of)
        self.state["current_regime"] = regime
        self.state["weight_a"] = w_a
        self.state["weight_b"] = w_b

        # 1. 计算全量信号与退出检查 (主账户历史持仓严格遵守退出纪律)
        super()._plan(as_of, execution_day)

        # 2. 区分 A 池 (Core 16 蓝筹) 与 B 池 (动态成长动量标的)
        signals = getattr(self, "latest_candidates", [])
        signals_a = [c for c in signals if c["symbol"] in self.symbols_a]
        signals_b = [c for c in signals if c["symbol"] not in self.symbols_a]

        today_holdings_a = {}
        today_holdings_b = {}
        target_weights = {}

        if regime != "RISK_OFF":
            # 来自 A 的意图 (最多3只)
            if w_a > 0 and signals_a:
                cfg_a = copy.deepcopy(self.config)
                cfg_a.max_positions = 3
                for c in signals_a[:3]:
                    sym = c["symbol"]
                    wt_a = target_weight(cfg_a, c)
                    today_holdings_a[sym] = wt_a
                    target_weights[sym] = target_weights.get(sym, 0.0) + w_a * wt_a

            # 来自 B 的意图 (Strong 模式 4 只，常规模式 max_positions 只)
            if w_b > 0 and signals_b:
                cfg_b = copy.deepcopy(self.config)
                cfg_b.max_positions = self.config.regime_strong_positions if regime == "STRONG_RISK_ON" else 3
                for i, c in enumerate(signals_b[:cfg_b.max_positions]):
                    sym = c["symbol"]
                    wt_b = target_weight(cfg_b, c, rank=i+1)
                    today_holdings_b[sym] = wt_b
                    target_weights[sym] = target_weights.get(sym, 0.0) + w_b * wt_b

        # 3. 组合层约束检查 (单票上限 35%, 行业上限 35%, 市场状态总仓位硬顶)
        # 严控历史遗留持仓 (阳光照明, 大千生态, 英力特)，其市值完全占用当前股票风险预算与行业限额
        merged_entries = {}
        total_equity = self.account.get_summary()["total_equity"]
        current_stock_val = sum(
            float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
            for sym, pos in self.account.positions.items()
            if sym not in self.state.get("pending_exits", {})
        )
        current_stock_wt = current_stock_val / total_equity if total_equity > 0 else 0.0

        max_regime_total_wt = {
            "RISK_OFF": 0.0,
            "NEUTRAL": 0.35,
            "RISK_ON": 0.70,
            "STRONG_RISK_ON": 0.90
        }.get(regime, 0.70)
        remaining_allowed_wt = max(0.0, max_regime_total_wt - current_stock_wt)

        sector_weights = {}
        for sym, pos in self.account.positions.items():
            ind = sector_of(sym, self.industry_map)
            pos_val = float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
            pos_wt = pos_val / total_equity if total_equity > 0 else 0.0
            sector_weights[ind] = sector_weights.get(ind, 0.0) + pos_wt

        # 仅当组合在当前市场状态下仍有剩余股票配额时，才允许开设新仓 (防止旧仓未退、新仓猛加导致仓位穿透)
        accumulated_new_wt = 0.0
        if remaining_allowed_wt >= 0.02:
            for sym, wt in sorted(target_weights.items(), key=lambda x: -x[1]):
                if sym in self.account.positions and sym not in self.state.get("pending_exits", {}):
                    continue
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
                            entry_reason=f"Product_AB_{regime}_混动配置(A:{w_a*100:.0f}%/B:{w_b*100:.0f}%)",
                            filled_shares=0,
                            target_shares=None,
                            blocked_date=None,
                            blocked_reason=None
                        )

        self.state["pending_entries"] = merged_entries
        self.state["planned_as_of"] = as_of
        self.state["planned_policy"] = f"Product_AB_{regime}"

        # 3.5 为测试账本排定订单计划 (严格基于标准 10 万元独立基准与当前测试持仓)
        self._plan_sandbox(as_of, execution_day, regime, w_a, w_b, signals_a, signals_b)

        # 4. 更新影子账本 (Shadow Ledger)
        self._update_shadow_ledger(as_of, regime, w_a, w_b, today_holdings_a, today_holdings_b)
        self.account.save()

    def _plan_sandbox(self, as_of, execution_day, regime, w_a, w_b, signals_a, signals_b):
        """为测试账本独立排定订单计划 (严格基于标准 10 万元独立基准与当前测试持仓)"""
        if not hasattr(self, "sandbox_account") or self.sandbox_account is None:
            return

        sb_exits = {}
        for sym, pos in list(self.sandbox_account.positions.items()):
            bars = self.history_cache.get(sym, [])
            if not bars:
                continue
            sig = holding_signal(bars, as_of)
            if not sig:
                continue
            cost = float(pos.get("cost_price", 0.0))
            price = float(sig.get("raw_signal_price", cost))
            reason = exit_reason(price, cost, sig, self.config)
            if reason:
                sb_exits[sym] = {"shares": pos.get("total_shares", pos.get("shares", 0)), "reason": reason}

        self.sandbox_state["pending_exits"] = sb_exits

        sb_entries = {}
        if regime != "RISK_OFF":
            sb_target_weights = {}
            if w_a > 0 and signals_a:
                cfg_a = copy.deepcopy(self.config)
                cfg_a.max_positions = 3
                for c in signals_a[:3]:
                    sym = c["symbol"]
                    wt_a = target_weight(cfg_a, c)
                    sb_target_weights[sym] = sb_target_weights.get(sym, 0.0) + w_a * wt_a

            if w_b > 0 and signals_b:
                cfg_b = copy.deepcopy(self.config)
                cfg_b.max_positions = 4 if regime == "STRONG_RISK_ON" else 3
                for i, c in enumerate(signals_b[:cfg_b.max_positions]):
                    sym = c["symbol"]
                    wt_b = target_weight(cfg_b, c, rank=i+1)
                    sb_target_weights[sym] = sb_target_weights.get(sym, 0.0) + w_b * wt_b

            sb_equity = self.sandbox_account.get_summary()["total_equity"]
            sb_stock_val = sum(
                float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
                for sym, pos in self.sandbox_account.positions.items()
                if sym not in sb_exits
            )
            sb_stock_wt = sb_stock_val / sb_equity if sb_equity > 0 else 0.0

            max_regime_total_wt = {
                "RISK_OFF": 0.0,
                "NEUTRAL": 0.35,
                "RISK_ON": 0.70,
                "STRONG_RISK_ON": 0.90
            }.get(regime, 0.70)
            sb_remaining_allowed_wt = max(0.0, max_regime_total_wt - sb_stock_wt)

            sb_sec_weights = {}
            for sym, pos in self.sandbox_account.positions.items():
                if sym not in sb_exits:
                    ind = sector_of(sym, self.industry_map)
                    mv = float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
                    sb_sec_weights[ind] = sb_sec_weights.get(ind, 0.0) + (mv / sb_equity if sb_equity > 0 else 0)

            sb_accumulated_new_wt = 0.0
            if sb_remaining_allowed_wt >= 0.02:
                for sym, raw_wt in sorted(sb_target_weights.items(), key=lambda x: -x[1]):
                    if sym in self.sandbox_account.positions and sym not in sb_exits:
                        continue
                    if sb_accumulated_new_wt >= sb_remaining_allowed_wt - 1e-4:
                        break
                    final_wt = min(raw_wt, self.config.max_stock_weight, sb_remaining_allowed_wt - sb_accumulated_new_wt)
                    ind = sector_of(sym, self.industry_map)
                    curr_sec = sb_sec_weights.get(ind, 0.0)
                    if curr_sec + final_wt > self.config.max_sector_weight + 1e-4:
                        allowed_sec = max(0.0, self.config.max_sector_weight - curr_sec)
                        final_wt = min(final_wt, allowed_sec)

                    if final_wt >= 0.02:
                        sb_accumulated_new_wt += final_wt
                        sb_sec_weights[ind] = curr_sec + final_wt
                        sig = next((c for c in signals_a + signals_b if c["symbol"] == sym), None)
                        if sig:
                            sb_entries[sym] = dict(
                                signal=sig,
                                target_weight=final_wt,
                                entry_reason=f"Sandbox_AB_{regime}_混动配置(A:{w_a*100:.0f}%/B:{w_b*100:.0f}%)",
                                filled_shares=0
                            )

        self.sandbox_state["pending_entries"] = sb_entries

    def _tick_sandbox(self, quotes: Dict[str, Dict[str, Any]], now=None):
        """时钟推进测试账本模拟撮合与估值 (严格基于 10 万元独立账户与独立风控状态)"""
        if not hasattr(self, "sandbox_account") or self.sandbox_account is None:
            return

        now_dt = now or dt.datetime.now(BEIJING)
        execution_day = now_dt.date().isoformat()

        self.sandbox_account.update_market_prices(quotes, now=now_dt)
        self.sandbox_account.update_t1_available_shares(today_str=execution_day)

        # 维护独立幂等性注册表
        executed_today = self.sandbox_state.setdefault("executed_orders_today", {})
        for k in list(executed_today.keys()):
            if not k.startswith(execution_day):
                executed_today.pop(k, None)

        from daily_momentum import is_trading_time
        if is_trading_time(now_dt):
            # 1. 测试账本独立实时风控与 ATR 动态追踪止盈 (与主账户物理隔离)
            sb_monitors = self.sandbox_state.setdefault("position_monitors", {})
            from position_guardian import update_position_monitors, evaluate_position_realtime_rules
            update_position_monitors(self.sandbox_account.positions, quotes, self.history_cache, sb_monitors, now=now_dt)

            for sym, pos in list(self.sandbox_account.positions.items()):
                q = quotes.get(sym, {})
                price = float(q.get("price") or q.get("current") or q.get("close") or 0.0)
                if price <= 0:
                    continue
                avail = pos.get("available_shares", 0) if self.sandbox_account.strict_t1 else pos.get("total_shares", 0)
                cost = float(pos.get("cost_price", 0.0))
                buy_day = min((lot.get("date", execution_day) for lot in pos.get("buy_lots", [])), default=execution_day)
                as_of = last_completed_trading_day(now_dt)

                guard_reason, _ = evaluate_position_realtime_rules(
                    sym, pos, q, self.history_cache, self.config, sb_monitors.get(sym, {}),
                    now=now_dt, available_shares=avail, validated_price=price, is_past_buy=(buy_day <= as_of)
                )

                trigger_reason = guard_reason
                if not trigger_reason and cost > 0 and (price / cost - 1.0) * 100.0 <= self.config.stop_loss_pct:
                    trigger_reason = "盘中硬止损信号"

                if trigger_reason and sym not in self.sandbox_state.get("pending_exits", {}):
                    self.sandbox_state.setdefault("pending_exits", {})[sym] = {"shares": avail, "reason": trigger_reason}

            # 2. 独立卖出撮合与幂等性校验
            pending_exits = self.sandbox_state.get("pending_exits", {})
            for sym, exit_info in list(pending_exits.items()):
                pos = self.sandbox_account.positions.get(sym)
                if not pos:
                    pending_exits.pop(sym, None)
                    sb_monitors.pop(sym, None)
                    continue
                avail = pos.get("available_shares", 0) if self.sandbox_account.strict_t1 else pos.get("total_shares", 0)
                if avail <= 0:
                    continue
                q = quotes.get(sym)
                if not q:
                    continue
                price = float(q.get("price") or q.get("close") or 0.0)
                if price <= 0:
                    continue

                idem_key = f"{execution_day}_{sym}_SELL"
                if executed_today.get(idem_key):
                    pending_exits.pop(sym, None)
                    continue

                res = self.sandbox_account.execute_sell(
                    symbol=sym,
                    price=price,
                    shares=avail,
                    reason=exit_info.get("reason", "趋势保护退出")
                )
                if res.get("success"):
                    executed_today[idem_key] = True
                    pending_exits.pop(sym, None)
                    sb_monitors.pop(sym, None)

            # 3. 独立买入撮合与幂等性校验
            pending_entries = self.sandbox_state.get("pending_entries", {})
            sb_summary = self.sandbox_account.get_summary()
            sb_equity = sb_summary["total_equity"]

            for sym, entry_info in list(pending_entries.items()):
                if sym in self.sandbox_account.positions:
                    pending_entries.pop(sym, None)
                    continue

                idem_key = f"{execution_day}_{sym}_BUY"
                if executed_today.get(idem_key):
                    pending_entries.pop(sym, None)
                    continue

                target_wt = float(entry_info.get("target_weight", 0.0))
                target_notional = sb_equity * target_wt
                q = quotes.get(sym)
                if not q:
                    continue
                price = float(q.get("price") or q.get("open") or q.get("close") or 0.0)
                if price <= 0:
                    continue
                pre_close = float(q.get("pre_close") or price)
                limit_up_price = round(pre_close * 1.10, 2)
                if price >= limit_up_price:
                    continue

                shares = int(target_notional / price / 100) * 100
                if shares < 100:
                    pending_entries.pop(sym, None)
                    continue
                needed_cash = shares * price * 1.0005
                if self.sandbox_account.cash < needed_cash:
                    shares = int(self.sandbox_account.cash / 1.0005 / price / 100) * 100
                    if shares < 100:
                        continue

                sig = entry_info.get("signal", {})
                name = sig.get("name") or q.get("name") or sym
                res = self.sandbox_account.execute_buy(
                    symbol=sym,
                    name=name,
                    price=price,
                    shares=shares,
                    reason=entry_info.get("entry_reason", "Product_AB_Sandbox_Entry")
                )
                if res.get("success"):
                    executed_today[idem_key] = True
                    pending_entries.pop(sym, None)

        # 4. 测试账本每日现金管理收益 (独立年化 2.5%, 绝不污染主账户)
        if now_dt.time() >= dt.time(15, 0) and is_trading_day(now_dt.date()):
            today_str = now_dt.date().isoformat()
            if self.sandbox_state.get("last_cash_yield_date") != today_str and self.sandbox_account.cash > 0:
                daily_yield = round(self.sandbox_account.cash * (float(self.config.annual_cash_yield) / 100.0 / 252.0), 2)
                if daily_yield > 0:
                    self.sandbox_account.cash += daily_yield
                self.sandbox_state["last_cash_yield_date"] = today_str
                self.sandbox_account.save()

    def on_tick(self, quotes, now=None):
        """时钟周期触发：主账户撮合 + 测试账本同步模拟撮合与估值 (完全隔离)"""
        now_dt = now or dt.datetime.now(BEIJING)
        execution_day = now_dt.date().isoformat()

        # 维护主账户幂等性注册表
        executed_today = self.state.setdefault("executed_orders_today", {})
        for k in list(executed_today.keys()):
            if not k.startswith(execution_day):
                executed_today.pop(k, None)

        super().on_tick(quotes, now=now)

        # 记录主账户今日成交幂等标记
        for t in self.account.trades[-10:]:
            if str(t.get("time", "")).startswith(execution_day.replace("-", "")):
                side = "BUY" if t.get("shares", 0) > 0 else "SELL"
                self.state["executed_orders_today"][f"{execution_day}_{t['symbol']}_{side}"] = True

        # 测试账本时钟推进
        self._tick_sandbox(quotes, now=now)

        # 主账户每日闲置现金收益独立计息
        if now_dt.time() >= dt.time(15, 0) and is_trading_day(now_dt.date()):
            today_str = now_dt.date().isoformat()
            if self.state.get("last_cash_yield_date") != today_str and self.account.cash > 0:
                daily_yield = round(self.account.cash * (float(self.config.annual_cash_yield) / 100.0 / 252.0), 2)
                if daily_yield > 0:
                    self.account.cash += daily_yield
                    self.state["last_cash_yield_date"] = today_str
                    self.account.save()

    def generate_diagnostic_report(self, as_of: Optional[str] = None) -> Dict[str, Any]:
        """生成每日三轨自动诊断报告 (主账户 vs Sandbox测试账本 vs 理论影子基准)"""
        from ab_diagnostic import build_diagnostic_report, format_diagnostic_markdown
        report = build_diagnostic_report(self, as_of=as_of)
        md_text = format_diagnostic_markdown(report)
        md_dir = BASE_DIR / "data" / "diagnostic_reports"
        md_dir.mkdir(parents=True, exist_ok=True)
        md_path = md_dir / f"diagnostic_{report['report_date'].replace('-', '')}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        return {
            "report": report,
            "markdown": md_text,
            "markdown_path": str(md_path)
        }

    def get_shadow_ledger(self) -> Dict[str, Any]:
        """获取双账本架构诊断详情：主账户 (生产历史连续) vs 测试账本 (纯净前向验证) vs 理论影子账本"""
        ledger = copy.deepcopy(self.state.get("shadow_ledger", {}))
        summary = self.account.get_summary()
        sb_summary = self.sandbox_account.get_summary() if hasattr(self, "sandbox_account") else {}

        return {
            "mode": "dual_ledger",
            "primary_account": {
                "name": "主账户 (生产历史连续)",
                "initial_cash": getattr(self.account, "initial_cash", 100000.0),
                "total_equity": summary["total_equity"],
                "cash": self.account.cash,
                "market_value": summary.get("market_value", 0.0),
                "positions_count": len(self.account.positions),
                "total_trades": len(self.account.trades),
                "unrealized_pnl": summary.get("unrealized_pnl", 0.0),
                "realized_pnl": summary.get("realized_pnl", 0.0),
                "total_pnl_pct": summary.get("total_pnl_pct", 0.0),
                "positions": {s: p.get("total_shares", p.get("shares", 0)) for s, p in self.account.positions.items()}
            },
            "sandbox_account": {
                "name": "测试账本 (Product AB 纯净前向验证)",
                "initial_cash": getattr(self.sandbox_account, "initial_cash", 100000.0) if hasattr(self, "sandbox_account") else 100000.0,
                "total_equity": sb_summary.get("total_equity", 100000.0),
                "cash": self.sandbox_account.cash if hasattr(self, "sandbox_account") else 100000.0,
                "market_value": sb_summary.get("market_value", 0.0),
                "positions_count": len(self.sandbox_account.positions) if hasattr(self, "sandbox_account") else 0,
                "total_trades": len(self.sandbox_account.trades) if hasattr(self, "sandbox_account") else 0,
                "total_return_pct": round((sb_summary.get("total_equity", 100000.0) / self.sandbox_account.initial_cash - 1.0) * 100.0, 2) if hasattr(self, "sandbox_account") else 0.0,
                "positions": {s: p.get("total_shares", p.get("shares", 0)) for s, p in self.sandbox_account.positions.items()} if hasattr(self, "sandbox_account") else {}
            },
            "theoretical_shadow": ledger,
            "total_equity": summary["total_equity"],
            **ledger
        }

    def allocation_status(self, now=None):
        """扩展状态展示：包含当前 Market Regime 与 A/B 动态权重分配"""
        status = super().allocation_status(now=now)
        as_of = last_completed_trading_day(now or dt.datetime.now(BEIJING))
        regime, w_a, w_b = self.get_current_regime_and_weights(as_of)
        ledger = self.state.get("shadow_ledger", {})

        target_pos_count = 4 if regime == "STRONG_RISK_ON" else 3
        active_pos_count = len([s for s in self.account.positions if s not in self.state.get("pending_exits", {})])
        vacant = max(0, target_pos_count - active_pos_count)
        pending_cnt = len(self.state.get("pending_entries", {}))

        max_regime_total_wt = {
            "RISK_OFF": 0.0,
            "NEUTRAL": 0.35,
            "RISK_ON": 0.70,
            "STRONG_RISK_ON": 0.90
        }.get(regime, 0.70)

        total_equity = self.account.get_summary()["total_equity"]
        current_stock_val = sum(
            float(pos.get("market_value", 0.0) or (pos.get("total_shares", pos.get("shares", 0)) * pos.get("cost_price", 0.0)))
            for sym, pos in self.account.positions.items()
            if sym not in self.state.get("pending_exits", {})
        )
        current_stock_wt = current_stock_val / total_equity if total_equity > 0 else 0.0
        remaining_allowed_wt = max(0.0, max_regime_total_wt - current_stock_wt)

        allow_new_entries = bool(regime != "RISK_OFF" and remaining_allowed_wt >= 0.02)
        if regime == "RISK_OFF":
            no_entry_reason = "当前市场处于 RISK_OFF 弱市状态，新买入上限为 0.0%，全额现金防御；绝不为了凑齐3只股票而主动买入"
            wait_reason = "当前处于 RISK_OFF 弱市状态，股票仓位上限为 0%，严格禁止任何新增开仓；已有持仓正常执行风控，空位保留现金防御"
        elif remaining_allowed_wt < 0.02:
            no_entry_reason = f"当前股票仓位已达上限 ({current_stock_wt*100:.1f}% / 上限 {max_regime_total_wt*100:.1f}%)，无剩余买入配额"
            wait_reason = f"股票仓位已达上限 ({current_stock_wt*100:.1f}%)，暂停新开仓"
        else:
            no_entry_reason = "允许按调仓周期与选股门槛开仓"
            wait_reason = f"允许新开仓（最多 {target_pos_count} 只，当前在持 {active_pos_count} 只，不强制补齐）"

        status["hybrid_mode"] = True
        status["market_regime"] = regime
        status["weight_product_a"] = f"{w_a*100:.0f}%"
        status["weight_product_b"] = f"{w_b*100:.0f}%"
        status["max_regime_total_wt"] = f"{max_regime_total_wt*100:.1f}%"
        status["max_regime_total_wt_num"] = max_regime_total_wt
        status["allow_new_entries"] = allow_new_entries
        status["no_entry_reason"] = no_entry_reason
        status["vacant_slots"] = vacant
        status["pending_entry_count"] = pending_cnt
        status["refill_enabled"] = False
        status["can_refill"] = False
        status["wait_reason"] = wait_reason
        status["target_positions_count"] = target_pos_count
        status["primary_positions_count"] = active_pos_count
        status["primary_max_positions"] = target_pos_count
        sb_active_cnt = len(self.sandbox_account.positions) if hasattr(self, "sandbox_account") else 0
        status["sandbox_positions_count"] = sb_active_cnt
        status["sandbox_max_positions"] = target_pos_count

        status["tracking_error_pct"] = f"{ledger.get('tracking_error_pct', 0.0):+.2f}%"
        status["regime_description"] = (
            "全额大盘蓝筹与安全垫防守 (A 100%)" if regime in ("RISK_OFF", "NEUTRAL") else
            "趋势主升浪进攻爆发 (A 20% / B 80%)" if regime == "RISK_ON" else
            "强趋势平衡攻防 (A 50% / B 50%)"
        )
        status["shadow_ledger_summary"] = {
            "nav_a": ledger.get("nav_a", 1.0),
            "nav_b": ledger.get("nav_b", 1.0),
            "nav_ab_theoretical": ledger.get("nav_ab_theoretical", 1.0),
            "nav_actual": ledger.get("nav_actual", 1.0),
            "tracking_error_pct": ledger.get("tracking_error_pct", 0.0),
            "history_count": len(ledger.get("history", []))
        }
        return status
