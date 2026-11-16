"""Pure completed-session indicators in the raw execution-price basis."""
import math


def completed_position_indicators(bars, signal_day):
    dated = sorted({str(b.get('date', b.get('trade_date', '')))[:10]: b
                    for b in bars if str(b.get('date', b.get('trade_date', '')))[:10] <= signal_day}.items())
    if not dated or dated[-1][0] != signal_day:
        return {}
    rows = [b for _, b in dated]
    if any(row.get('is_final') is False for row in rows[-21:]):
        return {}
    try:
        factor = float(rows[-1]['adjustment_factor'])
        closes = [float(b['close']) for b in rows]
        raw = float(rows[-1]['raw_close'])
        if not all(math.isfinite(x) and x > 0 for x in [factor, raw, *closes]):
            return {}
        if not math.isclose(closes[-1] / raw, factor, rel_tol=.001):
            return {}
        result = {'signal_asof': signal_day}
        if len(rows) >= 20:
            result['ma20'] = sum(closes[-20:]) / 20 / factor
            result['ma20_previous19_sum'] = sum(closes[-19:]) / factor
            count = 0
            for i in range(len(rows) - 1, max(18, len(rows) - 3), -1):
                if closes[i] < sum(closes[i-19:i+1]) / 20 * .99:
                    count += 1
                else:
                    break
            result['consecutive_below_ma20'] = count
        if len(rows) >= 15:
            ranges = []
            for i in range(len(rows) - 14, len(rows)):
                high, low, previous = float(rows[i]['high']), float(rows[i]['low']), closes[i-1]
                if not all(math.isfinite(v) and v > 0 for v in [high, low, previous]) or high < low:
                    return result
                ranges.append(max(high-low, abs(high-previous), abs(low-previous)))
            result['atr'] = sum(ranges) / 14 / factor
        return result
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return {}


def nontrading_on_execution(entry, execution_day):
    if not entry:
        return False
    status = entry.get('status') if isinstance(entry, dict) else entry
    status = str(getattr(status, 'value', status)).upper()
    if status == 'DELISTED':
        return not isinstance(entry, dict) or entry.get('effective_from', '') <= execution_day
    if status != 'SUSPENDED':
        return False
    if not isinstance(entry, dict):
        return True
    return (entry.get('effective_from', '') <= execution_day
            and entry.get('verified_through', execution_day) >= execution_day)
