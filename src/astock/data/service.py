# src/astock/data/service.py
import json
import logging
import math
import threading
import time
import uuid
from datetime import datetime, date, timedelta, time as datetime_time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .models import DailyBar, StockInfo, SyncProgress, normalize_symbol, to_clean_code
from .storage import LocalStorageManager
from .providers.manager import ProviderManager
from .quality import DataQualityValidator
from .security_status import load_nontrading_statuses
from ..domain.enums import MarketRegime, DataQuality, REGIME_STRONG_BREADTH_MIN
from ..trade.risk.market import MarketRiskRule
from ..domain.models import Bar, MarketSnapshot
from trade_calendar import BEIJING, is_trading_day, get_previous_trading_day

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parents[3]


def _last_completed_session(now=None) -> str:
    """A daily bar is eligible only after 15:05 Beijing on a real session day."""
    now = now or datetime.now(BEIJING)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING)
    else:
        now = now.astimezone(BEIJING)
    if is_trading_day(now.date()) and now.time() >= datetime_time(15, 5):
        return now.date().isoformat()
    return get_previous_trading_day(now.date()).isoformat()


class AStockDataService:
    """
    Central Market Data Service:
    - Single point of entry for Strategy Engine, Risk Manager, Backtest, and Scheduler
    - Coordinates Local Storage (Parquet/SQLite) and Provider Manager (BaoStock/Tencent/AKShare)
    - Produces unified MarketSnapshot (shared by Balanced & Aggressive accounts)
    - Completely offline capable for historical backtesting
    """

    def __init__(self, storage: Optional[LocalStorageManager] = None,
                 providers: Optional[ProviderManager] = None,
                 offline_mode: bool = False):
        self.storage = storage or LocalStorageManager(BASE_DIR / "data" / "storage")
        self.providers = providers or ProviderManager()
        self.offline_mode = offline_mode
        self._snapshot_cache: Dict[str, MarketSnapshot] = {}
        self._lock = threading.RLock()
        self._industry_map_cache: Optional[Dict[str, str]] = None
        self._stock_names_cache: Optional[Dict[str, str]] = None

    # --------------------------------------------------------------------------
    # Core Bar Access
    # --------------------------------------------------------------------------

    def get_bars(self, symbol: str, start_date: str = "", end_date: str = "",
                 adjust_type: str = "qfq", auto_fetch: bool = True) -> List[DailyBar]:
        """
        Query historical daily bars for a symbol.
        Checks local Parquet storage first. If incomplete/missing and auto_fetch is True
        (and not offline_mode), fetches via ProviderManager, stores to Parquet, and returns.
        """
        sym = normalize_symbol(symbol)
        bars = self.storage.read_daily_bars(sym, start_date, end_date, adjust_type=adjust_type)

        if bars:
            # Check if requested range is satisfied
            if not self.offline_mode and auto_fetch and end_date:
                latest = bars[-1].trade_date
                if latest < end_date:
                    # Fetch missing increment
                    new_bars = self.providers.fetch_stock_daily(sym, latest, end_date, adjust_type=adjust_type)
                    if new_bars:
                        self.storage.write_daily_bars(sym, new_bars, adjust_type=adjust_type)
                        bars = self.storage.read_daily_bars(sym, start_date, end_date, adjust_type=adjust_type)
            return bars

        if not self.offline_mode and auto_fetch:
            s_date = start_date or "2023-01-01"
            e_date = end_date or datetime.now().strftime("%Y-%m-%d")
            fetched = self.providers.fetch_stock_daily(sym, s_date, e_date, adjust_type=adjust_type)
            if fetched:
                self.storage.write_daily_bars(sym, fetched, adjust_type=adjust_type)
                return self.storage.read_daily_bars(sym, start_date, end_date, adjust_type=adjust_type)

        return []

    def get_benchmark_bars(self, index_code: str = "000300", start_date: str = "",
                           end_date: str = "", auto_fetch: bool = True) -> List[DailyBar]:
        """Query benchmark index bars (e.g. 000300 / 000001)."""
        idx_sym = normalize_symbol(index_code)
        bars = self.storage.read_daily_bars(idx_sym, start_date, end_date, adjust_type="none")

        if bars:
            if not self.offline_mode and auto_fetch and end_date:
                if bars[-1].trade_date < end_date or len(bars) < 120:
                    fetch_start = start_date if len(bars) < 120 else bars[-1].trade_date
                    if not fetch_start:
                        fetch_start = (date.fromisoformat(end_date) - timedelta(days=400)).isoformat()
                    new_bars = self.providers.fetch_index_daily(idx_sym, fetch_start, end_date)
                    if new_bars:
                        self.storage.write_daily_bars(idx_sym, new_bars, adjust_type="none")
                        bars = self.storage.read_daily_bars(idx_sym, start_date, end_date, adjust_type="none")
            return bars

        if not self.offline_mode and auto_fetch:
            s_date = start_date or "2023-01-01"
            e_date = end_date or datetime.now().strftime("%Y-%m-%d")
            fetched = self.providers.fetch_index_daily(idx_sym, s_date, e_date)
            if fetched:
                self.storage.write_daily_bars(idx_sym, fetched, adjust_type="none")
                return self.storage.read_daily_bars(idx_sym, start_date, end_date, adjust_type="none")

        return []

    # --------------------------------------------------------------------------
    # Metadata & Universe Lookups
    # --------------------------------------------------------------------------

    def get_security_statuses(self, as_of: str, symbols=None):
        """Only dated exchange/company evidence may waive a target-day bar."""
        return load_nontrading_statuses(
            BASE_DIR / "configs" / "security_status_evidence.json", as_of, symbols)

    def get_stock_names(self) -> Dict[str, str]:
        if self._stock_names_cache is None:
            stocks = self.storage.get_stocks()
            self._stock_names_cache = {s.symbol: s.name for s in stocks}
        return dict(self._stock_names_cache)

    def get_industry_map(self) -> Dict[str, str]:
        if self._industry_map_cache is not None:
            return dict(self._industry_map_cache)

        # Try reading local industry_map.json
        ind_file = BASE_DIR / "data" / "industry_map.json"
        if ind_file.exists():
            try:
                mapping = json.loads(ind_file.read_text(encoding="utf-8"))
                self._industry_map_cache = {normalize_symbol(k): v for k, v in mapping.items()}
                return dict(self._industry_map_cache)
            except Exception:
                pass

        # Fallback to provider
        if not self.offline_mode:
            prov_map = self.providers.fetch_industry_map()
            if prov_map:
                self._industry_map_cache = {normalize_symbol(k): v for k, v in prov_map.items()}
                try:
                    ind_file.write_text(json.dumps(self._industry_map_cache, ensure_ascii=False, indent=2))
                except Exception:
                    pass
                return dict(self._industry_map_cache)

        return {}

    def get_universe_symbols(self) -> List[str]:
        """Returns tradable stock symbols list."""
        stocks = self.storage.get_stocks(status="L")
        if stocks:
            return [s.symbol for s in stocks]
        # Fallback to stock_universe.json
        u_file = BASE_DIR / "stock_universe.json"
        if u_file.exists():
            try:
                data = json.loads(u_file.read_text(encoding="utf-8"))
                return [normalize_symbol(s["symbol"]) for s in data.get("stocks", [])]
            except Exception:
                pass
        return []

    # --------------------------------------------------------------------------
    # Market Analysis & Unified Snapshot (Shared by Dual Accounts)
    # --------------------------------------------------------------------------

    def compute_market_snapshot(self, as_of: str, watchlist: Optional[List[str]] = None) -> MarketSnapshot:
        """
        Pure local offline calculation:
        1. Benchmark HS300 MA120/MA60/MA20
        2. Market Breadth (% above MA120)
        3. MarketRegime determination
        4. Sector Momentum scores & ranking
        5. Stock signals & ranking
        6. Produces immutable MarketSnapshot
        """
        symbols = watchlist or self.get_universe_symbols()
        industry_map = self.get_industry_map()
        nontrading = self.get_security_statuses(as_of, symbols)
        progress = self.storage.get_all_sync_progress()
        missing = [s for s in symbols if s not in progress
                   or progress[s].last_trade_date < as_of]
        excluded = sorted(set(missing) | set(nontrading))
        coverage = (len(symbols) - len(missing)) / len(symbols) if symbols else 0.0

        # 1. Fetch benchmark bars (HS300)
        bench_bars = self.get_benchmark_bars("000300", start_date="", end_date=as_of, auto_fetch=False)

        # 2. Compute Regime & Market Breadth (Unified with MarketRiskRule & zero lookahead)
        filtered_bench = [b for b in bench_bars if b.trade_date <= as_of] if bench_bars else []

        if not filtered_bench or len(filtered_bench) < 120 or filtered_bench[-1].trade_date != as_of:
            regime = MarketRegime.RISK_OFF
            breadth = None
            quality = DataQuality.INVALID
        else:
            hs_close = filtered_bench[-1].close
            ma120 = sum(b.close for b in filtered_bench[-120:]) / 120.0
            ma60 = sum(b.close for b in filtered_bench[-60:]) / 60.0
            ma20 = sum(b.close for b in filtered_bench[-20:]) / 20.0

            # Calculate breadth across universe without sampling bias
            sorted_symbols = sorted(symbols)
            if len(sorted_symbols) > 500:
                step = max(1, len(sorted_symbols) // 500)
                sample_syms = sorted_symbols[::step][:500]
            else:
                sample_syms = list(sorted_symbols)

            above_120_count = 0
            valid_sample_count = 0
            for sym in sample_syms:
                if sym in nontrading:
                    continue
                s_bars = self.storage.read_daily_bars(sym, end_date=as_of, use_cache=True)
                filtered_s = [b for b in s_bars if b.trade_date <= as_of] if s_bars else []
                if len(filtered_s) >= 120 and filtered_s[-1].trade_date == as_of:
                    valid_sample_count += 1
                    s_ma120 = sum(b.close for b in filtered_s[-120:]) / 120.0
                    if filtered_s[-1].close > s_ma120:
                        above_120_count += 1

            cov = (valid_sample_count / len(sample_syms)) if sample_syms else 0.0
            if cov < 0.60 or valid_sample_count < 10:
                breadth = None
                quality = DataQuality.INVALID
                regime = MarketRegime.RISK_OFF
            else:
                breadth = round(above_120_count / valid_sample_count, 4)
                quality = DataQuality.VALID
                regime, _, _, _ = MarketRiskRule.classify_regime_from_indicators(
                    close=hs_close, ma20=ma20, ma60=ma60, ma120=ma120,
                    breadth=breadth, breadth_min=REGIME_STRONG_BREADTH_MIN
                )

        # Convert benchmark bars to domain Bar models
        domain_bench_bars = [
            Bar(
                date=b.trade_date,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                amount=b.amount,
                raw_open=b.raw_open,
                raw_high=b.raw_high,
                raw_low=b.raw_low,
                raw_close=b.raw_close,
                adjustment_factor=b.adjustment_factor,
                source=b.source,
                raw_pre_close=b.raw_pre_close,
                is_suspended=b.is_suspended,
                is_st=b.is_st
            ) for b in bench_bars[-120:]
        ]

        snapshot_id = f"snap_{as_of}_{uuid.uuid4().hex[:8]}"
        now_str = datetime.utcnow().isoformat()

        snapshot = MarketSnapshot(
            snapshot_id=snapshot_id,
            signal_asof_date=as_of,
            available_at=now_str,
            universe_version="v3.0",
            sector_version="sw2021",
            bars_by_symbol={},  # Loaded on demand to conserve heap memory
            benchmark_bars=domain_bench_bars,
            market_regime=regime,
            quality=quality,
            market_breadth=round(breadth, 4) if breadth is not None else None,
            sector_map=industry_map,
            source_lineage={"storage": "parquet", "as_of": as_of,
                            "coverage_ratio": coverage,
                            "trading_ready": quality == DataQuality.VALID and coverage >= 0.70,
                            "excluded_symbols": excluded,
                            "nontrading_statuses": nontrading}
        )

        with self._lock:
            self._snapshot_cache[as_of] = snapshot
        return snapshot

    def get_market_snapshot(self, as_of: str, watchlist: Optional[List[str]] = None,
                            force_refresh: bool = False) -> MarketSnapshot:
        """Cached market snapshot retrieval, shared by all accounts."""
        with self._lock:
            if not force_refresh and as_of in self._snapshot_cache:
                return self._snapshot_cache[as_of]
        return self.compute_market_snapshot(as_of, watchlist)

    # --------------------------------------------------------------------------
    # Incremental Daily Sync & Resumable Tasks
    # --------------------------------------------------------------------------

    def _incremental_stock_start(self, symbol: str, last_date: str, target_day: date) -> str:
        """Use a narrow overlap normally; look back only across a verified halt.

        A source with sparse trading-day bars cannot reproduce another source's
        zero-volume suspension placeholders. Request enough real observations
        for storage to prove the common price basis, capped at 400 calendar days.
        Storage still performs the complete suspension-bridge admission checks.
        """
        if not last_date:
            return (target_day - timedelta(days=400)).isoformat()
        latest = date.fromisoformat(last_date)
        narrow = (latest - timedelta(days=10)).isoformat()
        recent = self.storage.read_daily_bars(symbol, start_date=narrow, end_date=last_date,
                                               adjust_type="qfq")
        real = [b for b in recent if b.volume > 0 and b.is_suspended is not True]
        if (len(real) >= 3 or not recent or recent[-1].is_suspended is not True
                or recent[-1].volume != 0):
            return narrow
        earliest = (latest - timedelta(days=400)).isoformat()
        history = self.storage.read_daily_bars(symbol, start_date=earliest, end_date=last_date,
                                                adjust_type="qfq")
        real = [b for b in history if b.volume > 0 and b.is_suspended is not True]
        if len(real) < 3:
            return earliest
        candidate = (date.fromisoformat(real[-3].trade_date) - timedelta(days=1)).isoformat()
        return max(earliest, candidate)

    def sync_daily_increment(self, trade_date: str, symbols: Optional[List[str]] = None, max_workers: int = 6,
                             on_progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, Any]:
        """Catch up a dated symbol scope, accepting only a completed target-day bar.

        A source returning no rows is not proof of suspension. Keep missing symbols
        pending with an explicit error so the scheduler can retry them safely.
        """
        target_day = date.fromisoformat(trade_date)
        if not is_trading_day(target_day):
            raise ValueError("target_date_not_a_trading_session")
        if trade_date > _last_completed_session():
            raise ValueError("target_session_not_completed")
        targets = list(dict.fromkeys(normalize_symbol(s) for s in
                       (symbols if symbols is not None else self.get_universe_symbols())))
        nontrading = self.get_security_statuses(trade_date, targets)
        progress_map = self.storage.get_all_sync_progress()
        pending = [s for s in targets if s not in nontrading and
                   (s not in progress_map or progress_map[s].last_trade_date < trade_date)]
        task_id = f"sync_{trade_date}_{uuid.uuid4().hex[:6]}"
        self.storage.create_sync_task(task_id, "INCREMENTAL", trade_date, len(pending))
        logger.info(f"Task {task_id}: Starting sync for {trade_date}, {len(pending)} pending out of {len(targets)} symbols.")
        success_count = fail_count = completed_count = 0

        benchmark_error = None
        try:
            self.get_benchmark_bars(
                "000300", start_date=(target_day - timedelta(days=400)).isoformat(),
                end_date=trade_date, auto_fetch=True)
            benchmark = self.storage.read_daily_bars("sh000300", end_date=trade_date, adjust_type="none")
            benchmark_valid = bool(len(benchmark) >= 120 and benchmark[-1].trade_date == trade_date)
            if not benchmark_valid:
                benchmark_error = "benchmark_target_day_missing_or_insufficient_history"
        except Exception as exc:
            benchmark_valid = False
            benchmark_error = type(exc).__name__

        from concurrent.futures import ThreadPoolExecutor, as_completed
        progress_lock = threading.Lock()

        def record_failure(sym, message):
            previous = self.storage.get_sync_progress(sym) or progress_map.get(sym)
            self.storage.update_sync_progress(
                symbol=sym, last_trade_date=previous.last_trade_date if previous else "",
                total_bars=previous.total_bars if previous else 0,
                first_trade_date=previous.first_trade_date if previous else "",
                last_source=previous.last_source if previous else "",
                failed_attempts=(previous.failed_attempts + 1) if previous else 1,
                last_error=message)

        def sync_one(sym):
            nonlocal success_count, fail_count, completed_count
            succeeded = False
            try:
                previous = progress_map.get(sym)
                # Request an overlap before the latest cached bar so storage can
                # validate/rebase the qfq basis while filling any missing days.
                # A newly added stock needs MA120 warm-up, not one isolated bar.
                start = self._incremental_stock_start(
                    sym, previous.last_trade_date if previous else "", target_day)
                admitted = getattr(self.providers, "supports_storage_admission", False) is True
                if admitted:
                    bars = self.providers.fetch_stock_daily_admitted(
                        sym, start, trade_date,
                        accept=lambda rows: self.storage.write_daily_bars(sym, rows, adjust_type="qfq"),
                        adjust_type="qfq")
                else:
                    bars = self.providers.fetch_stock_daily(sym, start, trade_date, adjust_type="qfq")
                bars = [b for b in (bars or []) if start <= b.trade_date <= trade_date and b.is_final]
                if bars and not admitted:
                    self.storage.write_daily_bars(sym, bars, adjust_type="qfq")
                current = self.storage.get_sync_progress(sym)
                succeeded = bool(current and current.last_trade_date >= trade_date
                                 and any(b.trade_date == trade_date for b in bars))
                if not succeeded:
                    record_failure(sym, "target_day_missing" if bars else "empty_or_nonfinal_provider_response")
            except Exception as exc:
                # Persist a bounded, credential-free diagnostic rather than a
                # response body or a URL that could contain provider credentials.
                record_failure(sym, type(exc).__name__)
            finally:
                with progress_lock:
                    success_count += int(succeeded)
                    fail_count += int(not succeeded)
                    completed_count += 1
                    idx = completed_count
                if on_progress:
                    on_progress(idx, len(pending), sym)
                if idx % 50 == 0 or idx == len(pending):
                    self.storage.update_sync_task(task_id, success_count, fail_count,
                                                 "COMPLETED" if idx == len(pending) and fail_count == 0 else "RUNNING")

        workers = max(1, min(max_workers or 6, 8))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(sync_one, s) for s in pending]
            for future in as_completed(futures):
                future.result()

        current_progress = self.storage.get_all_sync_progress()
        covered_count = sum(s in current_progress and current_progress[s].last_trade_date >= trade_date
                            for s in targets)
        missing = [s for s in targets if s not in nontrading and
                   (s not in current_progress or current_progress[s].last_trade_date < trade_date)]
        expected_count = len(targets) - len(nontrading)
        coverage = round(covered_count / len(targets), 6) if targets else 0.0
        status = "COMPLETED" if not missing and benchmark_valid and targets else "PARTIAL"
        self.storage.update_sync_task(task_id, success_count, fail_count, status)
        return {
            "task_id": task_id, "target_date": trade_date, "total_symbols": len(targets),
            "total_pending": len(pending), "success_count": success_count, "fail_count": fail_count,
            "covered_symbols": covered_count, "coverage": coverage,
            "benchmark_valid": benchmark_valid, "benchmark_error": benchmark_error,
            "missing_symbols": missing,
            "nontrading_statuses": nontrading,
            "expected_daily_bar_symbols": expected_count,
            "current_expected_symbols": expected_count - len(missing),
            "excluded_symbols": sorted(set(missing) | set(nontrading)),
            "status": status
        }
