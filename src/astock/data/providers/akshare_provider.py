# src/astock/data/providers/akshare_provider.py
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional

from .base import BaseMarketDataProvider
from ..models import DailyBar, StockInfo, normalize_symbol, to_clean_code

logger = logging.getLogger(__name__)


class AKShareProvider(BaseMarketDataProvider):
    """
    Adapter for AKShare open-source financial data library.
    Provides stock daily history via EastMoney/Sina, index daily history, and full A-share lists.
    """

    def __init__(self):
        self._name = "akshare"
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    def is_healthy(self) -> bool:
        try:
            import akshare as ak
            return True
        except ImportError:
            return False

    def fetch_stock_daily(self, symbol: str, start_date: str, end_date: str,
                          adjust_type: str = "qfq") -> List[DailyBar]:
        """Fetch daily bars via akshare stock_zh_a_hist."""
        import akshare as ak

        sym = normalize_symbol(symbol)
        clean = to_clean_code(sym)
        s_date = start_date.replace("-", "")
        e_date = end_date.replace("-", "")
        adj = "qfq" if adjust_type == "qfq" else ("hfq" if adjust_type == "hfq" else "")

        with self._lock:
            df = ak.stock_zh_a_hist(
                symbol=clean,
                period="daily",
                start_date=s_date,
                end_date=e_date,
                adjust=adj
            )

        if df is None or df.empty:
            return []

        bars: List[DailyBar] = []
        now_iso = datetime.utcnow().isoformat()
        for _, row in df.iterrows():
            try:
                d_date = str(row["日期"])
                open_p = float(row["开盘"])
                close_p = float(row["收盘"])
                high_p = float(row["最高"])
                low_p = float(row["最低"])
                # AKShare volume is reported in 手 (lots)
                vol_lots = float(row["成交量"])
                amt_yuan = float(row["成交额"])
                turnover = float(row["换手率"]) if "换手率" in row and row["换手率"] is not None else None
                chg_pct = float(row["涨跌幅"]) if "涨跌幅" in row and row["涨跌幅"] is not None else None
                amp = float(row["振幅"]) if "振幅" in row and row["振幅"] is not None else None

                bars.append(DailyBar(
                    symbol=sym,
                    trade_date=d_date,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol_lots,
                    amount=amt_yuan,
                    source="akshare",
                    adjust_type=adjust_type,
                    updated_at=now_iso,
                    is_final=True,
                    raw_open=open_p,
                    raw_high=high_p,
                    raw_low=low_p,
                    raw_close=close_p,
                    adjustment_factor=1.0,
                    turnover_rate=turnover,
                    amplitude_pct=amp,
                    change_pct=chg_pct,
                    is_suspended=False,
                    is_st=False
                ))
            except (ValueError, TypeError, KeyError):
                continue

        return bars

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Fetch index daily bars via akshare stock_zh_index_daily_em."""
        import akshare as ak

        sym = normalize_symbol(index_code)
        clean = to_clean_code(sym)
        with self._lock:
            # For 000300, index code in em is sh000300
            em_code = sym
            try:
                df = ak.stock_zh_index_daily_em(symbol=em_code)
            except Exception:
                df = ak.stock_zh_index_daily(symbol=em_code)

        if df is None or df.empty:
            return []

        bars: List[DailyBar] = []
        now_iso = datetime.utcnow().isoformat()
        start = start_date or "1990-01-01"
        end = end_date or "9999-12-31"

        for _, row in df.iterrows():
            try:
                d_date = str(row.get("date") or row.get("日期"))
                if not (start <= d_date <= end):
                    continue
                open_p = float(row.get("open") or row.get("开盘"))
                close_p = float(row.get("close") or row.get("收盘"))
                high_p = float(row.get("high") or row.get("最高"))
                low_p = float(row.get("low") or row.get("最低"))
                vol = float(row.get("volume") or row.get("成交量", 0.0))
                amt = float(row.get("amount") or row.get("成交额", 0.0))

                bars.append(DailyBar(
                    symbol=sym,
                    trade_date=d_date,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol,
                    amount=amt,
                    source="akshare",
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
            except (ValueError, TypeError, KeyError):
                continue

        return bars

    def fetch_stock_list(self) -> List[StockInfo]:
        """Fetch all A-share stocks from AKShare."""
        import akshare as ak
        with self._lock:
            df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            return []
        stocks = []
        now_iso = datetime.utcnow().isoformat()
        for _, row in df.iterrows():
            code = str(row["code"])
            name = str(row["name"])
            stocks.append(StockInfo(
                symbol=normalize_symbol(code),
                name=name,
                status="L",
                is_st=("ST" in name.upper()),
                updated_at=now_iso
            ))
        return stocks

    def fetch_industry_map(self) -> Dict[str, str]:
        """Fetch industry boards mapping if available."""
        return {}
