"""Pure, dated momentum signals shared by paper trading and research.

All indicators consume completed bars through as_of. Prices in signals are
adjusted; order prices and cost accounting are unadjusted. No network or account
mutation belongs here.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
import math
import statistics


@dataclass
class MomentumConfig:
    max_positions: int = 6
    max_stock_weight: float = 0.20
    stop_loss_pct: float = -6.0
    stop_loss_mode: str = "intraday"
    risk_per_trade: float = 0.01
    max_sector_weight: float = 0.35
    volatility_target: float = 0.25
    momentum_w20: float = 2.0
    momentum_w60: float = 1.0
    use_ma120: bool = True
    use_high_filter: bool = True
    alpha_high: float = 0.95
    risk_adjusted: bool = False
    rebalance_every: int = 5
    refill_vacancies: bool = True
    rank_buffer: int = 3
    min_holding_days: int = 5
    cooldown_days: int = 3
    market_filter: bool = False
    participation_rate: float = 0.05
    require_positive_candle: bool = True
    max_entry_gap_down_pct: float = 3.0
    max_entry_gap_up_pct: float = 5.0
    max_entry_intraday_drop_pct: float = 3.0
    entry_require_ma20: bool = True
    # 持仓实时全息风控守护引擎参数
    enable_realtime_guardian: bool = True
    trailing_stop_enabled: bool = True
    trailing_stop_activation_pct: float = 4.0
    trailing_stop_drop_pct: float = 2.0
    breakeven_enabled: bool = True
    breakeven_activation_pct: float = 2.5
    breakeven_buffer_pct: float = 0.3
    realtime_ma20_exit: bool = True
    protect_limit_up: bool = True

    def __post_init__(self):
        if self.stop_loss_mode not in ("intraday", "daily_close"):
            raise ValueError("stop_loss_mode 必须为 intraday 或 daily_close")
        for name in ("entry_require_ma20", "refill_vacancies", "enable_realtime_guardian",
                     "trailing_stop_enabled", "breakeven_enabled", "realtime_ma20_exit", "protect_limit_up"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} 必须为布尔值")
        for name in ("max_entry_gap_down_pct", "max_entry_gap_up_pct", "max_entry_intraday_drop_pct"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= 20 or not math.isfinite(value)):
                raise ValueError(f"{name} 必须为 [0, 20] 之间的有限数值")
        for name in ("trailing_stop_activation_pct", "trailing_stop_drop_pct", "breakeven_activation_pct", "breakeven_buffer_pct"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= 100 or not math.isfinite(value)):
                raise ValueError(f"{name} 必须为 [0, 100] 之间的有限数值")
        for name in ("max_positions", "rebalance_every", "rank_buffer", "min_holding_days", "cooldown_days"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} 必须为整数")
        finite = [v for v in asdict(self).values() if isinstance(v, (int, float))]
        if not all(math.isfinite(v) for v in finite):
            raise ValueError("策略参数必须是有限数值")
        if not 1 <= self.max_positions <= 50:
            raise ValueError("max_positions 必须在 1 到 50 之间")
        for name in ("max_stock_weight", "max_sector_weight", "alpha_high", "participation_rate"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} 必须在 (0, 1] 之间")
        if not -50 <= self.stop_loss_pct < 0 or not 0 <= self.risk_per_trade <= .1:
            raise ValueError("止损须为负值，单笔风险预算须在 [0, .1] 之间")
        if self.volatility_target < 0 or self.rebalance_every < 1:
            raise ValueError("波动率目标不可为负，轮动周期须至少一个交易日")
        if min(self.rank_buffer, self.min_holding_days, self.cooldown_days) < 0:
            raise ValueError("缓冲、持仓和冷却天数不可为负")

    @classmethod
    def from_dict(cls, values=None, **overrides):
        values = {**(values or {}), **overrides}
        allowed = cls.__dataclass_fields__
        unknown = set(values) - set(allowed)
        if unknown:
            raise ValueError("未知策略参数: " + ", ".join(sorted(unknown)))
        return cls(**values)


def entry_guard_reason(signal, config, pre_close, open_price, execution_price, limit_down=None):
    """Check next-session entry using yesterday's signal and known quote prices.

    No current-session high, low, close or volume is used. Zero disables only
    the corresponding configurable threshold; the lower-limit guard remains.
    Prices are raw except MA20, whose adjusted basis comes from the signal.
    The caller validates signal/session dates and quote freshness.
    """
    def positive_number(value):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value) and value > 0
        except (OverflowError, ValueError):
            return False

    if not isinstance(signal, dict):
        return "entry_unverified_prices"
    raw_signal = signal.get("raw_signal_price")
    factor = signal.get("adjustment_factor")
    prices = (pre_close, open_price, execution_price, raw_signal, factor)
    if not all(positive_number(value) for value in prices):
        return "entry_unverified_prices"
    if limit_down is not None and not positive_number(limit_down):
        return "entry_unverified_prices"
    ma20 = signal.get("ma20")
    if config.entry_require_ma20 and not positive_number(ma20):
        return "entry_unverified_prices"

    # A different exchange reference can mean an unverified corporate action;
    # never compare raw execution prices to yesterday's adjusted close directly.
    previous = Decimal(str(pre_close))
    raw_previous = Decimal(str(raw_signal))
    opening = Decimal(str(open_price))
    execution = Decimal(str(execution_price))
    if abs(previous - raw_previous) > raw_previous * Decimal("0.01"):
        return "entry_reference_mismatch"
    lower_bound = (Decimal(str(limit_down)) if limit_down is not None
                   else previous * Decimal("0.9")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if execution <= lower_bound:
        return "entry_limit_down"

    gap_down = Decimal(str(config.max_entry_gap_down_pct)) / 100
    gap_up = Decimal(str(config.max_entry_gap_up_pct)) / 100
    intraday_drop = Decimal(str(config.max_entry_intraday_drop_pct)) / 100
    if gap_down and opening < previous * (1 - gap_down):
        return "entry_gap_down"
    if gap_up and (opening > previous * (1 + gap_up) or execution > previous * (1 + gap_up)):
        return "entry_gap_up"
    if intraday_drop and execution < opening * (1 - intraday_drop):
        return "entry_intraday_drop"
    if config.entry_require_ma20 and execution * Decimal(str(factor)) <= Decimal(str(ma20)):
        return "entry_below_ma20"
    return None


def evaluate_momentum(bars, as_of, config):
    """Return a dated eligible signal or None; never read bars after as_of."""
    dated = sorted({b["date"]: b for b in bars if b.get("date", "") <= as_of}.values(), key=lambda b: b["date"])
    if len(dated) < 125 or dated[-1]["date"] != as_of:
        return None
    dated = dated[-126:]
    try:
        closes = [float(b["close"]) for b in dated]
        if not all(math.isfinite(c) and c > 0 for c in closes):
            return None
        cl = closes[-1]
        if float(dated[-1].get("volume", 0)) <= 0:
            return None
        if config.require_positive_candle and cl < float(dated[-1]["open"]):
            return None
        ma20, ma60, ma120 = (sum(closes[-n:]) / n for n in (20, 60, 120))
        prev_ma60 = sum(closes[-65:-5]) / 60
        high20 = max(float(b["high"]) for b in dated[-20:])
        if not (cl > ma20 > ma60 and ma60 > prev_ma60):
            return None
        if config.use_ma120 and cl <= ma120:
            return None
        if config.use_high_filter and cl < config.alpha_high * high20:
            return None
        ret20 = (cl / closes[-21] - 1) * 100
        ret60 = (cl / closes[-61] - 1) * 100
        if min(ret20, ret60) <= 0:
            return None
        daily_returns = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 60, len(closes))]
        volatility = statistics.stdev(daily_returns) * math.sqrt(252)
        true_ranges = []
        for i in range(len(dated) - 14, len(dated)):
            hi, lo = float(dated[i]["high"]), float(dated[i]["low"])
            if not (math.isfinite(hi) and math.isfinite(lo) and hi >= lo > 0):
                return None
            true_ranges.append(max(hi - lo, abs(hi - closes[i - 1]), abs(lo - closes[i - 1])))
        atr_pct = sum(true_ranges) / len(true_ranges) / cl
        score = ret20 * config.momentum_w20 + ret60 * config.momentum_w60
        if config.risk_adjusted:
            score /= max(volatility, .05)
        return dict(as_of=as_of, score=score, ret_20=ret20, ret_60=ret60,
                    ma20=ma20, ma60=ma60, ma120=ma120, volatility=volatility,
                    atr_pct=atr_pct, signal_price=cl,
                    raw_signal_price=dated[-1].get("raw_close"),
                    adjustment_factor=dated[-1].get("adjustment_factor"))
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return None


def exit_reason(current_price, cost_price, signal, config, current_adjusted_price=None, *, include_stop=True):
    """Daily signal exit; call with the completed day's raw close and cost."""
    if include_stop and cost_price > 0 and (current_price / cost_price - 1) * 100 <= config.stop_loss_pct:
        return "硬止损信号"
    adjusted = current_adjusted_price
    if adjusted is None and signal:
        adjusted = current_price * float(signal.get("adjustment_factor") or 1)
    if signal and signal.get("ma20") is not None and adjusted is not None and adjusted < signal["ma20"]:
        return "跌破MA20信号"
    return None


