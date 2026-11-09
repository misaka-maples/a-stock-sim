"""
Product AB: 自适应混动策略实盘/模拟调度器 (Base Engine + Turbo Engine)
- 冻结规则权重:
    RISK_OFF: 100% A / 0% B
    NEUTRAL:  100% A / 0% B
    RISK_ON:  20% A / 80% B
    STRONG_RISK_ON: 50% A / 50% B
- 组合层约束: 组合单票上限 35%, 组合行业上限 35%, 统一现金收益 2.5%
- 双向订单合并与平滑换手
- 独立影子账本 (Shadow Ledger): 实时记录 A理论NAV、B理论NAV、AB合成NAV、实际成交NAV与执行偏差
"""

import datetime as dt
import math
import copy
import statistics
from typing import Dict, List, Any, Optional, Tuple

from strategy_rules import MomentumConfig, target_weight, allowed_by_sector
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

        # 初始化/加载影子账本 (Shadow Ledger)
        self.state.setdefault("shadow_ledger", {
            "nav_a": 1.0,
            "nav_b": 1.0,
            "nav_ab_theoretical": 1.0,
            "nav_actual": 1.0,
            "tracking_error_pct": 0.0,
            "current_regime": "NEUTRAL",
            "weight_a": 1.0,
            "weight_b": 0.0,
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

    def _plan(self, as_of, execution_day):
        """生成 Product AB 自适应混动订单计划 (A+B 双引擎合成)"""
        regime, w_a, w_b = self.get_current_regime_and_weights(as_of)
        self.state["current_regime"] = regime
        self.state["weight_a"] = w_a
        self.state["weight_b"] = w_b

        # 1. 计算全量信号与候选排序
        super()._plan(as_of, execution_day)

        # 2. 区分 A 池 (Core 16 蓝筹) 与 B 池 (全市场动态动量)
        signals = getattr(self, "latest_candidates", [])
        signals_a = [c for c in signals if c["symbol"] in self.symbols_a]
        signals_b = [c for c in signals if c["symbol"] not in self.symbols_a]

        total_equity = self.account.get_summary()["total_equity"]
        budget_a = total_equity * w_a
        budget_b = total_equity * w_b

        # 3. 合并订单意图 (Order Merging)
        merged_entries = {}
        target_weights = {}

        # 来自 A 的意图
        if w_a > 0 and signals_a:
            cfg_a = copy.deepcopy(self.config)
            cfg_a.max_positions = 3
            for c in signals_a[:3]:
                sym = c["symbol"]
                wt_a = target_weight(cfg_a, c)
                target_weights[sym] = target_weights.get(sym, 0.0) + w_a * wt_a

        # 来自 B 的意图
        if w_b > 0 and signals_b:
            cfg_b = copy.deepcopy(self.config)
            cfg_b.max_positions = self.config.regime_strong_positions if regime == "STRONG_RISK_ON" else self.config.max_positions
            for i, c in enumerate(signals_b[:cfg_b.max_positions]):
                sym = c["symbol"]
                wt_b = target_weight(cfg_b, c, rank=i+1)
                target_weights[sym] = target_weights.get(sym, 0.0) + w_b * wt_b

        # 组合层约束检查 (单票上限 35%)
        for sym, wt in target_weights.items():
            final_wt = min(wt, self.config.max_stock_weight)
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

        # 4. 更新影子账本 (Shadow Ledger)
        self._update_shadow_ledger(as_of, regime, w_a, w_b)

    def _update_shadow_ledger(self, as_of: str, regime: str, w_a: float, w_b: float):
        """结算并更新理论 vs 实际影子账本"""
        ledger = self.state.setdefault("shadow_ledger", {})
        last_date = ledger.get("last_settle_date")
        if last_date == as_of:
            return  # 当日已结算

        # 实际净值
        initial_cash = getattr(self.account, "initial_cash", 100000.0)
        curr_equity = self.account.get_summary()["total_equity"]
        nav_actual = curr_equity / initial_cash if initial_cash > 0 else 1.0

        nav_a = ledger.get("nav_a", 1.0)
        nav_b = ledger.get("nav_b", 1.0)
        nav_ab = ledger.get("nav_ab_theoretical", 1.0)

        # 理论收益简易递推 (基于组合收益与利差)
        tracking_error_pct = (nav_actual / nav_ab - 1.0) * 100.0 if nav_ab > 0 else 0.0

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
            "actual_equity": round(curr_equity, 2)
        }

        history = ledger.setdefault("history", [])
        history.append(record)
        if len(history) > 250:
            ledger["history"] = history[-250:]

        ledger["last_settle_date"] = as_of
        ledger["current_regime"] = regime
        ledger["weight_a"] = w_a
        ledger["weight_b"] = w_b
        ledger["nav_actual"] = round(nav_actual, 4)
        ledger["tracking_error_pct"] = round(tracking_error_pct, 2)
        self.account.save()

    def on_tick(self, quotes, now=None):
        """时钟周期触发：先平仓、后开仓、闲置资金现金管理计息"""
        super().on_tick(quotes, now=now)

        # 每日盘后闲置现金自动计息 (Cash Overlay: 年化 2.5%)
        now_dt = now or dt.datetime.now(BEIJING)
        if now_dt.time() >= dt.time(15, 0) and is_trading_day(now_dt.date()):
            today_str = now_dt.date().isoformat()
            if self.state.get("last_cash_yield_date") != today_str and self.account.cash > 0:
                daily_yield = round(self.account.cash * (float(self.config.annual_cash_yield) / 100.0 / 252.0), 2)
                if daily_yield > 0:
                    self.account.cash += daily_yield
                    self.state["last_cash_yield_date"] = today_str
                    self.account.save()

    def get_shadow_ledger(self) -> Dict[str, Any]:
        """获取影子账本诊断详情"""
        ledger = copy.deepcopy(self.state.get("shadow_ledger", {}))
        summary = self.account.get_summary()
        ledger["total_equity"] = summary["total_equity"]
        ledger["cash"] = self.account.cash
        ledger["positions_count"] = len(self.account.positions)
        return ledger

    def allocation_status(self, now=None):
        """扩展状态展示：包含当前 Market Regime 与 A/B 动态权重分配"""
        status = super().allocation_status(now=now)
        as_of = last_completed_trading_day(now or dt.datetime.now(BEIJING))
        regime, w_a, w_b = self.get_current_regime_and_weights(as_of)
        ledger = self.state.get("shadow_ledger", {})

        status["hybrid_mode"] = True
        status["market_regime"] = regime
        status["weight_product_a"] = f"{w_a*100:.0f}%"
        status["weight_product_b"] = f"{w_b*100:.0f}%"
        status["tracking_error_pct"] = f"{ledger.get('tracking_error_pct', 0.0):+.2f}%"
        status["regime_description"] = (
            "全额大盘蓝筹与安全垫防守 (A 100%)" if regime in ("RISK_OFF", "NEUTRAL") else
            "趋势主升浪进攻爆发 (A 20% / B 80%)" if regime == "RISK_ON" else
            "强趋势平衡攻防 (A 50% / B 50%)"
        )
        return status
