"""
Product AB: 自适应混动量化策略引擎 (Base Engine + Turbo Engine)
- Product A (Base Engine): 核心大盘蓝筹池 (Core 16)，以高抗跌、低波动、平稳复利为底盘。
- Product B (Turbo Engine): 全市场动态行业动量池 (Top 4 行业 x 12 龙头)，在主升浪环境爆发性进攻与捕获右尾收益。
- Product AB 依据 Market Regime 自适应动态分配两套子策略的风险预算与资金权重。
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional, List, Any
import statistics, math

from history_data import HistoricalDataFeed
from momentum_backtest import MomentumBacktestEngine
from strategy_rules import MomentumConfig


@dataclass
class ProductABConfig:
    """Product AB 自适应混动策略参数配置"""
    regime_weights: Dict[str, Tuple[float, float]] = field(default_factory=lambda: {
        "RISK_OFF": (1.00, 0.00),       # 熊市/防守: 100% A (安全垫防守)
        "NEUTRAL": (1.00, 0.00),        # 震荡市: 100% A (蓝筹平稳复利，回避宽幅震荡假突破)
        "RISK_ON": (0.20, 0.80),        # 趋势确认: 20% A / 80% B (全市场行业龙头全力爆发)
        "STRONG_RISK_ON": (0.50, 0.50)  # 强趋势/高位: 50% A / 50% B (大蓝筹缓冲防跳空下杀，B抓大牛股)
    })
    initial_cash: float = 100000.0


class ProductABEngine:
    """Product AB 自适应混动回测与执行引擎"""

    def __init__(self,
                 start_date: str,
                 end_date: str,
                 ab_config: Optional[ProductABConfig] = None,
                 config_a: Optional[MomentumConfig] = None,
                 config_b: Optional[MomentumConfig] = None,
                 feed: Optional[HistoricalDataFeed] = None):
        self.start_date = start_date
        self.end_date = end_date
        self.ab_config = ab_config or ProductABConfig()
        self.feed = feed or HistoricalDataFeed()

        # Product A 默认配置 (Core 16 弱化ATR)
        self.config_a = config_a or MomentumConfig(
            refill_vacancies=False,
            regime_sizing_enabled=True,
            trailing_stop_enabled=True,
            trailing_stop_mode="atr",
            trailing_stop_activation_pct=20.0,
            atr_trailing_multiplier=3.5,
            annual_cash_yield=2.5,
            breakeven_enabled=False,
            dynamic_universe_enabled=False
        )

        # Product B 默认配置 (动态行业轮动 + 动量分层 + 强市增仓)
        self.config_b = config_b or MomentumConfig(
            refill_vacancies=False,
            regime_sizing_enabled=True,
            trailing_stop_enabled=True,
            trailing_stop_mode="atr",
            trailing_stop_activation_pct=20.0,
            atr_trailing_multiplier=3.5,
            annual_cash_yield=2.5,
            breakeven_enabled=False,
            dynamic_universe_enabled=True,
            dynamic_top_industries=4,
            dynamic_stocks_per_industry=12,
            dynamic_min_amount=30000000.0,
            regime_breadth_confirmation=True,
            regime_strong_breadth_min=0.60,
            momentum_tiered_sizing=True,
            tier_a_multiplier=1.0,
            tier_b_multiplier=0.75,
            tier_c_multiplier=0.50,
            regime_strong_positions=4,
            regime_strong_risk=0.035,
            regime_strong_weight=0.35
        )

        self.symbols_a = [s for s in self.feed.get_preset_symbols("core_active")
                          if s.startswith(("sh60", "sz00"))]

    def run(self) -> Dict[str, Any]:
        """运行 Product A 与 Product B，并合成 Product AB 自适应混动回测结果"""
        engine_a = MomentumBacktestEngine(
            start_date=self.start_date,
            end_date=self.end_date,
            config=self.config_a,
            symbols=self.symbols_a,
            initial_cash=self.ab_config.initial_cash,
            feed=self.feed
        )
        res_a = engine_a.run()

        engine_b = MomentumBacktestEngine(
            start_date=self.start_date,
            end_date=self.end_date,
            config=self.config_b,
            symbols=None,
            initial_cash=self.ab_config.initial_cash,
            feed=self.feed
        )
        res_b = engine_b.run()

        curve_a = {p["date"]: p for p in res_a["equity_curve"]}
        curve_b = {p["date"]: p for p in res_b["equity_curve"]}
        common_dates = sorted(set(curve_a.keys()) & set(curve_b.keys()))

        initial_equity = float(self.ab_config.initial_cash)
        equity = initial_equity
        blended_curve = []
        peak_equity = initial_equity
        max_drawdown = 0.0

        for i, date in enumerate(common_dates):
            p_b = curve_b[date]
            regime = p_b.get("regime", "UNKNOWN")
            weights = self.ab_config.regime_weights.get(regime, (0.50, 0.50))
            w_a, w_b = weights

            if i == 0:
                ret_a = 0.0
                ret_b = 0.0
                port_ret = 0.0
            else:
                prev_date = common_dates[i - 1]
                p_a_curr, p_a_prev = curve_a[date], curve_a[prev_date]
                p_b_curr, p_b_prev = curve_b[date], curve_b[prev_date]

                ret_a = (p_a_curr["equity"] / p_a_prev["equity"] - 1.0) if p_a_prev["equity"] > 0 else 0.0
                ret_b = (p_b_curr["equity"] / p_b_prev["equity"] - 1.0) if p_b_prev["equity"] > 0 else 0.0
                port_ret = w_a * ret_a + w_b * ret_b
                equity = equity * (1.0 + port_ret)

            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd

            blended_curve.append({
                "date": date,
                "equity": round(equity, 2),
                "daily_return": round(port_ret * 100.0, 4),
                "regime": regime,
                "weight_a": w_a,
                "weight_b": w_b,
                "equity_a": p_a_curr["equity"] if i > 0 else initial_equity,
                "equity_b": p_b["equity"],
                "drawdown_pct": round(dd * 100.0, 2)
            })

        total_return_pct = round((equity / initial_equity - 1.0) * 100.0, 2)
        total_days = len(common_dates)
        cagr_pct = round(((1.0 + total_return_pct / 100.0) ** (252.0 / max(1, total_days)) - 1.0) * 100.0, 2)
        mdd_pct = round(max_drawdown * 100.0, 2)
        calmar = round(cagr_pct / mdd_pct, 2) if mdd_pct > 0 else 0.0

        daily_rets = [p["daily_return"] / 100.0 for p in blended_curve[1:]]
        if daily_rets and len(daily_rets) > 1:
            ann_vol = statistics.stdev(daily_rets) * math.sqrt(252)
            rf = 0.025
            ann_mean = statistics.mean(daily_rets) * 252
            sharpe = round((ann_mean - rf) / ann_vol, 2) if ann_vol > 0 else 0.0
        else:
            sharpe = 0.0

        summary = {
            "strategy": "Product AB (Adaptive Hybrid)",
            "initial_cash": initial_equity,
            "final_equity": round(equity, 2),
            "total_return_pct": total_return_pct,
            "cagr_pct": cagr_pct,
            "max_drawdown_pct": mdd_pct,
            "calmar_ratio": calmar,
            "sharpe_ratio": sharpe,
            "total_days": total_days,
            "product_a_summary": res_a["summary"],
            "product_b_summary": res_b["summary"]
        }

        return {
            "summary": summary,
            "equity_curve": blended_curve,
            "result_a": res_a,
            "result_b": res_b
        }
