# src/astock/data/providers/akshare_provider.py
import logging
import threading
import subprocess
import sys
import json
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

    def __init__(self, timeout: float = 30.0, isolated: bool = True):
        self._name = "akshare"
        self._lock = threading.Lock()
        self._timeout = timeout
        self._isolated = isolated

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
        if adjust_type not in ("none", "qfq", "hfq"):
            raise ValueError("Unsupported AKShare adjustment")
        if self._isolated:
            return [DailyBar.from_dict(row) for row in self._invoke(
                "fetch_stock_daily", [symbol, start_date, end_date, adjust_type])]
        import akshare as ak

        sym = normalize_symbol(symbol)
        clean = to_clean_code(sym)
        s_date = start_date.replace("-", "")
        e_date = end_date.replace("-", "")
        adj = "qfq" if adjust_type == "qfq" else ("hfq" if adjust_type == "hfq" else "")

        with self._lock:
            raw_df = ak.stock_zh_a_hist(symbol=clean, period="daily", start_date=s_date,
                                        end_date=e_date, adjust="")
            if raw_df is None or raw_df.empty:
                return []
            adj_df = raw_df if not adj else ak.stock_zh_a_hist(
                symbol=clean, period="daily", start_date=s_date, end_date=e_date, adjust=adj)
        if adj_df is None or adj_df.empty:
            raise ValueError("AKShare adjusted history missing")
        raw = {str(row["日期"]): row for _, row in raw_df.iterrows()}
        adjusted = {str(row["日期"]): row for _, row in adj_df.iterrows()}
        if set(raw) != set(adjusted):
            raise ValueError("AKShare raw/adjusted history dates do not align")
        bars = []
        now_iso = datetime.utcnow().isoformat()
        for day in sorted(raw):
            row, a = raw[day], adjusted[day]
            ro, rh, rl, rc = [float(row[k]) for k in ("开盘", "最高", "最低", "收盘")]
            ao, ah, al, ac = [float(a[k]) for k in ("开盘", "最高", "最低", "收盘")]
            if min(ro, rh, rl, rc, ao, ah, al, ac) <= 0:
                continue
            bars.append(DailyBar(
                symbol=sym, trade_date=day, open=ao, high=ah, low=al, close=ac,
                volume=float(row["成交量"]), amount=float(row["成交额"]), source="akshare",
                adjust_type=adjust_type, updated_at=now_iso, is_final=True,
                raw_open=ro, raw_high=rh, raw_low=rl, raw_close=rc,
                adjustment_factor=ac/rc,
                turnover_rate=float(row["换手率"]) if row.get("换手率") is not None else None,
                amplitude_pct=float(row["振幅"]) if row.get("振幅") is not None else None,
                change_pct=float(row["涨跌幅"]) if row.get("涨跌幅") is not None else None,
                is_suspended=float(row["成交量"]) == 0, is_st=False))
        return bars

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Fetch index daily bars via akshare stock_zh_index_daily_em."""
        if self._isolated:
            return [DailyBar.from_dict(row) for row in self._invoke(
                "fetch_index_daily", [index_code, start_date, end_date])]
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
        if self._isolated:
            return [StockInfo(**row) for row in self._invoke("fetch_stock_list", [])]
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

    def _invoke(self, method, args):
        # AKShare wrappers do not consistently expose HTTP timeouts. A killed
        # subprocess bounds the entire request, including retries inside the SDK.
        script = (
            "import contextlib,io,json,sys; from dataclasses import asdict; "
            f"from {__name__} import AKShareProvider; "
            "provider=AKShareProvider(isolated=False); request=json.loads(sys.argv[1]); "
            "capture=io.StringIO(); "
            "\nwith contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):"
            "\n result=getattr(provider,request['method'])(*request['args'])"
            "\nprint(json.dumps([asdict(x) for x in result],allow_nan=False))"
        )
        with self._lock:
            try:
                result = subprocess.run([sys.executable, "-c", script,
                    json.dumps({"method": method, "args": args})],
                    capture_output=True, text=True, timeout=self._timeout, check=True)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("AKShare worker deadline exceeded") from exc
        return json.loads(result.stdout)

    def fetch_industry_map(self) -> Dict[str, str]:
        """Fetch industry boards mapping if available."""
        return {}
