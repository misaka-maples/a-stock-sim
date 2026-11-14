# src/astock/data/providers/baostock_provider.py
import contextlib
import io
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .base import BaseMarketDataProvider
from ..models import DailyBar, StockInfo, normalize_symbol, to_baostock_code

logger = logging.getLogger(__name__)

# Add local market_sdk if present
_sdk_path = Path(__file__).resolve().parents[4] / "data" / "market_sdk"
if _sdk_path.exists() and str(_sdk_path) not in sys.path:
    sys.path.insert(0, str(_sdk_path))


class BaoStockProvider(BaseMarketDataProvider):
    """
    Adapter for BaoStock official free quantitative finance SDK (0.9.4).
    Zero credential requirement, provides historical daily bars with ST & suspension flags.
    """

    def __init__(self):
        self._name = "baostock"
        self._lock = threading.Lock()
        self._bs = None
        self._logged_in = False

    @property
    def name(self) -> str:
        return self._name

    def _ensure_login(self):
        if self._bs is None:
            import baostock as bs
            self._bs = bs
        if not self._logged_in:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                res = self._bs.login()
                if res.error_code == "0":
                    self._logged_in = True
                else:
                    raise RuntimeError(f"BaoStock login failed: {res.error_code} {res.error_msg}")

    def _safe_logout(self):
        if self._logged_in and self._bs:
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self._bs.logout()
            except Exception:
                pass
            finally:
                self._logged_in = False

    def is_healthy(self) -> bool:
        try:
            with self._lock:
                self._ensure_login()
                return self._logged_in
        except Exception:
            return False

    def fetch_stock_daily(self, symbol: str, start_date: str, end_date: str,
                          adjust_type: str = "qfq") -> List[DailyBar]:
        """
        Fetch daily bars for a single stock using BaoStock.
        adjustflag: '1' 后复权, '2' 前复权, '3' 不复权.
        """
        sym = normalize_symbol(symbol)
        code = to_baostock_code(sym)
        flag = "2" if adjust_type == "qfq" else ("1" if adjust_type == "hfq" else "3")

        with self._lock:
            self._ensure_login()
            fields = "date,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
            res = self._bs.query_history_k_data_plus(
                code, fields,
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag=flag
            )
            if res.error_code != "0":
                if res.error_code in ("10001001", "10001011"):
                    self._logged_in = False
                raise RuntimeError(f"BaoStock query error {res.error_code}: {res.error_msg}")

            bars: List[DailyBar] = []
            now_iso = datetime.utcnow().isoformat()
            while res.next():
                row = dict(zip(res.fields, res.get_row_data()))
                try:
                    d_date = row["date"]
                    # Volume in BaoStock is in shares (股) -> convert to lots (手, 100 shares)
                    vol_shares = float(row.get("volume") or 0.0)
                    vol_lots = vol_shares / 100.0
                    amt_yuan = float(row.get("amount") or 0.0)

                    open_p = float(row.get("open") or 0.0)
                    high_p = float(row.get("high") or 0.0)
                    low_p = float(row.get("low") or 0.0)
                    close_p = float(row.get("close") or 0.0)
                    pre_close = float(row["preclose"]) if row.get("preclose") else None

                    is_susp = (row.get("tradestatus") == "0")
                    is_st = (row.get("isST") == "1")

                    turnover = float(row["turn"]) if row.get("turn") not in (None, "") else None
                    chg_pct = float(row["pctChg"]) if row.get("pctChg") not in (None, "") else None

                    bars.append(DailyBar(
                        symbol=sym,
                        trade_date=d_date,
                        open=open_p,
                        high=high_p,
                        low=low_p,
                        close=close_p,
                        volume=vol_lots,
                        amount=amt_yuan,
                        source="baostock",
                        adjust_type=adjust_type,
                        updated_at=now_iso,
                        is_final=True,
                        raw_open=open_p,
                        raw_high=high_p,
                        raw_low=low_p,
                        raw_close=close_p,
                        raw_pre_close=pre_close,
                        adjustment_factor=1.0,
                        turnover_rate=turnover,
                        change_pct=chg_pct,
                        is_suspended=is_susp,
                        is_st=is_st
                    ))
                except (ValueError, TypeError, KeyError):
                    continue

            return bars

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Fetch daily bars for benchmark index (e.g. '000300' or 'sh000300')."""
        sym = normalize_symbol(index_code)
        code = to_baostock_code(sym)
        with self._lock:
            self._ensure_login()
            # Index does not support isST or adjustflag
            fields = "date,open,high,low,close,preclose,volume,amount,pctChg"
            res = self._bs.query_history_k_data_plus(
                code, fields,
                start_date=start_date, end_date=end_date,
                frequency="d"
            )
            if res.error_code != "0":
                if res.error_code in ("10001001", "10001011"):
                    self._logged_in = False
                raise RuntimeError(f"BaoStock index query error {res.error_code}: {res.error_msg}")

            bars: List[DailyBar] = []
            now_iso = datetime.utcnow().isoformat()
            while res.next():
                row = dict(zip(res.fields, res.get_row_data()))
                try:
                    vol_lots = float(row.get("volume") or 0.0) / 100.0
                    amt_yuan = float(row.get("amount") or 0.0)
                    open_p = float(row.get("open") or 0.0)
                    high_p = float(row.get("high") or 0.0)
                    low_p = float(row.get("low") or 0.0)
                    close_p = float(row.get("close") or 0.0)
                    pre_close = float(row["preclose"]) if row.get("preclose") else None
                    chg_pct = float(row["pctChg"]) if row.get("pctChg") not in (None, "") else None

                    bars.append(DailyBar(
                        symbol=sym,
                        trade_date=row["date"],
                        open=open_p,
                        high=high_p,
                        low=low_p,
                        close=close_p,
                        volume=vol_lots,
                        amount=amt_yuan,
                        source="baostock",
                        adjust_type="none",
                        updated_at=now_iso,
                        is_final=True,
                        raw_open=open_p,
                        raw_high=high_p,
                        raw_low=low_p,
                        raw_close=close_p,
                        raw_pre_close=pre_close,
                        adjustment_factor=1.0,
                        change_pct=chg_pct,
                        is_suspended=False,
                        is_st=False
                    ))
                except (ValueError, TypeError, KeyError):
                    continue

            return bars

    def fetch_stock_list(self) -> List[StockInfo]:
        """Fetch stock universe list (fallback to local stock_universe.json if query too slow)."""
        base_dir = Path(__file__).resolve().parents[4]
        universe_path = base_dir / "stock_universe.json"
        if universe_path.exists():
            try:
                data = json.loads(universe_path.read_text())
                stocks = []
                for s in data.get("stocks", []):
                    stocks.append(StockInfo(
                        symbol=normalize_symbol(s["symbol"]),
                        name=s.get("name", ""),
                        industry=s.get("industry", ""),
                        area=s.get("area", ""),
                        market=s.get("market", ""),
                        list_date=s.get("list_date", "")
                    ))
                return stocks
            except Exception:
                pass
        return []

    def fetch_industry_map(self) -> Dict[str, str]:
        """Return symbol -> industry name mapping from BaoStock query_stock_industry."""
        with self._lock:
            self._ensure_login()
            res = self._bs.query_stock_industry()
            mapping = {}
            if res.error_code == "0":
                while res.next():
                    row = res.get_row_data()
                    # row: [updateDate, code, code_name, industry, industryClassification]
                    if len(row) >= 4 and row[1]:
                        sym = normalize_symbol(row[1])
                        ind = row[3] or "其他"
                        mapping[sym] = ind
            return mapping
