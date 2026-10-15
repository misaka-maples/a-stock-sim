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
import fcntl
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
import tempfile
import select
import requests
from market_request_budget import (MarketRequestBudget, RequestBudgetExceeded,
                                   RequestQueueBusy, RequestBudgetUnavailable)


class ProviderUnavailable(ValueError):
    """Sanitized provider error; never contains remote response bodies or secrets."""


class HistoryNotCurrent(ProviderUnavailable):
    """Asset-specific missing/stale history; does not trip a global circuit."""


class ProviderBusy(ProviderUnavailable):
    """Local bounded queue contention, not evidence of a remote outage."""


ASSET_ERROR_CODES = frozenset({"invalid_number", "invalid_ohlc", "invalid_raw_ohlc", "negative_activity", "raw_adjusted_dates_mismatch", "missing_adjustment_factor", "empty_history", "stale_history"})
SAFE_ERROR_CODES = ASSET_ERROR_CODES | frozenset({"busy", "circuit_open", "provider_error", "api_rejected", "invalid_payload", "invalid_symbol", "invalid_request", "out_of_range", "query_failed", "query_incomplete", "login_failed", "worker_failed", "worker_write_failed", "worker_timeout", "worker_closed", "worker_response_too_large", "worker_protocol_noise", "invalid_worker_result", "provider_restricted", "provider_state_unavailable", "daily_limit", "untracked_prior_usage", "queue_busy", "rate_wait", "invalid_state", "state_unavailable"})
LOCAL_ADMISSION_CODES = frozenset({"busy", "daily_limit", "untracked_prior_usage", "queue_busy", "rate_wait", "invalid_state", "state_unavailable", "provider_restricted", "provider_state_unavailable"})
SAFE_EXCEPTION_NAMES = frozenset({"RuntimeError", "ValueError", "TypeError", "KeyError", "AttributeError", "TimeoutExpired", "Timeout", "ReadTimeout", "ConnectTimeout", "ConnectionError", "HTTPError", "OSError", "JSONDecodeError", "BrokenPipeError", "BlockingIOError"})


def _safe_error_code(exc):
    # Only constants generated here or a numeric SDK code can enter diagnostics.
    # Never expose arbitrary exception strings, SDK messages or response bodies.
    if isinstance(exc, ProviderUnavailable):
        code = str(exc)
        if code in SAFE_ERROR_CODES or code in SAFE_EXCEPTION_NAMES or re.fullmatch(r"sdk_error_-?\d{1,8}", code):
            return code
    return type(exc).__name__ if type(exc).__name__ in SAFE_EXCEPTION_NAMES else "provider_error"


def _sdk_error_code(value, fallback="query_failed"):
    text = str(value)
    return "sdk_error_" + text if re.fullmatch(r"-?\d{1,8}", text) else fallback


