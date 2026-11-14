# src/astock/data/providers/tencent_provider.py
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional
import requests

from .base import BaseMarketDataProvider
from ..models import DailyBar, StockInfo, normalize_symbol

logger = logging.getLogger(__name__)


class TencentProvider(BaseMarketDataProvider):
    """
    Adapter for Tencent (GTImg) public financial endpoints.
    Highly responsive, provides qfq daily bars and index bars without authentication.
    """

    def __init__(self, timeout: float = 6.0):
        self._name = "tencent"
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    def is_healthy(self) -> bool:
        try:
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000300,day,,,2,qfq"
            r = self._session.get(url, timeout=3.0)
            return r.status_code == 200 and r.json().get("code") == 0
        except Exception:
            return False

    def fetch_stock_daily(self, symbol: str, start_date: str, end_date: str,
                          adjust_type: str = "qfq") -> List[DailyBar]:
        """Fetch daily bars for a stock."""
        sym = normalize_symbol(symbol)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,320,{adjust_type}"
        with self._lock:
            resp = self._session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Tencent returned error code: {data.get('code')}")

        stock_data = data.get("data", {}).get(sym, {})
        # Tencent qfq is stored in "qfqday" key, unadjusted in "day" key
        kline_key = f"{adjust_type}day" if adjust_type != "none" else "day"
        raw_rows = stock_data.get(kline_key) or stock_data.get("day", [])
        if not raw_rows:
            return []

        bars: List[DailyBar] = []
        now_iso = datetime.utcnow().isoformat()
        start = start_date or "1990-01-01"
        end = end_date or "9999-12-31"

        for row in raw_rows:
            try:
                # row: [date, open, close, high, low, volume, ...]
                d_date = str(row[0])
                if not (start <= d_date <= end):
                    continue
                open_p = float(row[1])
                close_p = float(row[2])
                high_p = float(row[3])
                low_p = float(row[4])
                vol_lots = float(row[5]) # already in lots (手)
                amt_yuan = vol_lots * 100 * ((open_p + close_p) / 2.0) # approx amount if missing

                bars.append(DailyBar(
                    symbol=sym,
                    trade_date=d_date,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol_lots,
                    amount=amt_yuan,
                    source="tencent",
                    adjust_type=adjust_type,
                    updated_at=now_iso,
                    is_final=True,
                    raw_open=open_p,
                    raw_high=high_p,
                    raw_low=low_p,
                    raw_close=close_p,
                    adjustment_factor=1.0,
                    is_suspended=(vol_lots == 0),
                    is_st=False
                ))
            except (IndexError, ValueError, TypeError):
                continue

        return bars

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Fetch daily bars for benchmark index (e.g. '000300' or 'sh000300')."""
        sym = normalize_symbol(index_code)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,320,none"
        with self._lock:
            resp = self._session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Tencent index query failed: {data.get('code')}")

        stock_data = data.get("data", {}).get(sym, {})
        raw_rows = stock_data.get("day", [])
        if not raw_rows:
            return []

        bars: List[DailyBar] = []
        now_iso = datetime.utcnow().isoformat()
        start = start_date or "1990-01-01"
        end = end_date or "9999-12-31"

        for row in raw_rows:
            try:
                d_date = str(row[0])
                if not (start <= d_date <= end):
                    continue
                open_p = float(row[1])
                close_p = float(row[2])
                high_p = float(row[3])
                low_p = float(row[4])
                vol = float(row[5])
                bars.append(DailyBar(
                    symbol=sym,
                    trade_date=d_date,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol,
                    amount=vol * 100 * close_p,
                    source="tencent",
                    adjust_type="none",
                    updated_at=now_iso,
                    is_final=True,
                    raw_open=open_p,
                    raw_high=high_p,
                    raw_low=low_p,
                    raw_close=close_p,
                    adjustment_factor=1.0,
                    is_suspended=False,
                    is_st=False
                ))
            except (IndexError, ValueError, TypeError):
                continue

        return bars

    def fetch_stock_list(self) -> List[StockInfo]:
        return []

    def fetch_industry_map(self) -> Dict[str, str]:
        return {}
