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

    def __post_init__(self):
        if self.stop_loss_mode not in ("intraday", "daily_close"):
            raise ValueError("stop_loss_mode 必须为 intraday 或 daily_close")
        for name in ("entry_require_ma20", "refill_vacancies"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} 必须为布尔值")
        for name in ("max_entry_gap_down_pct", "max_entry_gap_up_pct", "max_entry_intraday_drop_pct"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= 20 or not math.isfinite(value)):
                raise ValueError(f"{name} 必须为 [0, 20] 之间的有限数值")
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
