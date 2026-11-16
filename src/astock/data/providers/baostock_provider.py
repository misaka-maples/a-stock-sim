# src/astock/data/providers/baostock_provider.py
import contextlib
import io
import json
import logging
import os
import sys
import threading
import atexit
import subprocess
import select
import time
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

    def __init__(self, timeout: float = 30.0, isolated: bool = True):
        self._name = "baostock"
        self._lock = threading.Lock()
        self._bs = None
        self._logged_in = False
        self._timeout = timeout
        self._isolated = isolated
        self._process = None
        atexit.register(self.close)

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
        if self._isolated and self._bs is None:
            try:
                return bool(self._worker_request("is_healthy", []))
            except Exception:
                return False
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
        if adjust_type not in ("none", "qfq", "hfq"):
            raise ValueError("Unsupported BaoStock adjustment")
        sym = normalize_symbol(symbol)
        code = to_baostock_code(sym)
        flag = "2" if adjust_type == "qfq" else ("1" if adjust_type == "hfq" else "3")

        if self._isolated and self._bs is None:
            payload = self._worker_request("fetch_stock_daily", [symbol, start_date, end_date, adjust_type])
            return [DailyBar.from_dict(row) for row in payload]
        with self._lock:
            self._ensure_login()
            fields = "date,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
            def query(adjustflag):
                res = self._bs.query_history_k_data_plus(
                    code, fields, start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag=adjustflag)
                if res.error_code != "0":
                    if res.error_code in ("10001001", "10001011"):
                        self._logged_in = False
                    raise RuntimeError(f"BaoStock query error {res.error_code}")
                rows = {}
                while res.next():
                    row = dict(zip(res.fields, res.get_row_data()))
                    rows[row["date"]] = row
                return rows
            raw = query("3")
            adjusted = raw if flag == "3" else query(flag)
            if set(raw) != set(adjusted):
                raise ValueError("BaoStock raw/adjusted history dates do not align")
            bars = []
            now_iso = datetime.utcnow().isoformat()
            for day in sorted(raw):
                row, adj = raw[day], adjusted[day]
                try:
                    ro, rh, rl, rc = [float(row.get(k) or 0) for k in ("open", "high", "low", "close")]
                    ao, ah, al, ac = [float(adj.get(k) or 0) for k in ("open", "high", "low", "close")]
                    # Blank suspended observations cannot authorize invented prices.
                    if min(ro, rh, rl, rc, ao, ah, al, ac) <= 0:
                        continue
                    bars.append(DailyBar(
                        symbol=sym, trade_date=day, open=ao, high=ah, low=al, close=ac,
                        volume=float(row.get("volume") or 0)/100.0,
                        amount=float(row.get("amount") or 0), source="baostock",
                        adjust_type=adjust_type, updated_at=now_iso, is_final=True,
                        raw_open=ro, raw_high=rh, raw_low=rl, raw_close=rc,
                        raw_pre_close=float(row["preclose"]) if row.get("preclose") else None,
                        adjustment_factor=ac/rc,
                        turnover_rate=float(row["turn"]) if row.get("turn") else None,
                        change_pct=float(row["pctChg"]) if row.get("pctChg") else None,
                        is_suspended=row.get("tradestatus")=="0", is_st=row.get("isST")=="1"))
                except (ValueError, TypeError, KeyError):
                    continue
            return bars

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Fetch daily bars for benchmark index (e.g. '000300' or 'sh000300')."""
        if self._isolated and self._bs is None:
            payload = self._worker_request("fetch_index_daily", [index_code, start_date, end_date])
            return [DailyBar.from_dict(row) for row in payload]
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

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=1)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()

    def _worker_request(self, method, args):
        # BaoStock's SDK has a blocking global socket. Confine it to a process
        # which can actually be killed on timeout; a Future timeout is insufficient.
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self.close()
                module = __name__
                command = [sys.executable, "-u", "-c",
                           f"from {module} import _stdio_worker; _stdio_worker()"]
                self._process = subprocess.Popen(command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                os.set_blocking(self._process.stdout.fileno(), False)
            process = self._process
            deadline = time.monotonic() + self._timeout
            try:
                request = (json.dumps({"method": method, "args": args}) + "\n").encode()
                process.stdin.write(request)
                response = bytearray()
                while b"\n" not in response:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                        raise TimeoutError("BaoStock worker deadline exceeded")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("BaoStock worker exited")
                    response.extend(chunk)
                    if len(response)>50_000_000:
                        raise RuntimeError("BaoStock worker response too large")
                payload = json.loads(response.split(b"\n",1)[0])
                if "error" in payload:
                    raise RuntimeError("BaoStock worker: " + payload["error"])
                return payload["result"]
            except Exception:
                self.close()
                raise

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
        if self._isolated and self._bs is None:
            return self._worker_request("fetch_industry_map", [])
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


def _stdio_worker():
    """Private persistent SDK process; only structured replies reach stdout."""
    provider = BaoStockProvider(isolated=False)
    allowed = {"is_healthy", "fetch_stock_daily", "fetch_index_daily", "fetch_industry_map"}
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request["method"] not in allowed:
                    raise ValueError("invalid worker method")
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = getattr(provider, request["method"])(*request["args"])
                if isinstance(result, list):
                    result = [bar.to_dict() for bar in result]
                reply = {"result": result}
            except Exception as exc:
                reply = {"error": type(exc).__name__}
            sys.stdout.write(json.dumps(reply, allow_nan=False) + "\n")
            sys.stdout.flush()
    finally:
        provider._safe_logout()
