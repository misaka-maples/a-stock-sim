"""Normalize native and legacy completed-day candidate payloads for V3.

Order prices remain raw. Adjusted signal prices are carried separately so the
weak-market gate compares stock trends on the same basis as historical MAs.
Missing industry rankings are deliberately not invented by this adapter.
"""
import math
from typing import Any, Dict, List

from ..models import Signal


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def candidate_signals(as_of: str, market_data: Dict[str, Any], run_id: str,
                      reason_prefix: str) -> List[Signal]:
    history = market_data.get("stock_bars") or market_data.get("candidate_bars") or {}
    industries = market_data.get("industry_map") or {}
    signals = []
    for rank, candidate in enumerate(market_data.get("candidates", []), 1):
        symbol = candidate.get("symbol", "")
        signal_asof = str(candidate.get("signal_asof") or candidate.get("as_of") or as_of)
        # Data from later sessions must never fill missing signal features.
        cutoff = min(signal_asof, as_of)
        bars = history.get(symbol, []) or []
        dated = [b for b in bars if str(b.get("date", b.get("trade_date", "")))[:10] <= cutoff]
        latest = max(dated, key=lambda b: str(b.get("date", b.get("trade_date", "")))) if dated else {}

        raw_price = _number(candidate.get("price", candidate.get("raw_signal_price",
                            candidate.get("current", candidate.get("close", 0.0)))))
        adjusted_price = _number(candidate.get("signal_price"))
        factor = _number(candidate.get("adjustment_factor"))
        if adjusted_price <= 0:
            adjusted_price = raw_price * factor if factor > 0 else raw_price
        amount = _number(candidate.get("amount", candidate.get("turnover", latest.get("amount", 0.0))))
        atr = _number(candidate.get("atr"))
        if atr <= 0 and factor > 0:
            # Legacy atr_pct is measured on adjusted closes; convert back to raw units.
            atr = _number(candidate.get("atr_pct")) * adjusted_price / factor

        score = _number(candidate.get("score"))
        signals.append(Signal(
            run_id=run_id, signal_asof=signal_asof, symbol=symbol,
            name=candidate.get("name", ""), rank=rank, score=score,
            sector=candidate.get("sector") or candidate.get("industry") or industries.get(symbol) or "其他",
            price=raw_price, amount=amount,
            ma20=_number(candidate.get("ma20")), ma60=_number(candidate.get("ma60")),
            ma120=_number(candidate.get("ma120")), atr=atr,
            ret20=_number(candidate.get("ret20", candidate.get("ret_20", 0.0))),
            ret60=_number(candidate.get("ret60", candidate.get("ret_60", 0.0))),
            features={"signal_price_adjusted": adjusted_price},
            reasons=[f"{reason_prefix}_{score:.2f}"]
        ))
    return signals


def leading_industries(as_of: str, history: Any, industry_map: Dict[str, str],
                       symbols=None, names=None, top_n: int = 4) -> Dict[str, Any]:
    """Dated legacy industry screen, shared without account or network mutation.

    Matches the existing dynamic-universe research rule: >=3 liquid stocks per
    industry, positive mean 20-day return and >=40% above MA20; rank by
    mean_ret20 * (1 + breadth_ma20). No future bars enter the screen.
    """
    groups = {}
    names = names or {}
    for symbol in symbols if symbols is not None else industry_map:
        sector = industry_map.get(symbol)
        if not sector or sector in ("其他", "未知行业"):
            continue
        bars = history.get(symbol, []) or []
        dated = sorted((b for b in bars if str(b.get("date", b.get("trade_date", "")))[:10] <= as_of),
                       key=lambda b: str(b.get("date", b.get("trade_date", ""))))
        if len(dated) < 126 or str(dated[-1].get("date", dated[-1].get("trade_date", "")))[:10] != as_of:
            continue
        last = dated[-1]
        name = str(last.get("name") or names.get(symbol, "")).upper()
        if last.get("is_st") or "ST" in name or "退" in name or last.get("is_suspended"):
            continue
        if _number(last.get("volume")) <= 0:
            continue
        recent = dated[-20:]
        mean_amount = sum(_number(b.get("amount")) for b in recent) / 20.0
        if mean_amount < 30_000_000:
            continue
        closes = [_number(b.get("close")) for b in recent]
        current = closes[-1]
        prior20 = _number(dated[-21].get("close"))
        prior60 = _number(dated[-61].get("close"))
        if min(closes + [prior20, prior60]) <= 0:
            continue
        ret20 = (current / prior20 - 1) * 100.0
        groups.setdefault(sector, []).append((ret20, current > sum(closes) / 20.0))

    scores = {}
    stats = {}
    for sector, rows in groups.items():
        if len(rows) < 3:
            continue
        avg_return = sum(r[0] for r in rows) / len(rows)
        breadth = sum(r[1] for r in rows) / len(rows)
        if avg_return > 0 and breadth >= .40:
            scores[sector] = avg_return * (1.0 + breadth)
            stats[sector] = {"stock_count": len(rows), "mean_ret20": avg_return,
                             "breadth_ma20": breadth, "score": scores[sector]}
    top = sorted(scores, key=lambda s: (-scores[s], s))[:top_n]
    return {"top_sectors": top, "sector_scores": scores,
            "industry_stats": stats, "signal_asof_date": as_of}
