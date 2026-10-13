#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史行情数据获取与本地缓存模块 (Historical Market Data Feed)

支持功能：
1. 腾讯 (Tencent) & 东方财富 (Eastmoney) 公共前复权日K线数据获取（OHLCV、成交额、振幅、涨跌幅）。
2. 基准指数（沪深300 000300、上证指数 000001）历史数据拉取与对齐。
3. 本地磁盘高速缓存 (data/history_cache/)，秒级重复加载，零额外重试负担。
4. 多线程并发批量下载与标准数据结构规整。
5. 内置精选代表性标的池（核心活跃池、沪深300代表池等）。
"""

import os
import json
import datetime
import math
import re
import tempfile
import threading
import copy
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

from trade_calendar import BEIJING, get_previous_trading_day, is_trading_day, to_date

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data" / "history_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 预设标的池
PRESET_POOLS = {
    "core_active": [
        # 当前持仓与前期表现优异个股
        "sz300319",  # 麦捷科技 (电子/芯片)
        "sh600237",  # 铜峰电子 (电子元器件)
        "sh600105",  # 永鼎股份 (通信/光模块)
        "sz003035",  # 南网能源 (绿电/储能)
        "sh603578",  # 三星新材 (新材料)
        # 高流动性活跃成长/科技龙头
        "sz300750",  # 宁德时代 (电池/新能源)
        "sz002594",  # 比亚迪 (新能源车)
        "sz300059",  # 东方财富 (金融科技)
        "sz300308",  # 中际旭创 (光通信/CPO)
        "sh601127",  # 赛力斯 (智能汽车)
        "sh601138",  # 工业富联 (算力/AI服务器)
        "sz002475",  # 立讯精密 (消费电子/苹果链)
        "sh600519",  # 贵州茅台 (白酒/消费)
        "sz000001",  # 平安银行 (银行/大金融)
        "sh601318",  # 中国平安 (保险/金融)
        "sh601899",  # 紫金矿业 (有色金属/黄金)
        "sh600900",  # 长江电力 (红利/公用事业)
        "sz002230",  # 科大讯飞 (人工智能)
        "sz002460",  # 赣锋锂业 (锂电/能源金属)
        "sh600036",  # 招商银行 (银行)
    ],
    "csi300_sample": [
        "sh600519", "sh601318", "sz300750", "sz002594", "sh600036",
        "sh601899", "sh600900", "sz000858", "sh600276", "sh601138",
        "sz300059", "sz002475", "sh601012", "sh600309", "sh603259",
        "sz000001", "sh600030", "sh600887", "sz002352", "sh601888",
    ],
}


def normalize_code_for_eastmoney(symbol_or_code: str) -> Tuple[str, str]:
    """
    返回 (secid, clean_code)
    例如: 'sh600519' -> ('1.600519', '600519')
    """
    s = symbol_or_code.strip().lower()
    if s.startswith("sh"):
        code = s[2:]
        market = "1"
    elif s.startswith("sz"):
        code = s[2:]
        market = "0"
    elif s.startswith("bj"):
        code = s[2:]
        market = "0"
    else:
        code = s
        market = "1" if code.startswith(("60", "68")) else "0"
    return f"{market}.{code}", code


def normalize_symbol_for_tencent(symbol_or_code: str) -> str:
    """
    返回标准小写带前缀代码，例如 'sh600519'
    """
    s = symbol_or_code.strip().lower()
    if s.startswith(("sh", "sz", "bj")):
        return s
    if s.startswith(("60", "68")):
        return f"sh{s}"
    elif s.startswith(("00", "30", "399")):
        return f"sz{s}"
    elif s.startswith(("43", "83", "87", "92")):
        return f"bj{s}"
    return f"sz{s}"


def previous_trading_day(date=None) -> str:
    """Previous exchange session, strictly before the supplied Beijing date."""
    return get_previous_trading_day(date).isoformat()


def last_completed_trading_day(now=None) -> str:
    """Use today's daily bar only after 15:05 Beijing time on a session day."""
    now = now or datetime.datetime.now(BEIJING)
    if not isinstance(now, datetime.datetime):
        now = datetime.datetime.combine(to_date(now), datetime.time(), BEIJING)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING)
    else:
        now = now.astimezone(BEIJING)
    if is_trading_day(now.date()) and now.time() >= datetime.time(15, 5):
        return now.date().isoformat()
    return previous_trading_day(now)


