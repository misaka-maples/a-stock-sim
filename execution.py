"""Offline, conservative A-share main-board execution models.

Volumes are in lots (one lot is 100 shares). Callers enforce cash, holdings,
fees and T+1, and retain consumption by ``(quote_key, side)``. These functions
are pure: they never place orders, fetch data or mutate the supplied snapshot.
"""

import datetime as dt
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from trade_calendar import BEIJING, is_trading_day, is_trading_time


CENT = Decimal("0.01")
HUNDRED = Decimal(100)


def _number(value, label, positive=False):
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label}必须为有限数值") from None
    if not value.is_finite() or (positive and value <= 0):
        raise ValueError(f"{label}必须为{'正的' if positive else ''}有限数值")
    return value


def _integer(value, label, positive=False):
    value = _number(value, label, positive=positive)
    if value != value.to_integral_value() or value < 0:
        raise ValueError(f"{label}必须为{'正' if positive else '非负'}整数")
    return int(value)


def _symbol(snapshot):
    symbol = str(snapshot.get("symbol") or snapshot.get("code") or "").strip().lower()
    if not re.fullmatch(r"(?:sh|sz)?\d{6}", symbol):
        raise ValueError("缺少有效主板股票代码")
    code = symbol[-6:]
    market = "sh" if code.startswith(("600", "601", "603", "605")) else "sz"
    if not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        raise ValueError("仅支持沪深主板股票")
    if len(symbol) == 8 and not symbol.startswith(market):
        raise ValueError("股票代码与交易所前缀不一致")
    return market + code


def _timestamp(value):
    if isinstance(value, dt.datetime):
        result = value
    elif isinstance(value, str):
        value = value.strip()
        try:
            if re.fullmatch(r"\d{14}", value):
                result = dt.datetime.strptime(value, "%Y%m%d%H%M%S")
            elif re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", value):
                result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                raise ValueError
        except ValueError:
            raise ValueError("行情时间必须为YYYYmmddHHMMSS或含秒的ISO时间") from None
    else:
        raise ValueError("缺少真实行情时间")
    return result.replace(tzinfo=BEIJING) if result.tzinfo is None else result.astimezone(BEIJING)


def _risk_and_limits(side, snapshot, pre_close, trading_date):
    name = str(snapshot.get("name", "")).upper()
    # Dated risk metadata takes precedence over a vendor's current name.
    risk_warning = bool(snapshot["is_st"]) if "is_st" in snapshot else "ST" in name
    if side == "BUY" and (risk_warning or "退" in name or name.startswith("PT")):
        raise ValueError("禁止买入ST/退市风险标的")
    # SSE/SZSE 2026 rules took effect on July 6, including the main-board
    # risk-warning limit change from 5% to 10%. Explicit exchange limits win.
    rate = Decimal("0.05") if risk_warning and trading_date < dt.date(2026, 7, 6) else Decimal("0.10")
    upper = _number(snapshot.get("limit_up", pre_close * (1 + rate)), "涨停价", True)
    lower = _number(snapshot.get("limit_down", pre_close * (1 - rate)), "跌停价", True)
    upper = upper.quantize(CENT, rounding=ROUND_HALF_UP)
    lower = lower.quantize(CENT, rounding=ROUND_HALF_UP)
    if lower >= upper or not lower <= pre_close <= upper:
        raise ValueError("涨跌停价元数据无效")
    return lower, upper


def _parameters(side, requested_shares, participation_rate, consumed_shares):
    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("方向必须为BUY或SELL")
    requested = _integer(requested_shares, "委托股数", positive=True)
    if side == "BUY" and requested % 100:
        raise ValueError("买入股数必须为100股整数倍")
    participation = _number(participation_rate, "参与率", True)
    if participation > 1:
        raise ValueError("参与率必须在(0,1]内")
    consumed = _integer(consumed_shares, "已消耗股数")
    return side, requested, participation, consumed