def target_weight(config, signal):
    weight = min(config.max_stock_weight, 1.0 / config.max_positions)
    if config.risk_per_trade:
        distance = max(abs(config.stop_loss_pct) / 100, 2 * signal.get("atr_pct", 0))
        weight = min(weight, config.risk_per_trade / max(distance, .001))
    if config.volatility_target and signal.get("volatility", 0) > 0:
        weight = min(weight, config.volatility_target / signal["volatility"] / config.max_positions)
    return max(0.0, weight)


def compute_risk_sizing_row(symbol, name, shares, cost_price, total_equity, config,
                            atr_pct=0.0, volatility=0.0, cost_basis=None):
    """计算单只持仓标的的风控头寸测算明细（ATR真实波幅、2×ATR距离、风险预算上限、耗资及仓位占比）"""
    atr_val = float(atr_pct or 0.0)
    vol_val = float(volatility or 0.0)
    shares_int = int(shares or 0)
    cost_val = float(cost_price or 0.0)
    equity_val = float(total_equity or 0.0)

    # 1. 2×ATR 风险距离 (或与硬止损取大)
    stop_dist = abs(getattr(config, "stop_loss_pct", -6.0) or -6.0) / 100.0
    atr_dist = 2.0 * atr_val
    risk_distance = max(stop_dist, atr_dist) if atr_val > 0 else stop_dist

    # 2. 风险预算算得权重上限
    risk_budget = float(getattr(config, "risk_per_trade", 0.01) or 0.01)
    max_pos = max(1, int(getattr(config, "max_positions", 3) or 3))
    slot_cap = min(float(getattr(config, "max_stock_weight", 0.33) or 0.33), 1.0 / max_pos)
    risk_cap = risk_budget / max(risk_distance, 0.001) if risk_budget > 0 else slot_cap

    vol_target = float(getattr(config, "volatility_target", 0.25) or 0.25)
    vol_cap = (vol_target / vol_val / max_pos) if (vol_target > 0 and vol_val > 0) else slot_cap

    final_weight_cap = max(0.0, min(slot_cap, risk_cap, vol_cap))

    # 3. 耗资与实际仓位占比
    basis = float(cost_basis) if cost_basis is not None else round(shares_int * cost_val, 2)
    actual_pct = (basis / equity_val * 100.0) if equity_val > 0 else 0.0

    risk_label = f"{round(risk_budget * 100)}%" if round(risk_budget * 100, 2) == round(risk_budget * 100) else f"{risk_budget * 100:.1f}%"

    return {
        "symbol": symbol,
        "name": name,
        "label": f"{name} ({symbol})",
        "atr_pct": atr_val,
        "atr_pct_str": f"{atr_val * 100:.2f}%" if atr_val > 0 else "--",
        "risk_distance": risk_distance,
        "risk_distance_str": f"{risk_distance * 100:.2f}%" if risk_distance > 0 else "--",
        "risk_budget": risk_budget,
        "risk_budget_pct_label": risk_label,
        "risk_weight_cap": final_weight_cap,
        "risk_weight_cap_str": f"{final_weight_cap * 100:.2f}%",
        "shares": shares_int,
        "shares_str": f"{shares_int} 股",
        "cost": basis,
        "cost_str": f"¥{basis:,.0f}" if basis >= 1000 else f"¥{basis:,.2f}",
        "actual_pct": actual_pct,
        "actual_pct_str": f"{actual_pct:.1f}%"
    }