class CircuitBreaker:
    def __init__(self, failures=3, cooldown=300, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = failures, cooldown, clock
        self._states = {}
        self._lock = threading.Lock()

    def call(self, provider, callback):
        with self._lock:
            state = self._states.setdefault(provider, {"failures": 0, "until": 0, "probe": False, "last_error": None, "last_error_code": None})
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
                state["last_error_code"] = _safe_error_code(exc)
                if probe or state["failures"] >= self.threshold:
                    state["until"] = self.clock() + self.cooldown
            raise ProviderUnavailable(_safe_error_code(exc)) from None
        with self._lock:
            state.update(failures=0, until=0, probe=False, last_error=None)
        return value

    def status(self):
        with self._lock:
            now = self.clock()
            return {name: {"failures": s["failures"], "state": "half_open" if s["probe"] else "open" if s["until"] > now else "closed", "retry_after_seconds": max(0, round(s["until"] - now)), "last_error": s["last_error"], "last_error_code": s["last_error_code"]} for name, s in self._states.items()}


def _number(value, positive=False):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise HistoryNotCurrent("invalid_number") from None
    if not math.isfinite(value) or (positive and value <= 0):
        raise HistoryNotCurrent("invalid_number")
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
        raise HistoryNotCurrent("invalid_ohlc")
    if out["raw_high"] < max(out["raw_open"], out["raw_close"], out["raw_low"]) - 1e-8 or out["raw_low"] > min(out["raw_open"], out["raw_close"], out["raw_high"]) + 1e-8:
        raise HistoryNotCurrent("invalid_raw_ohlc")
    out["volume"] = _number(raw["volume"])
    out["amount"] = _number(raw.get("amount", 0))
    if out["volume"] < 0 or out["amount"] < 0:
        raise HistoryNotCurrent("negative_activity")
    out["adjustment_factor"] = out["close"] / out["raw_close"]
    if pre_close not in (None, ""):
        try:
            out["raw_pre_close"] = _number(pre_close, positive=True)
            out["raw_pre_close_source"] = "provider_pre_close"
        except HistoryNotCurrent:
            # IPO/blank/zero exchange reference is unknown, not corrupt OHLC.
            # Never substitute a fabricated previous close or limit price.
            pass
    return out


def normalize_baostock(raw_rows, adjusted_rows, adjustment="qfq"):
    """BaoStock volume is shares; amount yuan; preclose is exchange reference."""
    raws = {_day(r["date"]): r for r in raw_rows}
    adjusted = {_day(r["date"]): r for r in adjusted_rows}
    if set(raws) != set(adjusted):
        raise HistoryNotCurrent("raw_adjusted_dates_mismatch")
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
        raise HistoryNotCurrent("missing_adjustment_factor")
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
        raise ProviderUnavailable(_sdk_error_code(result.error_code))
    rows = []
    while result.next():
        rows.append(dict(zip(result.fields, result.get_row_data())))
    if result.error_code != "0":
        raise ProviderUnavailable(_sdk_error_code(result.error_code, "query_incomplete"))
    return rows


def _acquire_worker_lease():
    path = Path(os.environ.get("QUANT_BAO_LEASE_PATH", str(Path.home() / ".cache/openarmx-quant/baostock.lock")))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = open(path, "a+b")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ProviderBusy("busy") from None
    return handle


def _install_bao_budget():
    """Instrument the SDK packet boundary, including login/logout and pages."""
    import baostock.util.socketutil as transport
    budget = MarketRequestBudget()
    try:
        state = budget.initialize("baostock")
    except (RequestBudgetExceeded, RequestQueueBusy, RequestBudgetUnavailable) as exc:
        raise ProviderBusy(exc.code) from None
    if state.get("reason") in ("untracked_prior_usage", "daily_limit"):
        raise ProviderBusy(state["reason"])
    # Parent and one-shot workers both enforce the durable provider freeze
    # before sdk.login() can even create a socket.
    base = Path(os.environ.get("QUANT_MARKET_BASE_DIR", str(Path(__file__).resolve().parent)))
    state_path = base / "data/private/market_provider_state.json"
    if state_path.exists():
        try:
            until = float(json.loads(state_path.read_text()).get("baostock", {}).get("blocked_until", 0))
            if not math.isfinite(until):
                raise ValueError
        except (OSError, ValueError, TypeError, AttributeError):
            raise ProviderBusy("provider_state_unavailable") from None
        if until > time.time():
            raise ProviderBusy("provider_restricted")
    original = transport.send_msg
    if getattr(original, "_quant_budgeted", False) is True:
        return
    def guarded(message):
        try:
            with budget.request("baostock"):
                response = original(message)
                if response is None:
                    raise ProviderUnavailable("query_failed")
                return response
        except (RequestBudgetExceeded, RequestQueueBusy, RequestBudgetUnavailable) as exc:
            raise ProviderBusy(exc.code) from None
    guarded._quant_budgeted = True
    transport.send_msg = guarded


def _baostock_worker(symbol, start, end, adjustment):
    # Run only inside the isolated worker. SDK prints login and remote errors;
    # swallow these entirely, emitting only the JSON normalized data protocol.
    import baostock as sdk
    _install_bao_budget()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        login = sdk.login()
        if login.error_code != "0":
            raise ProviderUnavailable(_sdk_error_code(login.error_code, "login_failed"))
        may_logout = True
        try:
            code = symbol[:2] + "." + symbol[2:]
            raw = _bao_query(sdk, code, start, end, "3")
            adjusted = _bao_query(sdk, code, start, end, "2") if adjustment == "qfq" else raw
            return normalize_baostock(raw, adjusted, adjustment)
        except ProviderUnavailable as exc:
            if str(exc) == "sdk_error_10001011":
                may_logout = False
            raise
        finally:
            if may_logout:
                sdk.logout()


def _baostock_stream():
    """JSON-lines worker. One login/session; SDK chatter never hits protocol."""
    protocol = sys.stdout
    sdk = None
    logged_in = False
    lease = None
    with open(os.devnull, "w") as quiet:
        try:
            lease = _acquire_worker_lease()
            with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                import baostock as sdk
                _install_bao_budget()
                login = sdk.login()
                if login.error_code != "0":
                    # Startup happens before reading a request. Emit a bounded,
                    # sanitized handshake error so parent can distinguish an
                    # explicit login refusal from an unexplained dead process.
                    protocol.write(json.dumps({"id": None, "phase": "login", "error": _sdk_error_code(login.error_code, "login_failed"), "asset_error": False}) + "\n")
                    protocol.flush()
                    return 1
                logged_in = True
            while True:
                # Release the SDK session/OS lease after five idle seconds, so
                # another research feed can acquire the single allowed login.
                if not select.select([sys.stdin], [], [], 5)[0]:
                    break
                line = sys.stdin.readline()
                if not line:
                    break
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
                except Exception as exc:
                    code = _safe_error_code(exc)
                    if code == "sdk_error_10001011":
                        logged_in = False
                    protocol.write(json.dumps({"id": request_id, "error": code, "asset_error": code in ASSET_ERROR_CODES}) + "\n")
                    protocol.flush()
                    # Failed SDK protocol state must not contaminate next call.
                    return 1
            return 0
        except Exception as exc:
            protocol.write(json.dumps({"id": None, "phase": "login", "error": _safe_error_code(exc), "asset_error": False}) + "\n")
            protocol.flush()
            return 1
        finally:
            if sdk is not None and logged_in:
                with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                    try:
                        sdk.logout()
                    except Exception:
                        pass
            if lease is not None:
                lease.close()


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
        self._budget = MarketRequestBudget()
        self._daily_memory = {}
        self._bao_stream = baostock_stream
        self._bao_process = None
        self._bao_request_id = 0
        self._failure_lock = threading.Lock()
        self._failure_diagnostics = {}
        self._provider_state_path = self.base_dir / "data/private/market_provider_state.json"
        self._bao_lease_path = Path.home() / ".cache/openarmx-quant/baostock.lock"
        atexit.register(self.close)

    def status(self):
        with self._failure_lock:
            diagnostics = {name: dict(info, counts_by_code=dict(info["counts_by_code"])) for name, info in self._failure_diagnostics.items()}
        try:
            restriction = self._bao_restriction()
        except ProviderUnavailable:
            restriction = {"state_error": "provider_state_unavailable"}
        circuits = self.circuit.status()
        if restriction.get("retry_after_seconds", 0):
            current = circuits.setdefault("baostock", {"failures": 0, "last_error": None})
            current.update(state="open", retry_after_seconds=max(current.get("retry_after_seconds", 0), restriction["retry_after_seconds"]), blocked_until=restriction["blocked_until"], last_error_code="sdk_error_10001011")
        return {"tushare": {"configured": bool(self._token)}, "baostock": dict(installed=self._bao_available, **restriction), "circuits": circuits, "failure_diagnostics": diagnostics, "request_budget": self._budget_status()}

    def _bao_restriction(self):
        try:
            state = json.loads(self._provider_state_path.read_text())
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError, TypeError):
            raise ProviderUnavailable("provider_state_unavailable") from None
        try:
            until = float(state.get("baostock", {}).get("blocked_until", 0))
            if not math.isfinite(until):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ProviderUnavailable("provider_state_unavailable") from None
        return {"blocked_until": until, "retry_after_seconds": max(0, math.ceil(until-time.time()))}

    def _persist_bao_restriction(self):
        path = self._provider_state_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path.with_suffix(".lock"), "a+b") as lock:
            os.chmod(path.with_suffix(".lock"), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                state = json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            previous = state.get("baostock", {})
            if not isinstance(previous, dict):
                previous = {}
            try:
                prior_until = float(previous.get("blocked_until", 0))
                if not math.isfinite(prior_until):
                    prior_until = 0
            except (TypeError, ValueError):
                prior_until = 0
            until = max(prior_until, time.time()+21600)
            state["baostock"] = {"blocked_until": until, "last_error_code": "sdk_error_10001011", "updated_at": time.time()}
            tempname = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
                    tempname = tmp.name
                    json.dump(state, tmp)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tempname, path)
            finally:
                if tempname and os.path.exists(tempname):
                    os.unlink(tempname)

    def _worker_env(self):
        env = dict(os.environ)
        env["QUANT_BAO_LEASE_PATH"] = str(self._bao_lease_path)
        env["QUANT_MARKET_BASE_DIR"] = str(self.base_dir)
        return env

    def _record_failure(self, provider, symbol, exc):
        code = _safe_error_code(exc)
        with self._failure_lock:
            info = self._failure_diagnostics.setdefault(provider, {"last_error_code": None, "last_failed_symbol": None, "counts_by_code": {}})
            info["last_error_code"] = code
            info["last_failed_symbol"] = symbol
            info["counts_by_code"][code] = info["counts_by_code"].get(code, 0) + 1
        return code

    def request(self, provider, callback):
        """Admit one HTTP request; local limits never count as source failures."""
        try:
            with self._budget.request(provider):
                return callback()
        except (RequestBudgetExceeded, RequestQueueBusy, RequestBudgetUnavailable) as exc:
            raise ProviderBusy(exc.code) from None

    def _budget_status(self):
        result = {}
        for provider in ("tushare", "baostock", "eastmoney", "tencent"):
            try:
                result[provider] = self._budget.status(provider)
            except (RequestBudgetExceeded, RequestQueueBusy, RequestBudgetUnavailable) as exc:
                result[provider] = {"reason": exc.code, "retry_after": exc.retry_after}
        return result

    def _tushare_api(self, api, params, fields):
        try:
            with self._budget.request("tushare"):
                response = self.session.post("https://api.tushare.pro", json={"api_name": api, "token": self._token, "params": params, "fields": fields}, timeout=self.timeout)
                response.raise_for_status()
        except (RequestBudgetExceeded, RequestQueueBusy, RequestBudgetUnavailable) as exc:
            raise ProviderBusy(exc.code) from None
        data = response.json()
        if data.get("code") != 0 or not isinstance(data.get("data"), dict):
            raise ProviderUnavailable("api_rejected")
        payload = data["data"]
        if not isinstance(payload.get("fields"), list) or not isinstance(payload.get("items"), list):
            raise ProviderUnavailable("invalid_payload")
        return [dict(zip(payload["fields"], row)) for row in payload["items"]]

    def _daily_cache_path(self, day):
        return self.base_dir / "data/history_cache/tushare_daily" / (_day(day).replace("-", "") + ".json")

    @staticmethod
    def _validate_daily_batch(payload, day):
        if not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("complete") is not True or payload.get("date") != day:
            raise ProviderUnavailable("invalid_payload")
        rows, factors = payload.get("daily"), payload.get("adj_factor")
        if not isinstance(rows, list) or not rows or not isinstance(factors, list):
            raise ProviderUnavailable("invalid_payload")
        daily_codes, factor_codes = set(), set()
        for data, codes in ((rows, daily_codes), (factors, factor_codes)):
            for row in data:
                code = row.get("ts_code")
                if not isinstance(code, str) or not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code) or _day(row.get("trade_date")) != day or code in codes:
                    raise ProviderUnavailable("invalid_payload")
                codes.add(code)
        if not daily_codes.issubset(factor_codes):
            raise HistoryNotCurrent("missing_adjustment_factor")
        # Check every record before publishing an all-market cache. An invalid
        # instrument must never silently leave a partially populated batch.
        factors_by_code = {r["ts_code"]: r for r in factors}
        for row in rows:
            normalize_tushare([row], [factors_by_code[row["ts_code"]]])
        return payload

    def _read_daily_batch(self, day):
        try:
            path = self._daily_cache_path(day)
            signature = (path.stat().st_mtime_ns, path.stat().st_size)
            cached = self._daily_memory.get(day)
            if cached and cached[0] == signature:
                return cached[1]
            payload = self._validate_daily_batch(json.loads(path.read_text()), day)
            if len(self._daily_memory) >= 12:
                self._daily_memory.pop(next(iter(self._daily_memory)))
            payload["_daily_by_code"] = {r["ts_code"]: r for r in payload["daily"]}
            payload["_factor_by_code"] = {r["ts_code"]: r for r in payload["adj_factor"]}
            self._daily_memory[day] = (signature, payload)
            return payload
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def _tushare_day_pages(self, api, day, fields):
        rows, seen, offset, limit = [], set(), 0, 5000
        for _ in range(20):
            page = self._tushare_api(api, {"trade_date": day.replace("-", ""), "limit": limit, "offset": offset}, fields)
            if not isinstance(page, list) or len(page) > limit:
                raise ProviderUnavailable("invalid_payload")
            for row in page:
                key = (row.get("ts_code"), row.get("trade_date"))
                if key in seen:
                    # API ignored offset, or returned a repeated/truncated page.
                    raise ProviderUnavailable("query_incomplete")
                seen.add(key)
            rows.extend(page)
            if not page:
                return rows
            offset += len(page)
        raise ProviderUnavailable("query_incomplete")

    def _prepare_trade_day(self, day):
        path = self._daily_cache_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProviderBusy("busy") from None
            existing = self._read_daily_batch(day)
            if existing is not None:
                return existing, False
            failure_path = path.with_suffix(".failure.json")
            try:
                failure = json.loads(failure_path.read_text())
                retry_until = float(failure["retry_until"])
            except (OSError, ValueError, KeyError, TypeError):
                retry_until = 0
            if retry_until > time.time():
                raise ProviderBusy("rate_wait")
            try:
                rows = self._tushare_day_pages("daily", day, "ts_code,trade_date,open,high,low,close,pre_close,vol,amount")
                factors = self._tushare_day_pages("adj_factor", day, "ts_code,trade_date,adj_factor")
                payload = self._validate_daily_batch({"version": 1, "complete": True, "date": day, "daily": rows, "adj_factor": factors}, day)
            except ProviderBusy:
                raise
            except ProviderUnavailable as exc:
                with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
                    json.dump({"retry_until": time.time()+3600, "code": _safe_error_code(exc)}, tmp)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    failure_temp = tmp.name
                os.replace(failure_temp, failure_path)
                raise
            tempname = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
                    tempname = tmp.name
                    json.dump(payload, tmp, allow_nan=False)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tempname, path)
                failure_path.unlink(missing_ok=True)
            finally:
                if tempname and os.path.exists(tempname):
                    os.unlink(tempname)
            return payload, True

    def prepare_trade_days(self, days):
        """Fetch complete all-stock daily/factor batches once per trading day.

        Call once before an incremental universe refresh, never for full-history
        bootstrap. Unconfigured Tushare makes no requests. Errors remain explicit
        so callers may fall back without treating a partial batch as complete.
        """
        from trade_calendar import is_trading_day
        result = {"configured": bool(self._token), "ready": [], "downloaded": [], "errors": {}}
        if not self._token:
            return result
        for day in sorted({_day(day) for day in days}):
            now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
            if not is_trading_day(day) or day > now.date().isoformat() or (day == now.date().isoformat() and now.time() < dt.time(18, 0)):
                continue
            try:
                _, downloaded = self.circuit.call("tushare", lambda: self._prepare_trade_day(day))
                result["ready"].append(day)
                if downloaded:
                    result["downloaded"].append(day)
            except ProviderUnavailable as exc:
                result["errors"][day] = _safe_error_code(exc)
                # Never repeat a denied/failed all-market operation for every
                # remaining date in the same pass.
                break
        return result

    def cached_tushare(self, symbol, start, end, adjustment="qfq"):
        """Return normalized per-symbol slice, or None if any day is uncached."""
        from trade_calendar import is_trading_day
        code = symbol[2:] + "." + symbol[:2].upper()
        rows, factors = [], []
        left, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        # Do not scan years of filesystem entries during first-time bootstrap.
        if (last - left).days > 45:
            return None
        while left <= last:
            day = left.isoformat()
            if is_trading_day(day):
                batch = self._read_daily_batch(day)
                if batch is None:
                    return None
                if code in batch["_daily_by_code"]:
                    rows.append(batch["_daily_by_code"][code])
                if code in batch["_factor_by_code"]:
                    factors.append(batch["_factor_by_code"][code])
            left += dt.timedelta(days=1)
        return normalize_tushare(rows, factors, adjustment)

    def _tushare(self, symbol, start, end, adjustment):
        cached = None if symbol in ("sh000300", "sh000001", "sz399001", "sz399006") else self.cached_tushare(symbol, start, end, adjustment)
        if cached is not None:
            return cached
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
        result = subprocess.run(command, capture_output=True, text=True, timeout=max(self.timeout, 30) if self.timeout >= 6 else self.timeout, check=False, env=self._worker_env())
        if result.returncode:
            try:
                code = json.loads(result.stdout).get("error")
            except (ValueError, TypeError, AttributeError):
                code = None
            if isinstance(code, str):
                if code in LOCAL_ADMISSION_CODES:
                    raise ProviderBusy(code)
                if code in ASSET_ERROR_CODES:
                    raise HistoryNotCurrent(code)
                if code in SAFE_ERROR_CODES or re.fullmatch(r"sdk_error_-?\d{1,8}", code):
                    raise ProviderUnavailable(code)
            raise ProviderUnavailable("worker_failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise ProviderUnavailable("invalid_worker_result")
        return payload

    def _baostock(self, symbol, start, end, adjustment):
        # Persist a first-day unknown-usage guard without connecting. All
        # callers share this same per-user ledger across project checkouts.
        try:
            admission = self._budget.initialize("baostock")
        except (RequestBudgetExceeded, RequestQueueBusy, RequestBudgetUnavailable) as exc:
            raise ProviderBusy(exc.code) from None
        if admission.get("reason") in ("untracked_prior_usage", "daily_limit"):
            raise ProviderBusy(admission["reason"])
        # A documented provider restriction survives process/service restarts.
        if self._bao_restriction()["retry_after_seconds"]:
            raise ProviderUnavailable("provider_restricted")
        # Serialize a persistent session, with bounded queueing as well as I/O.
        if not _BAO_LOCK.acquire(timeout=self.timeout):
            raise ProviderBusy("busy")
        try:
            if not self._bao_stream:
                return self._baostock_once(symbol, start, end, adjustment)
            deadline = time.monotonic() + (max(self.timeout, 30) if self.timeout >= 6 else self.timeout)
            if self._bao_process is None or self._bao_process.poll() is not None:
                self._stop_bao_worker()
                command = [sys.executable, "-u", str(Path(__file__).resolve()), "--baostock-stream"]
                self._bao_process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                     stderr=subprocess.DEVNULL, bufsize=0, env=self._worker_env())
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
                        if not isinstance(payload, dict):
                            raise ProviderUnavailable("invalid_worker_result")
                        startup_error = payload.get("phase") == "login" and payload.get("id") is None and "error" in payload
                        if payload.get("id") != request_id and not startup_error:
                            raise ProviderUnavailable("invalid_worker_result")
                        if "error" in payload:
                            # Never trust worker messages outside our allowlist.
                            code = payload["error"]
                            if code in ASSET_ERROR_CODES and payload.get("asset_error") is True:
                                raise HistoryNotCurrent(code)
                            if code in LOCAL_ADMISSION_CODES:
                                raise ProviderBusy(code)
                            if isinstance(code, str) and (code in SAFE_ERROR_CODES or re.fullmatch(r"sdk_error_-?\d{1,8}", code)):
                                raise ProviderUnavailable(code)
                            raise ProviderUnavailable("invalid_worker_result")
                        if not isinstance(payload.get("bars"), list):
                            raise ProviderUnavailable("invalid_worker_result")
                        return payload["bars"]
        except Exception as exc:
            self._stop_bao_worker()
            if isinstance(exc, ProviderUnavailable) and str(exc) == "sdk_error_10001011":
                self._persist_bao_restriction()
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
                warnings.append(name + "_" + self._record_failure(name, symbol, exc))
        if legacy_download is not None:
            name, bars, source, errors = legacy_download(symbol, start, end, adjustment)
            return name, bars, source, warnings + errors
        return symbol[2:], [], "unavailable", warnings


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--baostock-stream":
        sys.exit(_baostock_stream())
    elif len(sys.argv) == 6 and sys.argv[1] == "--baostock-worker":
        try:
            with _acquire_worker_lease():
                result = _baostock_worker(*sys.argv[2:])
                sys.stdout.write(json.dumps(result, allow_nan=False))
        except Exception as exc:
            sys.stdout.write(json.dumps({"error": _safe_error_code(exc)}, allow_nan=False))
            sys.stdout.flush()
            sys.exit(1)
    else:
        sys.exit(2)