def _key(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def quote_time_key(quote):
    """Identity for a feed timestamp, even when the vendor revises its book.

    Callers should cap cumulative consumption using this key as well as the
    detailed quote_key, so a changed book with the same time cannot replenish
    the snapshot's original execution budget.
    """
    return _key({"symbol": _symbol(quote), "time": _timestamp(quote.get("time")).isoformat()})


def quote_key(quote):
    """Return the complete book identity without matching or consuming it."""
    return _key({"symbol": _symbol(quote), "time": _timestamp(quote.get("time")).isoformat(),
                 "asks": quote.get("asks", []), "bids": quote.get("bids", [])})


def _result(requested=0, shares=0, price=Decimal(0), key="", capacity=0, reason="", model="", snapshot_key=""):
    return {
        "success": shares > 0,
        "reason": reason,
        "price": float(price),
        "shares": shares,
        "status": "REJECTED" if not shares else ("FILLED" if shares == requested else "PARTIALLY_FILLED"),
        "quote_key": key,
        "capacity_shares": capacity,
        "model": model,
        "snapshot_key": snapshot_key,
    }


def _round_shares(side, value):
    return value // 100 * 100 if side == "BUY" else value


def match_quote(side, requested_shares, quote, now=None, max_age_seconds=120,
                participation_rate=.05, consumed_shares=0, limit_price=None):
    """Match at most five opposing levels and return their actual weighted price.

    Per-snapshot capacity is the smaller of visible depth and reported day
    volume, multiplied by participation_rate. Consumed shares exhaust the
    best-priced levels first; repeated calls must supply accumulated fills.
    Timestamp checks never trust ``fresh=True`` as evidence of timeliness.
    """
    key, snapshot_key, capacity = "", "", 0
    model = "五档对手盘深度参与率上限；逐档消耗并按成交量加权；不推断后续盘口补单"
    try:
        side, requested, participation, consumed = _parameters(
            side, requested_shares, participation_rate, consumed_shares)
        if not isinstance(quote, dict):
            raise ValueError("行情必须为字典")
        _symbol(quote)
        current_time = _timestamp(now) if now is not None else dt.datetime.now(BEIJING)
        quote_time = _timestamp(quote.get("time"))
        max_age = _number(max_age_seconds, "最大行情年龄")
        if max_age < 0:
            raise ValueError("最大行情年龄不能为负数")
        age = Decimal(str((current_time - quote_time).total_seconds()))
        if age < 0 or age > max_age or quote_time.date() != current_time.date():
            raise ValueError("行情已过期、来自未来或不是当日行情")
        if quote.get("fresh") is False:
            raise ValueError("行情已标记为过期")
        if not is_trading_time(current_time) or not is_trading_time(quote_time):
            raise ValueError("仅允许连续交易时段撮合")
        key, snapshot_key = quote_key(quote), quote_time_key(quote)
        current = _number(quote.get("current"), "现价", True)
        previous = _number(quote.get("pre_close"), "昨收", True)
        lower, upper = _risk_and_limits(side, quote, previous, quote_time.date())
        if not lower <= current <= upper:
            raise ValueError("现价超出合法涨跌停范围")
        volume = _number(quote.get("volume_lots"), "成交量", True) * HUNDRED
        if quote.get("suspended") or quote.get("is_suspended"):
            raise ValueError("停牌标的不能成交")
        raw_levels = quote.get("asks" if side == "BUY" else "bids")
        if not isinstance(raw_levels, (list, tuple)) or not raw_levels:
            raise ValueError("缺少有效对手盘口，不能回退至现价成交")
        levels = []
        for entry in raw_levels[:5]:
            if not isinstance(entry, dict):
                raise ValueError("盘口档位无效")
            price = _number(entry.get("price"), "盘口价格", True)
            shares = _integer(entry.get("volume"), "盘口挂量") * 100
            if not lower <= price <= upper:
                raise ValueError("盘口价格超出合法涨跌停范围")
            if price != price.quantize(CENT):
                raise ValueError("主板股票盘口价格必须符合0.01元最小价位")
            if shares:
                levels.append((price, shares))
        if not levels:
            raise ValueError("对手盘口挂量为零")
        limit = _number(limit_price, "委托限价", True) if limit_price is not None else None
        levels.sort(key=lambda item: item[0], reverse=side == "SELL")
        depth = sum(shares for _, shares in levels)
        capacity = _round_shares(side, int(min(Decimal(depth), volume) * participation))
        skip, remaining_levels = consumed, []
        for price, available in levels:
            skipped = min(skip, available)
            skip -= skipped
            available -= skipped
            if limit is not None and ((side == "BUY" and price > limit) or
                                      (side == "SELL" and price < limit)):
                continue
            if available:
                remaining_levels.append((price, available))
        eligible = sum(available for _, available in remaining_levels)
        shares = _round_shares(side, min(requested, eligible, max(0, capacity - consumed)))
        if not shares:
            raise ValueError("该盘口快照剩余容量不足或没有满足限价的对手盘")
        remaining, amount = shares, Decimal(0)
        for price, available in remaining_levels:
            filled = min(remaining, available)
            amount += price * filled
            remaining -= filled
            if not remaining:
                break
        if remaining:
            raise ValueError("可见盘口深度不足")
        return _result(requested, shares, amount / shares, key, capacity,
                       "按对手盘口成交" if shares == requested else "盘口容量或限价限制，部分成交", model,
                       snapshot_key=snapshot_key)
    except (ValueError, TypeError, InvalidOperation, OverflowError) as error:
        return _result(key=key, capacity=capacity, reason=str(error), model=model, snapshot_key=snapshot_key)


def match_daily(side, requested_shares, bar, pre_close, participation_rate=.05,
                slippage_rate=.001, consumed_shares=0):
    """Approximate next-day opening execution using unadjusted daily OHLC.

    Daily volume participation is an aggregate approximation, not evidence
    that this quantity was available at the opening. One-price limit boards
    are conservatively rejected. No intraday low/high triggers are inferred.
    """
    key, capacity = "", 0
    model = "次日原始开盘价加滑点；当日总量参与率仅为聚合容量近似，不能证明开盘可成交；一字涨跌停拒绝"
    try:
        side, requested, participation, consumed = _parameters(
            side, requested_shares, participation_rate, consumed_shares)
        if not isinstance(bar, dict):
            raise ValueError("日线必须为字典")
        symbol = _symbol(bar)
        try:
            date = dt.date.fromisoformat(str(bar.get("date", "")))
        except ValueError:
            raise ValueError("缺少有效日线日期") from None
        if not is_trading_day(date):
            raise ValueError("日线日期不是交易日")
        raw_fields = ["raw_" + field for field in ("open", "close", "high", "low")]
        if all(field in bar for field in raw_fields):
            prices = [_number(bar[field], field, True) for field in raw_fields]
        elif bar.get("adjustment") == "none" and not any(field in bar for field in raw_fields):
            prices = [_number(bar.get(field), field, True) for field in ("open", "close", "high", "low")]
        else:
            raise ValueError("撮合需要完整原始OHLC，不能用前复权价格成交")
        opening, closing, high, low = prices
        if low > high or not low <= opening <= high or not low <= closing <= high:
            raise ValueError("原始OHLC价格边界无效")
        previous = _number(pre_close, "原始昨收", True)
        lower, upper = _risk_and_limits(side, bar, previous, date)
        volume = _number(bar.get("volume"), "日成交量", True) * HUNDRED
        if bar.get("suspended") or bar.get("is_suspended"):
            raise ValueError("停牌标的不能成交")
        if high == low and (high >= upper or low <= lower):
            raise ValueError("一字涨跌停缺少可确认流动性，保守拒绝成交")
        if not lower <= opening <= upper:
            raise ValueError("原始开盘价超出合法涨跌停范围")
        slippage = _number(slippage_rate, "滑点")
        if not 0 <= slippage < 1:
            raise ValueError("滑点必须在[0,1)内")
        valid_low = max(low, lower).quantize(CENT, rounding=ROUND_CEILING)
        valid_high = min(high, upper).quantize(CENT, rounding=ROUND_FLOOR)
        if valid_low > valid_high:
            raise ValueError("当日价格与涨跌停范围没有合法成交价")
        target = opening * (1 + slippage if side == "BUY" else 1 - slippage)
        target = target.quantize(CENT, rounding=ROUND_HALF_UP)
        price = min(valid_high, max(valid_low, target))
        capacity = _round_shares(side, int(volume * participation))
        key = _key({"symbol": symbol, "date": date.isoformat(),
                    "raw_ohlc": [str(p) for p in prices], "volume": str(volume),
                    "pre_close": str(previous), "limits": [str(lower), str(upper)]})
        shares = _round_shares(side, min(requested, max(0, capacity - consumed)))
        if not shares:
            raise ValueError("该日线剩余容量不足")
        return _result(requested, shares, price, key, capacity,
                       "日线聚合近似成交" if shares == requested else "日成交量容量限制，部分成交", model)
    except (ValueError, TypeError, InvalidOperation, OverflowError) as error:
        return _result(key=key, capacity=capacity, reason=str(error), model=model)
