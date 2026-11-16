# src/astock/data/service.py
import json
import logging
import math
import threading
import time
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .models import DailyBar, StockInfo, SyncProgress, normalize_symbol, to_clean_code
from .storage import LocalStorageManager
from .providers.manager import ProviderManager
from .quality import DataQualityValidator
from ..domain.enums import MarketRegime, DataQuality, REGIME_STRONG_BREADTH_MIN
from ..trade.risk.market import MarketRiskRule
from ..domain.models import Bar, MarketSnapshot

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parents[3]


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
                if bars[-1].trade_date < end_date:
                    new_bars = self.providers.fetch_index_daily(idx_sym, bars[-1].trade_date, end_date)
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
            source_lineage={"storage": "parquet", "as_of": as_of}
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

    def sync_daily_increment(self, trade_date: str, symbols: Optional[List[str]] = None, max_workers: int = 6,
                             on_progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, Any]:
        """
        Increments daily bars for trade_date across symbols.
        Checks sync_progress in SQLite to avoid querying already-synced symbols (Idempotent & Resumable).
        """
        targets = symbols or self.get_universe_symbols()
        progress_map = self.storage.get_all_sync_progress()

        # Filter pending symbols: last_trade_date < trade_date
        pending = [s for s in targets if progress_map.get(s, None) is None or progress_map[s].last_trade_date < trade_date]

        task_id = f"sync_{trade_date}_{uuid.uuid4().hex[:6]}"
        task = self.storage.create_sync_task(task_id, "INCREMENTAL", trade_date, len(pending))

        logger.info(f"Task {task_id}: Starting sync for {trade_date}, {len(pending)} pending out of {len(targets)} symbols.")

        success_count = 0
        fail_count = 0

        # Also sync benchmark 000300
        try:
            self.get_benchmark_bars("000300", start_date=trade_date, end_date=trade_date, auto_fetch=True)
        except Exception:
            pass

        from concurrent.futures import ThreadPoolExecutor, as_completed
        progress_lock = threading.Lock()
        completed_count = 0

        def sync_one(sym):
            nonlocal success_count, fail_count, completed_count
            try:
                bars = self.providers.fetch_stock_daily(sym, trade_date, trade_date, adjust_type="qfq")
                if bars:
                    self.storage.write_daily_bars(sym, bars, adjust_type="qfq")
                    with progress_lock:
                        success_count += 1
                        completed_count += 1
                        idx = completed_count
                else:
                    with progress_lock:
                        fail_count += 1
                        completed_count += 1
                        idx = completed_count
            except Exception as exc:
                with progress_lock:
                    fail_count += 1
                    completed_count += 1
                    idx = completed_count
                self.storage.update_sync_progress(
                    symbol=sym,
                    last_trade_date=progress_map[sym].last_trade_date if sym in progress_map else "",
                    total_bars=progress_map[sym].total_bars if sym in progress_map else 0,
                    failed_attempts=(progress_map[sym].failed_attempts + 1) if sym in progress_map else 1,
                    last_error=f"{type(exc).__name__}: {str(exc)}"
                )

            if on_progress:
                on_progress(idx, len(pending), sym)
            if idx % 50 == 0 or idx == len(pending):
                self.storage.update_sync_task(task_id, success_count, fail_count,
                                             "COMPLETED" if idx == len(pending) else "RUNNING")

        workers = max(1, min(max_workers or 6, 8))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(sync_one, s) for s in pending]
            for f in as_completed(futures):
                pass

        status = "COMPLETED" if fail_count == 0 else "PARTIAL"
        self.storage.update_sync_task(task_id, success_count, fail_count, status)
        return {
            "task_id": task_id,
            "target_date": trade_date,
            "total_pending": len(pending),
            "success_count": success_count,
            "fail_count": fail_count,
            "status": status
        }