def format_position_sizing_markdown_table(rows, total_weight_cap=None, total_cost=None, total_actual_pct=None, risk_budget_pct="1%"):
    """生成对齐用户要求的风控头寸精准测算 Markdown 表格"""
    header_cap = f"{risk_budget_pct} 风险预算算得权重上限"
    lines = [
        f"| 持仓股票 | 日均波幅 ATR% | 2×ATR 风险距离 | {header_cap} | 实际买入股数 | 耗资 (元) | 实际仓位占比 |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
    ]
    if not rows:
        lines.append("| *当前无持仓* | - | - | - | - | - | - |")
        return "\n".join(lines)

    sum_weight = total_weight_cap if total_weight_cap is not None else sum(r.get("risk_weight_cap", 0.0) for r in rows)
    sum_cost = total_cost if total_cost is not None else sum(r.get("cost", 0.0) for r in rows)
    sum_pct = total_actual_pct if total_actual_pct is not None else sum(r.get("actual_pct", 0.0) for r in rows)

    for r in rows:
        lines.append(f"| {r['label']} | {r['atr_pct_str']} | {r['risk_distance_str']} | {r['risk_weight_cap_str']} | {r['shares_str']} | {r['cost_str']} | {r['actual_pct_str']} |")

    lines.append(f"| **合计** | - | - | **{sum_weight * 100:.2f}%** | - | **¥{sum_cost:,.0f}** | **~{sum_pct:.1f}%** |")
    return "\n".join(lines)