class HistoricalDataFeed:
    """Validated adjusted indicators and original execution prices with bounded I/O.

    Volume is reported in lots (100 shares). A missing daily bar is not filled in:
    it can mean suspension, an IPO boundary or a provider gap. Coverage warnings
    preserve that uncertainty instead of manufacturing tradable observations.
    """

    CACHE_SCHEMA = 2
    CACHE_TTL = datetime.timedelta(hours=24)
    INDUSTRY_CACHE_TTL = datetime.timedelta(days=7)
    MAX_WORKERS = 6

    def __init__(self, cache_dir: Optional[Path] = None, request_timeout: float = 6):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(HEADERS)
        self.request_timeout = request_timeout
        self.industry_file = BASE_DIR / "data" / "industry_map.json"
        self.metadata: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _get_cache_path(self, symbol: str, start_date=None, end_date=None,
                        adjustment: str = "qfq") -> Path:
        # Dates belong in metadata, not the filename: daily refreshes update one
        # file per symbol and retain wider validated history when requested.
        return self.cache_dir / f"{symbol.replace('.', '_')}_{adjustment}.json"

    def _legacy_cache_path(self, symbol, start, end):
        return self.cache_dir / f"{symbol.replace('.', '_')}_{start}_{end}.json"

    @staticmethod
    def _dates(start_date, end_date):
        start = datetime.date.fromisoformat(start_date).isoformat()
        end = datetime.date.fromisoformat(end_date or last_completed_trading_day()).isoformat()
        if start > end:
            raise ValueError("start_date must not be after end_date")
        return start, end

    @staticmethod
    def _validate_bars(bars):
        """Reject malformed/non-finite daily bars; sort and deduplicate by date."""
        valid = {}
        rejected = 0
        duplicates = 0
        for bar in bars:
            try:
                item = dict(bar)
                item["date"] = datetime.date.fromisoformat(str(item["date"])).isoformat()
                for field in ("open", "close", "high", "low", "volume"):
                    item[field] = float(item[field])
                    if not math.isfinite(item[field]):
                        raise ValueError("non-finite bar")
                if min(item[k] for k in ("open", "close", "high", "low")) <= 0:
                    raise ValueError("non-positive price")
                if item["volume"] < 0:
                    raise ValueError("negative volume")
                if (item["high"] + 1e-8 < max(item["open"], item["close"], item["low"])
                        or item["low"] - 1e-8 > min(item["open"], item["close"], item["high"])):
                    raise ValueError("inconsistent OHLC")
                for field in ("amount", "amplitude_pct", "change_pct", "change_amount", "turnover_rate"):
                    if field in item and not math.isfinite(float(item[field])):
                        raise ValueError("non-finite supplemental field")
                if item["date"] in valid:
                    duplicates += 1
                valid[item["date"]] = item
            except (KeyError, TypeError, ValueError, OverflowError):
                rejected += 1
        return [valid[d] for d in sorted(valid)], rejected, duplicates

    @staticmethod
    def _session_dates(start, end):
        day = datetime.date.fromisoformat(start)
        finish = datetime.date.fromisoformat(end)
        sessions = []
        while day <= finish:
            if is_trading_day(day):
                sessions.append(day.isoformat())
            day += datetime.timedelta(days=1)
        return sessions

    @staticmethod
    def _has_raw(bar):
        try:
            values = [float(bar[k]) for k in ("raw_open", "raw_close", "raw_high", "raw_low", "adjustment_factor")]
            return (all(math.isfinite(v) and v > 0 for v in values)
                    and values[2] + 1e-8 >= max(values[0], values[1], values[3])
                    and values[3] - 1e-8 <= min(values[0], values[1], values[2])
                    and math.isclose(float(bar["close"]) / values[1], values[4], rel_tol=1e-6))
        except (TypeError, KeyError, ValueError, ZeroDivisionError):
            return False

    def _describe(self, bars, start, end, source, fetched_at=None, warnings=None, error=None):
        dates = {b["date"] for b in bars}
        missing = [d for d in self._session_dates(start, end) if d not in dates]
        warnings = list(warnings or [])
        if missing:
            warnings.append("missing_sessions_may_be_suspension_ipo_or_provider_gap")
        raw_ok = bool(bars) and all(self._has_raw(b) for b in bars)
        if bars and not raw_ok:
            warnings.append("original_execution_prices_missing; adjusted_prices_must_not_be_used_for_fills")
        fetched_at = fetched_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            fetched = datetime.datetime.fromisoformat(fetched_at)
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=datetime.timezone.utc)
            age = datetime.datetime.now(datetime.timezone.utc) - fetched
            fresh = datetime.timedelta(0) <= age <= self.CACHE_TTL
        except (ValueError, TypeError):
            fresh = False
        if not fresh:
            warnings.append("cache_expired")
        return {
            "schema_version": self.CACHE_SCHEMA,
            "fetched_at": fetched_at,
            "requested_start": start, "requested_end": end,
            "adjustment": "qfq", "volume_unit": "lots_100_shares", "source": source,
            "coverage": {"start": bars[0]["date"] if bars else None,
                         "end": bars[-1]["date"] if bars else None,
                         "count": len(bars), "missing_trading_dates": missing},
            "coverage_complete": bool(bars) and not missing,
            "raw_prices_available": raw_ok, "fresh": fresh,
            "warning": list(dict.fromkeys(warnings)), "error": error,
        }

    def _read_cache(self, symbol, start, end, adjustment):
        try:
            data = json.loads(self._get_cache_path(symbol, adjustment=adjustment).read_text(encoding="utf-8"))
            if data.get("schema_version") != self.CACHE_SCHEMA or data.get("symbol") != symbol:
                return None
            if data.get("metadata", {}).get("adjustment") != adjustment:
                return None
            bars, rejected, _ = self._validate_bars(data.get("bars", []))
            data["bars"] = bars
            if rejected:
                data.setdefault("metadata", {}).setdefault("warning", []).append("invalid_cached_bars")
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _read_legacy(self, symbol, start, end):
        try:
            data = json.loads(self._legacy_cache_path(symbol, start, end).read_text(encoding="utf-8"))
            if isinstance(data, list):
                data = {"symbol": symbol, "name": symbol, "bars": data}
            bars, _, _ = self._validate_bars(data.get("bars", []))
            # A legacy date-named file has no freshness or adjustment evidence.
            data["bars"] = [b for b in bars if start <= b["date"] <= end]
            return data if data["bars"] else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _write_cache(self, symbol, adjustment, name, bars, metadata):
        target = self._get_cache_path(symbol, adjustment=adjustment)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                             prefix=target.stem + ".", suffix=".tmp", delete=False) as stream:
                temp_path = stream.name
                json.dump({"schema_version": self.CACHE_SCHEMA, "symbol": symbol,
                           "name": name, "bars": bars, "metadata": metadata},
                          stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, target)
        except (OSError, ValueError, TypeError):
            metadata["warning"].append("cache_write_failed")
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    def _eastmoney(self, symbol, start, end, adjustment):
        secid, _ = normalize_code_for_eastmoney(symbol)
        params = {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                  "klt": 101, "fqt": 1 if adjustment == "qfq" else 0,
                  "beg": start.replace("-", ""), "end": end.replace("-", "")}
        response = self.session.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                                    params=params, timeout=self.request_timeout)
        response.raise_for_status()
        payload = response.json().get("data") or {}
        bars = []
        for line in payload.get("klines", []):
            row = line.split(",")
            try:
                if len(row) < 7:
                    continue
                bar = {"date": row[0], "open": float(row[1]), "close": float(row[2]),
                       "high": float(row[3]), "low": float(row[4]), "volume": float(row[5]),
                       "amount": float(row[6]), "source": "eastmoney"}
                for i, key in ((7, "amplitude_pct"), (8, "change_pct"), (9, "change_amount"), (10, "turnover_rate")):
                    if i < len(row):
                        bar[key] = float(row[i])
                bars.append(bar)
            except (ValueError, TypeError):
                continue
        return payload.get("name") or symbol[2:], bars

    def _tencent(self, symbol, start, end, adjustment):
        # Limit each request to one calendar year (< 640 daily observations).
        # An empty start/end plus 640 latest bars cannot cover a multi-year study.
        start_day = datetime.date.fromisoformat(start)
        finish = datetime.date.fromisoformat(end)
        bars, name, errors = [], symbol[2:], []
        while start_day <= finish:
            slice_end = min(datetime.date(start_day.year, 12, 31), finish)
            param = f"{symbol},day,{start_day.isoformat()},{slice_end.isoformat()},640,{adjustment if adjustment == 'qfq' else ''}"
            try:
                response = self.session.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                                            params={"param": param}, timeout=self.request_timeout)
                response.raise_for_status()
                payload = response.json().get("data", {}).get(symbol, {})
                quote = payload.get("qt", {}).get(symbol, [])
                if len(quote) > 1 and quote[1]:
                    name = quote[1]
                rows = payload.get("qfqday" if adjustment == "qfq" else "day", [])
                # Never silently label unadjusted 'day' as adjusted 'qfqday'.
                for row in rows:
                    try:
                        if len(row) < 6 or not start_day.isoformat() <= row[0] <= slice_end.isoformat():
                            continue
                        op, cl, hi, lo, vol = map(float, row[1:6])
                        bars.append({"date": row[0], "open": op, "close": cl, "high": hi,
                                     "low": lo, "volume": vol, "source": "tencent",
                                     "amount": vol * (op + cl) / 2 * 100, "amount_estimated": True,
                                     "amplitude_pct": (hi - lo) / lo * 100 if lo > 0 else 0,
                                     "turnover_rate": 0})
                    except (ValueError, TypeError):
                        continue
            except (requests.RequestException, ValueError, AttributeError, TypeError) as exc:
                errors.append(f"tencent_{start_day.year}_{type(exc).__name__}")
            start_day = slice_end + datetime.timedelta(days=1)
        return name, bars, errors

    def _download(self, symbol, start, end, adjustment):
        warnings = []
        name, adjusted, raw = symbol[2:], [], []
        try:
            name, adjusted = self._eastmoney(symbol, start, end, adjustment)
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            warnings.append(f"eastmoney_adjusted_{type(exc).__name__}")
        if adjustment == "none":
            raw = adjusted
        elif adjusted:
            try:
                _, raw = self._eastmoney(symbol, start, end, "none")
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                warnings.append(f"eastmoney_original_{type(exc).__name__}")
        if not adjusted:
            name, adjusted, errors = self._tencent(symbol, start, end, adjustment)
            warnings.extend(errors)
        if adjustment == "none":
            raw = adjusted
        elif not raw:
            _, raw, errors = self._tencent(symbol, start, end, "none")
            warnings.extend(errors)
        adjusted, rejected, duplicates = self._validate_bars(adjusted)
        raw, raw_rejected, raw_duplicates = self._validate_bars(raw)
        if rejected or raw_rejected:
            warnings.append(f"invalid_bars_rejected:{rejected + raw_rejected}")
        if duplicates or raw_duplicates:
            warnings.append(f"duplicate_dates_removed:{duplicates + raw_duplicates}")
        raw_by_date = {b["date"]: b for b in raw}
        previous_raw_by_date = {}
        previous_raw = None
        for original in raw:
            previous_raw_by_date[original["date"]] = previous_raw
            previous_raw = original
        for bar in adjusted:
            original = raw_by_date.get(bar["date"])
            if original:
                for key in ("open", "close", "high", "low"):
                    bar[f"raw_{key}"] = original[key]
                bar["adjustment_factor"] = bar["close"] / original["close"]
                bar["volume"] = original["volume"]
                bar["amount"] = original.get("amount", bar.get("amount", 0))
                pre_close = None
                # Eastmoney's original-price daily change uses the exchange's
                # reference close, including ex-dividend reference adjustment.
                if "change_amount" in original:
                    pre_close = original["close"] - original["change_amount"]
                    bar["raw_pre_close_source"] = "provider_change_amount"
                elif previous_raw_by_date.get(bar["date"]):
                    pre_close = previous_raw_by_date[bar["date"]]["close"]
                    bar["raw_pre_close_source"] = "previous_raw_close"
                if pre_close is not None and math.isfinite(pre_close) and pre_close > 0:
                    bar["raw_pre_close"] = round(pre_close, 8)
        sources = sorted({b.get("source", "unknown") for b in adjusted + raw})
        return name, adjusted, "+".join(sources) or "unavailable", warnings

    def _fetch(self, symbol, start_date, end_date, use_cache, adjustment="qfq", cache_symbol=None):
        if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
            raise ValueError("symbol must be an exchange prefix plus six digits")
        start, end = self._dates(start_date, end_date)
        key = cache_symbol or symbol
        cached = self._read_cache(key, start, end, adjustment) if use_cache else None
        legacy = None
        if cached:
            subset = [b for b in cached["bars"] if start <= b["date"] <= end]
            old = cached.get("metadata", {})
            metadata = self._describe(subset, start, end, old.get("source", "cache"),
                                      old.get("fetched_at"), old.get("warning", []))
            metadata["adjustment"] = adjustment
            range_ok = (old.get("requested_start", "9999") <= start
                        and old.get("requested_end", "") >= end)
            if range_ok and metadata["fresh"] and metadata["coverage_complete"] and metadata["raw_prices_available"]:
                with self._lock:
                    self.metadata[key] = metadata
                return symbol, cached.get("name", symbol[2:]), subset
        else:
            legacy = self._read_legacy(key, start, end) if use_cache else None

        # One preceding session provides a real reference close for a first-day
        # Tencent bar. Expand to the cache range so a short request never erases
        # longer history. Re-download the union: qfq changes after corporate
        # actions must not be mixed with an older adjustment basis.
        download_start, download_end = previous_trading_day(start), end
        if cached and cached["bars"]:
            download_start = min(download_start, cached["bars"][0]["date"])
            download_end = max(end, cached["bars"][-1]["date"])
        name, downloaded, source, warnings = self._download(symbol, download_start, download_end, adjustment)
        downloaded = [b for b in downloaded if download_start <= b["date"] <= download_end]
        subset = [b for b in downloaded if start <= b["date"] <= end]
        error = None
        if not subset:
            error = "no_valid_bars_for_requested_range"
            fallback = cached or legacy
            if fallback:
                subset = [b for b in fallback["bars"] if start <= b["date"] <= end]
                name = fallback.get("name", name)
                source = "stale_cache" if cached else "legacy_cache"
                warnings.append("download_failed_cache_fallback_not_eligible_for_live_signals")
        metadata = self._describe(subset, start, end, source,
                                  cached.get("metadata", {}).get("fetched_at") if source == "stale_cache" else None,
                                  warnings, error)
        metadata["adjustment"] = adjustment
        if source in ("legacy_cache", "stale_cache"):
            metadata["fresh"] = False
        with self._lock:
            self.metadata[key] = metadata
        # A partially failed refresh must not destroy the wider old cache. We
        # cannot merge old/new qfq bases safely, so return the explicitly partial
        # fetch while retaining the old file for the next repair attempt.
        cache_dates = {b["date"] for b in cached["bars"]} if cached else set()
        downloaded_dates = {b["date"] for b in downloaded}
        regression = bool(cache_dates - downloaded_dates)
        if regression and downloaded:
            metadata["warning"].append("partial_refresh_retained_wider_cache")
        if downloaded and not regression:
            cache_meta = self._describe(downloaded, min(start, download_start), download_end, source,
                                        metadata["fetched_at"], warnings)
            cache_meta["adjustment"] = adjustment
            self._write_cache(key, adjustment, name, downloaded, cache_meta)
        return symbol, name, subset

    def fetch_stock_kline(self, symbol_or_code: str, start_date: str = "2024-01-01",
                          end_date: Optional[str] = None, use_cache: bool = True):
        """Return (symbol, name, sorted qfq bars); self.metadata holds provenance."""
        return self._fetch(normalize_symbol_for_tencent(symbol_or_code), start_date, end_date, use_cache)

    def fetch_benchmark_kline(self, index_code: str = "000300", start_date: str = "2024-01-01",
                              end_date: Optional[str] = None, use_cache: bool = True):
        code = index_code.lower().replace("sh", "").replace("sz", "")
        symbol = f"sh{code}" if code in ("000300", "000001", "000905") else f"sz{code}"
        _, _, bars = self._fetch(symbol, start_date, end_date, use_cache,
                                adjustment="none", cache_symbol=f"idx_{code}")
        return bars

    def fetch_universe_klines(self, symbols: List[str], start_date: str = "2024-01-01",
                              end_date: Optional[str] = None, max_workers: int = 6,
                              use_cache: bool = True):
        """Every requested symbol is present, including explicit failure entries."""
        # Resolve dynamic end once so one large request never straddles a cutoff.
        start, end = self._dates(start_date, end_date)
        unique = list(dict.fromkeys(normalize_symbol_for_tencent(s) for s in symbols))
        results = {}

        def fetch_one(symbol):
            try:
                sym, name, bars = self.fetch_stock_kline(symbol, start, end, use_cache)
                with self._lock:
                    metadata = copy.deepcopy(self.metadata.get(sym, {}))
                return sym, {"symbol": sym, "name": name, "bars": bars,
                             "by_date": {b["date"]: b for b in bars},
                             "metadata": metadata, "error": metadata.get("error")}
            except Exception as exc:
                metadata = self._describe([], start, end, "unavailable", error=type(exc).__name__)
                return symbol, {"symbol": symbol, "name": symbol[2:], "bars": [], "by_date": {},
                                "metadata": metadata, "error": metadata["error"]}

        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), self.MAX_WORKERS))) as executor:
            for symbol, item in executor.map(fetch_one, unique):
                results[symbol] = item
        return results

    @staticmethod
    def _valid_industries(value):
        if not isinstance(value, dict):
            return {}
        industries = {}
        for symbol, industry in value.items():
            if not isinstance(symbol, str) or not isinstance(industry, str):
                continue
            symbol = normalize_symbol_for_tencent(symbol)
            industry = industry.strip()
            if (re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol)
                    and industry and industry not in ("-", "--", "---", "未知", "unknown", "None")):
                industries[symbol] = industry
        return industries

    def fetch_industry_map(self, use_cache: bool = True) -> Dict[str, str]:
        """Load manual classifications first, then fill gaps from a bounded feed.

        The user's data/industry_map.json is read only; its classifications always
        win. Provider data lives in a separate seven-day cache. Status is exposed
        as self.metadata['industry_map'], including partial coverage and errors.
        This is a current classification snapshot, not point-in-time industry
        history suitable for removing all historical classification bias.
        """
        target = self.cache_dir / "industry_map_eastmoney.json"
        warnings = []
        manual, fallback = {}, {}
        manual_source = "manual"
        try:
            supplied = json.loads(self.industry_file.read_text(encoding="utf-8"))
            supplied_map = self._valid_industries(supplied.get("industries", {}))
            manual_source = str(supplied.get("source", "manual"))
            if manual_source in ("eastmoney", "eastmoney_clist"):
                fallback.update(supplied_map)
            else:
                manual.update(supplied_map)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError):
            warnings.append("manual_industry_file_invalid")

        cached = {}
        if use_cache:
            try:
                cached = json.loads(target.read_text(encoding="utf-8"))
                if cached.get("schema_version") != 1:
                    cached = {}
                else:
                    cached_map = self._valid_industries(cached.get("industries", {}))
                    fallback.update(cached_map)
                    previous = cached.get("metadata", {})
                    stamp = datetime.datetime.fromisoformat(previous.get("fetched_at", ""))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
                    age = datetime.datetime.now(datetime.timezone.utc) - stamp
                    fresh = datetime.timedelta(0) <= age <= self.INDUSTRY_CACHE_TTL
                    if fresh and previous.get("coverage_complete") and cached_map:
                        output = {**cached_map, **manual}
                        status = copy.deepcopy(previous)
                        status.update({"fresh": True, "cache_hit": True, "manual_symbols": len(manual),
                                       "symbols": len(output), "source": "manual+eastmoney" if manual else "eastmoney",
                                       "manual_source": manual_source if manual else None,
                                       "warning": list(dict.fromkeys(warnings + status.get("warning", [])))})
                        with self._lock:
                            self.metadata["industry_map"] = status
                        return output
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError, TypeError):
                warnings.append("industry_cache_invalid")

        downloaded = {}
        reported_total = None
        received_rows = 0
        missing_industry = 0
        excluded_non_main_board = 0
        duplicate_rows = 0
        seen_symbols = set()
        attempted_pages = 0
        completed_pages = 0
        error = None
        # The production provider caps pz at 100 even if 500 is requested.
        # Count actual received rows toward reported_total; using requested page
        # capacity would silently truncate the universe at the tenth page.
        max_pages, page_size = 50, 100
        for page in range(1, max_pages + 1):
            attempted_pages = page
            params = {"pn": page, "pz": page_size, "po": 0, "np": 1, "fltt": 2,
                      "fid": "f12", "fs": "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:13",
                      "fields": "f12,f13,f14,f100"}
            try:
                response = self.session.get("https://push2.eastmoney.com/api/qt/clist/get", params=params,
                                            timeout=min(10, max(1, self.request_timeout)))
                response.raise_for_status()
                payload = response.json().get("data")
                if not isinstance(payload, dict):
                    raise ValueError("industry response has no data")
                total = int(payload.get("total", 0))
                if total < 0:
                    raise ValueError("industry response total is negative")
                reported_total = total if reported_total is None else max(total, reported_total)
                rows = payload.get("diff", [])
                if isinstance(rows, dict):
                    rows = list(rows.values())
                if not isinstance(rows, list):
                    raise ValueError("industry rows are not a list")
                completed_pages += 1
                received_rows += len(rows)
                for item in rows:
                    if not isinstance(item, dict):
                        missing_industry += 1
                        continue
                    market = str(item.get("f13", ""))
                    code = str(item.get("f12", ""))
                    industry = item.get("f100")
                    symbol = ("sh" if market == "1" else "sz") + code
                    if market not in ("0", "1") or not re.fullmatch(r"\d{6}", code):
                        missing_industry += 1
                        continue
                    if symbol in seen_symbols:
                        duplicate_rows += 1
                    seen_symbols.add(symbol)
                    # The provider's fs response can also contain STAR/ChiNext
                    # stocks. Count them for pagination, but do not introduce
                    # them into this project's main-board experiment universe.
                    if not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
                        excluded_non_main_board += 1
                        continue
                    valid = self._valid_industries({symbol: industry})
                    if valid:
                        downloaded.update(valid)
                    else:
                        missing_industry += 1
                if received_rows >= reported_total:
                    break
                if not rows:
                    error = "industry_empty_page_before_reported_total"
                    break
            except (requests.RequestException, ValueError, AttributeError, TypeError) as exc:
                error = f"industry_fetch_{type(exc).__name__}"
                break
        complete_pages = reported_total is not None and received_rows >= reported_total and error is None
        truncated = reported_total is not None and received_rows < reported_total
        if truncated:
            warnings.append("industry_pagination_incomplete")
        if missing_industry:
            warnings.append("industry_field_missing_or_invalid")
        if duplicate_rows:
            warnings.append("industry_duplicate_rows_or_overlapping_pages")
        if not downloaded:
            error = error or "industry_no_valid_classifications"
        if error:
            warnings.append("industry_fetch_failed_using_existing_mapping")
        fallback_only = set(fallback) - set(downloaded) - set(manual)
        if fallback_only:
            warnings.append("existing_industry_mapping_may_be_stale")
        output = {**fallback, **downloaded, **manual}
        status = {
            "schema_version": 1, "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": "manual+eastmoney" if manual and downloaded else (manual_source if manual else
                      ("eastmoney" if downloaded else ("stale_cache" if fallback else "unavailable"))),
            "manual_source": manual_source if manual else None,
            "fresh": complete_pages and bool(downloaded) and not fallback_only, "cache_hit": False,
            "symbols": len(output), "manual_symbols": len(manual), "provider_symbols": len(downloaded),
            "fallback_symbols": len(fallback_only),
            "coverage_complete": (complete_pages and bool(downloaded) and missing_industry == 0
                                  and duplicate_rows == 0
                                  and len(downloaded) + excluded_non_main_board >= reported_total),
            "coverage": {"reported_total": reported_total, "received_rows": received_rows,
                         "valid_industry_symbols": len(downloaded), "missing_industry_rows": missing_industry,
                         "received_unique_symbols": len(seen_symbols),
                         "excluded_non_main_board_rows": excluded_non_main_board,
                         "duplicate_rows": duplicate_rows,
                         "attempted_pages": attempted_pages, "completed_pages": completed_pages,
                         "page_size": page_size, "max_pages": max_pages, "truncated": truncated},
            "warning": list(dict.fromkeys(warnings + ["current_industry_snapshot_not_point_in_time"])),
            "error": error,
        }
        # Persist only provider observations, never the manual overlay. A failed
        # refresh must not replace a larger existing provider cache.
        prior_provider_count = len(self._valid_industries(cached.get("industries", {})))
        if downloaded and complete_pages and len(downloaded) >= prior_provider_count:
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                                 prefix="industry_map.", suffix=".tmp", delete=False) as stream:
                    temp_path = stream.name
                    json.dump({"schema_version": 1, "industries": downloaded, "metadata": status},
                              stream, ensure_ascii=False, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, target)
            except (OSError, ValueError, TypeError):
                status["warning"].append("industry_cache_write_failed")
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
        with self._lock:
            self.metadata["industry_map"] = status
        return output

    @staticmethod
    def get_preset_symbols(pool_type: str = "core_active") -> List[str]:
        return PRESET_POOLS.get(pool_type, PRESET_POOLS["core_active"])
