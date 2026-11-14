import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional
import requests

from .base import BaseMarketDataProvider
from ..models import DailyBar, StockInfo, normalize_symbol

logger = logging.getLogger(__name__)


class SinaProvider(BaseMarketDataProvider):
    """
    Adapter for Sina Finance public JSON K-line endpoint.
    Provides fast, free daily bars for A-share stocks and market indices without authentication.
    """

    def __init__(self, timeout: float = 6.0):
        self._name = "sina"
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://finance.sina.com.cn"
        })
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    def is_healthy(self) -> bool:
        try:
            url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh000300&scale=240&ma=no&datalen=1"
            r = self._session.get(url, timeout=3.0)
            return r.status_code == 200 and len(r.json()) > 0
        except Exception:
            return False

    def fetch_stock_daily(self, symbol: str, start_date: str, end_date: str,
                          adjust_type: str = "qfq") -> List[DailyBar]:
        """Fetch daily bars for a stock."""
        sym = normalize_symbol(symbol)
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=300"
        with self._lock:
            resp = self._session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, list):
            return []

        bars: List[DailyBar] = []
        now_iso = datetime.utcnow().isoformat()
        start = start_date or "1990-01-01"
        end = end_date or "9999-12-31"

        for row in data:
            try:
                d_date = str(row.get("day", ""))
                if not (start <= d_date <= end):
                    continue
                open_p = float(row["open"])
                high_p = float(row["high"])
                low_p = float(row["low"])
                close_p = float(row["close"])
                vol_shares = float(row.get("volume") or 0.0)
                vol_lots = vol_shares / 100.0
                amt_yuan = vol_lots * 100 * ((open_p + close_p) / 2.0)

                bars.append(DailyBar(
                    symbol=sym,
                    trade_date=d_date,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol_lots,
                    amount=amt_yuan,
                    source="sina",
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
            except (KeyError, ValueError, TypeError):
                continue

        return bars

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Fetch daily bars for benchmark index (e.g. '000300' or 'sh000300')."""
        sym = normalize_symbol(index_code)
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=300"
        with self._lock:
            resp = self._session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, list):
            return []

        bars: List[DailyBar] = []
        now_iso = datetime.utcnow().isoformat()
        start = start_date or "1990-01-01"
        end = end_date or "9999-12-31"

        for row in data:
            try:
                d_date = str(row.get("day", ""))
                if not (start <= d_date <= end):
                    continue
                open_p = float(row["open"])
                high_p = float(row["high"])
                low_p = float(row["low"])
                close_p = float(row["close"])
                vol_shares = float(row.get("volume") or 0.0)
                vol_lots = vol_shares / 100.0
                amt_yuan = vol_lots * 100 * close_p

                bars.append(DailyBar(
                    symbol=sym,
                    trade_date=d_date,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol_lots,
                    amount=amt_yuan,
                    source="sina",
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
            except (KeyError, ValueError, TypeError):
                continue

        return bars

    def fetch_stock_list(self) -> List[StockInfo]:
        return []

    def fetch_industry_map(self) -> Dict[str, str]:
        return {}