def sector_of(symbol, industry_map):
    value = (industry_map or {}).get(symbol)
    if isinstance(value, dict):
        value = value.get("industry") or value.get("sector")
    return value or "未知行业"


def allowed_by_sector(symbol, weight, weights, industry_map, cap):
    sector = sector_of(symbol, industry_map)
    current = sum(w for sym, w in weights.items() if sector_of(sym, industry_map) == sector)
    return current + weight <= cap + 1e-12


def holding_signal(bars, as_of):
    """Dated raw-close stop evidence; MA20 is optional when history is short."""
    history = sorted({b["date"]: b for b in bars if b.get("date", "") <= as_of}.values(), key=lambda b: b["date"])
    if not history or history[-1]["date"] != as_of:
        return None
    try:
        raw = float(history[-1]["raw_close"])
        if not math.isfinite(raw) or raw <= 0:
            return None
        result = dict(as_of=as_of, raw_signal_price=raw, signal_price=None,
                      adjustment_factor=None, ma20=None)
        try:
            close = float(history[-1].get("close") or 0)
            factor = float(history[-1].get("adjustment_factor") or 0)
        except (TypeError, ValueError):
            return result
        if not all(math.isfinite(v) and v > 0 for v in (close, factor)):
            return result
        result.update(signal_price=close, adjustment_factor=factor)
        if len(history) < 20:
            return result
        try:
            closes = [float(b["close"]) for b in history[-20:]]
        except (KeyError, TypeError, ValueError):
            return result
        if not all(math.isfinite(v) and v > 0 for v in closes):
            return result
        result["ma20"] = sum(closes) / 20
        return result
    except (KeyError, TypeError, ValueError):
        return None
