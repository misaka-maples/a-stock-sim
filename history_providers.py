"""Independent optional daily-history providers and bounded failover.

SourceRouter.download(symbol, start, end, adjustment='qfq', legacy_download=None)
returns the HistoricalDataFeed four-tuple. ``status()`` contains no credentials.
Optional config: TUSHARE_TOKEN or data/private/market_data.json {"tushare_token":...}.
BaoStock is optional (tested SDK 0.9.4); a reusable isolated process keeps one
SDK login, with a hard deadline per request because its socket has no deadline.
No fabricated bars, credentials in URLs, proxy bypass, or WAF workarounds.
"""
import atexit
import contextlib
import datetime as dt
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import threading
import time

import requests


class ProviderUnavailable(ValueError):
    """Sanitized provider error; never contains remote response bodies or secrets."""


class HistoryNotCurrent(ProviderUnavailable):
    """Asset-specific missing/stale history; does not trip a global circuit."""


class ProviderBusy(ProviderUnavailable):
    """Local bounded queue contention, not evidence of a remote outage."""


class CircuitBreaker:
    def __init__(self, failures=3, cooldown=300, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = failures, cooldown, clock
        self._states = {}
        self._lock = threading.Lock()

    def call(self, provider, callback):
        with self._lock:
            state = self._states.setdefault(provider, {"failures": 0, "until": 0, "probe": False, "last_error": None})
            now = self.clock()
            if state["until"] > now or state["probe"]:
                raise ProviderUnavailable("circuit_open")
            probe = state["until"] != 0
            if probe:
                state["probe"] = True
        try:
            value = callback()
        except ProviderBusy:
            with self._lock:
                state["probe"] = False
            raise
        except HistoryNotCurrent:
            with self._lock:
                state["probe"] = False
                # A successful protocol response proves connectivity even when
                # this particular delisted/suspended asset has no current bar.
                state.update(failures=0, until=0, last_error=None)
            raise
        except Exception as exc:
            # Error class only: exception strings can contain request tokens.
            with self._lock:
                state["probe"] = False
                state["failures"] += 1
                state["last_error"] = type(exc).__name__
                if probe or state["failures"] >= self.threshold:
                    state["until"] = self.clock() + self.cooldown
            raise ProviderUnavailable(type(exc).__name__) from None
        with self._lock:
            state.update(failures=0, until=0, probe=False, last_error=None)
        return value

    def status(self):
        with self._lock:
            now = self.clock()
            return {name: {"failures": s["failures"], "state": "half_open" if s["probe"] else "open" if s["until"] > now else "closed", "retry_after_seconds": max(0, round(s["until"] - now)), "last_error": s["last_error"]} for name, s in self._states.items()}


def _number(value, positive=False):
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ProviderUnavailable("invalid_number")
    return value


def _symbol(symbol):
    if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
        raise ProviderUnavailable("invalid_symbol")
    return symbol


def _day(value):
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return dt.date.fromisoformat(text).isoformat()


def _bar(raw, adjusted, source, date, pre_close=None):
    out = {"date": _day(date), "source": source}
    for key in ("open", "high", "low", "close"):
        out[key] = _number(adjusted[key], positive=True)
        out["raw_" + key] = _number(raw[key], positive=True)
    if out["high"] < max(out["open"], out["close"], out["low"]) - 1e-8 or out["low"] > min(out["open"], out["close"], out["high"]) + 1e-8:
        raise ProviderUnavailable("invalid_ohlc")
    if out["raw_high"] < max(out["raw_open"], out["raw_close"], out["raw_low"]) - 1e-8 or out["raw_low"] > min(out["raw_open"], out["raw_close"], out["raw_high"]) + 1e-8:
        raise ProviderUnavailable("invalid_raw_ohlc")
    out["volume"] = _number(raw["volume"])
    out["amount"] = _number(raw.get("amount", 0))
    if out["volume"] < 0 or out["amount"] < 0:
        raise ProviderUnavailable("negative_activity")
    out["adjustment_factor"] = out["close"] / out["raw_close"]
    if pre_close not in (None, ""):
        out["raw_pre_close"] = _number(pre_close, positive=True)
        out["raw_pre_close_source"] = "provider_pre_close"
    return out


def normalize_baostock(raw_rows, adjusted_rows, adjustment="qfq"):
    """BaoStock volume is shares; amount yuan; preclose is exchange reference."""
    raws = {_day(r["date"]): r for r in raw_rows}
    adjusted = {_day(r["date"]): r for r in adjusted_rows}
    if set(raws) != set(adjusted):
        raise ProviderUnavailable("raw_adjusted_dates_mismatch")
    result = []
    for date in sorted(raws):
        raw = dict(raws[date])
        adj = adjusted[date] if adjustment == "qfq" else raw
        # Some suspended rows have blank OHLC. Preserve real zero-volume rows
        # with prices, but do not manufacture a tradable observation for blanks.
        if raw.get("tradestatus") == "0" and any(raw.get(k) in (None, "") for k in ("open", "close", "high", "low")):
            continue
        raw["volume"] = _number(raw["volume"]) / 100
        bar = _bar(raw, adj, "baostock", date, raw.get("preclose"))
        # Dated provider flag, never infer non-ST from an empty/missing field.
        if raw.get("isST") in ("0", "1"):
            bar["is_st"] = raw["isST"] == "1"
        result.append(bar)
    return result


def normalize_tushare(raw_rows, factor_rows, adjustment="qfq"):
    """Tushare daily vol is lots, amount thousand yuan; factor is cumulative."""
    raws = {_day(r["trade_date"]): r for r in raw_rows}
    factors = {_day(r["trade_date"]): _number(r["adj_factor"], True) for r in factor_rows}
    if adjustment == "qfq" and not set(raws).issubset(factors):
        raise ProviderUnavailable("missing_adjustment_factor")
    anchor = factors[max(raws)] if raws and adjustment == "qfq" else 1
    result = []
    for date in sorted(raws):
        raw = dict(raws[date])
        raw["volume"] = raw["vol"]
        raw["amount"] = _number(raw["amount"]) * 1000
        scale = factors[date] / anchor if adjustment == "qfq" else 1
        adj = {key: _number(raw[key], True) * scale for key in ("open", "high", "low", "close")}
        result.append(_bar(raw, adj, "tushare", date, raw.get("pre_close")))
    return result


def _bao_query(sdk, code, start, end, flag):
    fields = "date,open,high,low,close,preclose,volume,amount,tradestatus"
    # BaoStock index rows do not support isST. Request the dated field only
    # for the Shanghai/Shenzhen mainboard stocks this strategy can trade.
    if re.fullmatch(r"(?:sh\.60\d{4}|sz\.00\d{4})", code):
        fields += ",isST"
    result = sdk.query_history_k_data_plus(code, fields, start_date=start, end_date=end, frequency="d", adjustflag=flag)
    if result.error_code != "0":
        raise ProviderUnavailable("query_failed")
    rows = []
    while result.next():
        rows.append(dict(zip(result.fields, result.get_row_data())))
    if result.error_code != "0":
        raise ProviderUnavailable("query_incomplete")
    return rows


def _baostock_worker(symbol, start, end, adjustment):
    # Run only inside the isolated worker. SDK prints login and remote errors;
    # swallow these entirely, emitting only the JSON normalized data protocol.
    import baostock as sdk
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        login = sdk.login()
        if login.error_code != "0":
            raise ProviderUnavailable("login_failed")
        try:
            code = symbol[:2] + "." + symbol[2:]
            raw = _bao_query(sdk, code, start, end, "3")
            adjusted = _bao_query(sdk, code, start, end, "2") if adjustment == "qfq" else raw
            return normalize_baostock(raw, adjusted, adjustment)
        finally:
            sdk.logout()


def _baostock_stream():
    """JSON-lines worker. One login/session; SDK chatter never hits protocol."""
    protocol = sys.stdout
    sdk = None
    logged_in = False
    with open(os.devnull, "w") as quiet:
        try:
            with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                import baostock as sdk
                if sdk.login().error_code != "0":
                    return 1
                logged_in = True
            for line in sys.stdin:
                request_id = None
                try:
                    request = json.loads(line)
                    request_id = request["id"]
                    symbol, start, end, adjustment = request["args"]
                    _symbol(symbol)
                    start, end = _day(start), _day(end)
                    if start > end or adjustment not in ("qfq", "none"):
                        raise ProviderUnavailable("invalid_request")
                    with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                        code = symbol[:2] + "." + symbol[2:]
                        raw = _bao_query(sdk, code, start, end, "3")
                        adjusted = _bao_query(sdk, code, start, end, "2") if adjustment == "qfq" else raw
                        bars = normalize_baostock(raw, adjusted, adjustment)
                    protocol.write(json.dumps({"id": request_id, "bars": bars}, allow_nan=False) + "\n")
                    protocol.flush()
                except Exception:
                    protocol.write(json.dumps({"id": request_id, "error": "query_failed"}) + "\n")
                    protocol.flush()
                    # Failed SDK protocol state must not contaminate next call.
                    return 1
            return 0
        except Exception:
            return 1
        finally:
            if sdk is not None and logged_in:
                with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                    try:
                        sdk.logout()
                    except Exception:
                        pass


_BAO_LOCK = threading.Lock()


class SourceRouter:
    def __init__(self, base_dir=None, timeout=6, *, circuit=None, session=None, baostock_stream=True):
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent)
        self.timeout = max(1, min(float(timeout), 60))
        self.circuit = circuit or CircuitBreaker()
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._token = os.environ.get("TUSHARE_TOKEN", "").strip()
        if not self._token:
            try:
                config = json.loads((self.base_dir / "data/private/market_data.json").read_text())
                self._token = str(config.get("tushare_token", "")).strip()
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self._bao_available = importlib.util.find_spec("baostock") is not None
        self._bao_stream = baostock_stream
        self._bao_process = None
        self._bao_request_id = 0
        atexit.register(self.close)

    def status(self):
        return {"tushare": {"configured": bool(self._token)}, "baostock": {"installed": self._bao_available}, "circuits": self.circuit.status()}

    def _tushare_api(self, api, params, fields):
        response = self.session.post("https://api.tushare.pro", json={"api_name": api, "token": self._token, "params": params, "fields": fields}, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0 or not isinstance(data.get("data"), dict):
            raise ProviderUnavailable("api_rejected")
        payload = data["data"]
        if not isinstance(payload.get("fields"), list) or not isinstance(payload.get("items"), list):
            raise ProviderUnavailable("invalid_payload")
        return [dict(zip(payload["fields"], row)) for row in payload["items"]]

    def _tushare(self, symbol, start, end, adjustment):
        code = symbol[2:] + "." + symbol[:2].upper()
        # Paginate time windows under the daily API row limit; exact factor-date
        # validation rejects permissions failures and truncation rather than
        # returning a plausible but incorrectly adjusted history.
        rows, factors = [], []
        left, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        is_index = adjustment == "none" and symbol in ("sh000300", "sh000001", "sz399001", "sz399006")
        while left <= last:
            right = min(last, left + dt.timedelta(days=1500))
            params = {"ts_code": code, "start_date": left.strftime("%Y%m%d"), "end_date": right.strftime("%Y%m%d")}
            rows.extend(self._tushare_api("index_daily" if is_index else "daily", params, "trade_date,open,high,low,close,pre_close,vol,amount"))
            if adjustment == "qfq":
                factors.extend(self._tushare_api("adj_factor", params, "trade_date,adj_factor"))
            left = right + dt.timedelta(days=1)
        return normalize_tushare(rows, factors, adjustment)

    def _stop_bao_worker(self, graceful=False):
        process, self._bao_process = self._bao_process, None
        if process is None:
            return
        # Close input to request a normal logout, but never wait indefinitely
        # for an SDK blocked inside recv. Kill/reap it after a bounded grace.
        try:
            if process.stdin:
                process.stdin.close()
            if graceful and process.poll() is None:
                try:
                    process.wait(timeout=.25)
                except subprocess.TimeoutExpired:
                    pass
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=.5)
        finally:
            for pipe in (process.stdout, process.stderr):
                if pipe:
                    pipe.close()

    def close(self):
        if not _BAO_LOCK.acquire(timeout=self.timeout):
            return
        try:
            self._stop_bao_worker(graceful=True)
        finally:
            _BAO_LOCK.release()

    def _baostock_once(self, symbol, start, end, adjustment):
        """Bounded one-shot protocol retained for standalone diagnostics."""
        command = [sys.executable, str(Path(__file__).resolve()), "--baostock-worker", symbol, start, end, adjustment]
        result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        if result.returncode:
            raise ProviderUnavailable("worker_failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise ProviderUnavailable("invalid_worker_result")
        return payload

    def _baostock(self, symbol, start, end, adjustment):
        # Serialize a persistent session, with bounded queueing as well as I/O.
        if not _BAO_LOCK.acquire(timeout=self.timeout):
            raise ProviderBusy("busy")
        try:
            if not self._bao_stream:
                return self._baostock_once(symbol, start, end, adjustment)
            deadline = time.monotonic() + self.timeout
            if self._bao_process is None or self._bao_process.poll() is not None:
                self._stop_bao_worker()
                command = [sys.executable, "-u", str(Path(__file__).resolve()), "--baostock-stream"]
                self._bao_process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                     stderr=subprocess.DEVNULL, bufsize=0)
                os.set_blocking(self._bao_process.stdin.fileno(), False)
                os.set_blocking(self._bao_process.stdout.fileno(), False)
            process = self._bao_process
            self._bao_request_id += 1
            request_id = self._bao_request_id
            request = (json.dumps({"id": request_id, "args": [symbol, start, end, adjustment]}) + "\n").encode()
            # Tiny fixed-size request fits PIPE_BUF; never allow a blocking write.
            if os.write(process.stdin.fileno(), request) != len(request):
                raise ProviderUnavailable("worker_write_failed")
            response = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ProviderUnavailable("worker_timeout")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise ProviderUnavailable("worker_closed")
                    response.extend(chunk)
                    if len(response) > 16 * 1024 * 1024:
                        raise ProviderUnavailable("worker_response_too_large")
                    if b"\n" in response:
                        line, extra = bytes(response).split(b"\n", 1)
                        if extra:
                            raise ProviderUnavailable("worker_protocol_noise")
                        payload = json.loads(line)
                        if not isinstance(payload, dict) or payload.get("id") != request_id or not isinstance(payload.get("bars"), list):
                            raise ProviderUnavailable("invalid_worker_result")
                        return payload["bars"]
        except Exception:
            self._stop_bao_worker()
            raise
        finally:
            _BAO_LOCK.release()

    @staticmethod
    def _validated(provider, callback, start, end):
        bars = callback()
        if not bars:
            raise HistoryNotCurrent("empty_history")
        if any(not start <= b["date"] <= end for b in bars):
            raise ProviderUnavailable("out_of_range")
        from trade_calendar import is_trading_day, get_previous_trading_day
        expected = end if is_trading_day(end) else get_previous_trading_day(end).isoformat()
        if max(b["date"] for b in bars) != expected:
            raise HistoryNotCurrent("stale_history")
        return bars

    def download(self, symbol, start, end, adjustment="qfq", legacy_download=None):
        _symbol(symbol)
        start, end = _day(start), _day(end)
        if start > end or adjustment not in ("qfq", "none"):
            raise ValueError("invalid_history_request")
        warnings = []
        providers = []
        if self._token:
            providers.append(("tushare", self._tushare))
        if self._bao_available and symbol[:2] in ("sh", "sz"):
            providers.append(("baostock", self._baostock))
        for name, callback in providers:
            try:
                bars = self.circuit.call(name, lambda cb=callback, p=name: self._validated(p, lambda: cb(symbol, start, end, adjustment), start, end))
                return symbol[2:], bars, name, warnings
            except ProviderUnavailable as exc:
                warnings.append(name + "_" + str(exc))
        if legacy_download is not None:
            name, bars, source, errors = legacy_download(symbol, start, end, adjustment)
            return name, bars, source, warnings + errors
        return symbol[2:], [], "unavailable", warnings


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--baostock-stream":
        sys.exit(_baostock_stream())
    elif len(sys.argv) == 6 and sys.argv[1] == "--baostock-worker":
        try:
            result = _baostock_worker(*sys.argv[2:])
            sys.stdout.write(json.dumps(result, allow_nan=False))
        except Exception:
            sys.exit(1)
    else:
        sys.exit(2)
