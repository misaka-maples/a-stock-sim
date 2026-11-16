"""Tencent daily history with independently verified raw/adjusted price bases."""
import math
import threading
from datetime import datetime
from typing import Dict, List
import requests

from .base import BaseMarketDataProvider
from ..models import DailyBar, StockInfo, normalize_symbol


class TencentProvider(BaseMarketDataProvider):
    def __init__(self, timeout: float = 6.0):
        self._name = "tencent"
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0"})
        self._lock = threading.Lock()

    @property
    def name(self):
        return self._name

    def _request(self, sym, start, end, adjust):
        # Official AKShare Tencent adapter uses newfqkline for both bases.
        # An explicit end is exclusive for adjusted series. Request an open
        # tail and apply the inclusive cutoff locally in _rows instead.
        base = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        param = f"{sym},day,{start},,640,{'' if adjust == 'none' else adjust}"
        with self._lock:
            response = self._session.get(base, params={"param": param}, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError("Tencent history request failed")
        return payload.get("data", {}).get(sym, {})

    def is_healthy(self):
        try:
            return bool(self._request("sh000300", "", "", "none").get("day"))
        except Exception:
            return False

    @staticmethod
    def _rows(data, adjust, start, end):
        key = "day" if adjust == "none" else adjust + "day"
        # Tencent explicitly uses `day` for adjusted requests with no adjustment
        # events (also supported by AKShare's official adapter). Do not replace
        # a present-but-empty qfqday/hfqday, which represents missing evidence.
        rows = data.get(key, []) if key in data else data.get("day", [])
        return {str(r[0]): r for r in rows if len(r) >= 6 and
                (not start or str(r[0]) >= start) and (not end or str(r[0]) <= end)}

    @staticmethod
    def _dated_st_flag(data, sym, day):
        """Only a same-session provider quote can establish a dated ST flag.

        None means unknown, not non-ST. Today's quote name must never change
        the historical risk status of an earlier daily bar.
        """
        quote_map = data.get("qt")
        quote = quote_map.get(sym) if isinstance(quote_map, dict) else None
        if not isinstance(quote, (list, tuple)) or len(quote) <= 30:
            return None
        name = str(quote[1] or "").strip()
        if not name:
            return None
        try:
            stamp = datetime.strptime(str(quote[30]), "%Y%m%d%H%M%S")
        except (ValueError, TypeError):
            return None
        if stamp.date().isoformat() != day:
            return None
        return "ST" in name.upper()

    def fetch_stock_daily(self, symbol, start_date, end_date, adjust_type="qfq"):
        if adjust_type not in ("none", "qfq", "hfq"):
            raise ValueError("Unsupported Tencent adjustment")
        sym = normalize_symbol(symbol)
        raw_data = self._request(sym, start_date, end_date, "none")
        raw = self._rows(raw_data, "none", start_date, end_date)
        if not raw:
            return []
        query_start = start_date
        # Some valid symbols return only the current row for an explicit start.
        # Recover from the canonical open-range response, then retain the local
        # cutoff and all raw/adjusted and incremental-overlap validation.
        if (len(raw) == 1 and start_date and end_date
                and (datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days >= 7):
            wide_data = self._request(sym, "", "", "none")
            wide_raw = self._rows(wide_data, "none", start_date, end_date)
            if len(wide_raw) > len(raw):
                raw_data, raw, query_start = wide_data, wide_raw, ""
        adjusted_data = raw_data if adjust_type == "none" else self._request(
            sym, query_start, end_date, adjust_type)
        adjusted = raw if adjust_type == "none" else self._rows(
            adjusted_data, adjust_type, start_date, end_date)
        if set(raw) != set(adjusted):
            raise ValueError("Tencent raw/adjusted history dates do not align")
        uses_unadjusted_alias = (adjust_type != "none" and
                                adjust_type + "day" not in adjusted_data and
                                "day" in adjusted_data)
        if uses_unadjusted_alias:
            # Only accept the documented alias when the two independent
            # responses agree that every returned OHLC needs factor 1.
            if any(not math.isclose(float(raw[day][i]), float(adjusted[day][i]),
                                    rel_tol=1e-7, abs_tol=1e-6)
                   for day in raw for i in range(1, 5)):
                raise ValueError("Tencent day alias disagrees with unadjusted prices")
        now = datetime.utcnow().isoformat()
        bars = []
        for day in sorted(raw):
            r, a = raw[day], adjusted[day]
            ro, rc, rh, rl = map(float, r[1:5])
            ao, ac, ah, al = map(float, a[1:5])
            if min(ro, rc, rh, rl, ao, ac, ah, al) <= 0:
                raise ValueError("Invalid Tencent price")
            volume = float(r[5])
            if sym.startswith(("sh688", "sh689")):
                volume /= 100.0  # STAR raw endpoint reports shares; others report lots.
            # New raw daily schema: 0=date 1=open 2=close 3=high 4=low
            # 5=volume(手), 7=换手率, 8=amount(万元). Missing means unknown.
            amount = float(r[8]) * 10000.0 if len(r) > 8 and r[8] not in (None, "") else 0.0
            if not math.isfinite(amount) or amount < 0:
                raise ValueError("Invalid Tencent amount")
            bars.append(DailyBar(
                symbol=sym, trade_date=day, open=ao, high=ah, low=al, close=ac,
                volume=volume, amount=amount, source="tencent", adjust_type=adjust_type,
                updated_at=now, is_final=True, raw_open=ro, raw_high=rh,
                raw_low=rl, raw_close=rc, adjustment_factor=ac/rc,
                turnover_rate=float(r[7]) if len(r)>7 and r[7] not in (None, "") else None,
                is_suspended=volume == 0, is_st=self._dated_st_flag(raw_data, sym, day)))
        return bars

    def fetch_index_daily(self, index_code, start_date, end_date):
        return self.fetch_stock_daily(index_code, start_date, end_date, adjust_type="none")

    def fetch_stock_list(self) -> List[StockInfo]:
        return []

    def fetch_industry_map(self) -> Dict[str, str]:
        return {}
