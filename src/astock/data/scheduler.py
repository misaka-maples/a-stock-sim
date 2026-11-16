# src/astock/data/scheduler.py
import datetime
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from .service import AStockDataService
from ..domain.enums import DataQuality
from ..domain.models import MarketSnapshot
from trade_calendar import BEIJING, is_trading_day, get_previous_trading_day

logger = logging.getLogger(__name__)


class DataScheduler:
    """
    Automated market data ingestion and computation scheduler:
    1. A-share exchange calendar aware (skips weekends and public holidays)
    2. Post-close settlement synchronization pipeline (16:30+ Beijing time)
    3. Triggers cross-market regime analysis and dual-account target portfolio planning
    4. Resumable execution with SQLite checkpointing and error isolation
    5. Manual trigger support for ad-hoc synchronizations
    """

    def __init__(self, data_service: AStockDataService,
                 on_pipeline_completed: Optional[Callable[[str, Any], None]] = None,
                 sync_symbols: Optional[List[str]] = None):
        self.service = data_service
        self.on_pipeline_completed = on_pipeline_completed
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.sync_symbols = list(dict.fromkeys(sync_symbols)) if sync_symbols is not None else None
        self.last_sync_date: Optional[str] = None  # Last successful target, not last attempt.
        self.last_usable_date: Optional[str] = None
        self.last_attempt_date: Optional[str] = None
        self._retry_failures = 0
        self._next_retry_at = 0.0
        self._retry_delays = (300.0, 900.0, 1800.0)
        self.last_sync_result: Dict[str, Any] = {}
        self.latest_snapshot: Optional[MarketSnapshot] = None
        self.is_syncing = False

    def get_latest_snapshot(self) -> Optional[MarketSnapshot]:
        """Returns the latest verified MarketSnapshot computed by the daily pipeline."""
        with self._lock:
            return self.latest_snapshot

    def set_sync_symbols(self, symbols: List[str]):
        """Bind ingestion to the configured trading pool without rewriting stock metadata."""
        with self._lock:
            self.sync_symbols = list(dict.fromkeys(symbols))

    def should_run(self, target_date: str) -> bool:
        return (not self.is_syncing and self.last_sync_date != target_date
                and (self.last_attempt_date != target_date or time.monotonic() >= self._next_retry_at))

    def _record_outcome(self, target_date: str, succeeded: bool):
        if succeeded:
            self.last_sync_date = target_date
            self._retry_failures = 0
            self._next_retry_at = 0.0
        else:
            self._retry_failures += 1
            delay = self._retry_delays[min(self._retry_failures - 1, len(self._retry_delays) - 1)]
            self._next_retry_at = time.monotonic() + delay

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="DataSchedulerThread", daemon=True)
            self._thread.start()
            logger.info("DataScheduler background thread started.")

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        logger.info("DataScheduler background thread stopped.")

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                now = datetime.datetime.now(BEIJING)
                today_str = now.date().isoformat()

                # Check if today is a trading day and post-close (> 16:30)
                if is_trading_day(now.date()) and now.time() >= datetime.time(16, 30):
                    target_date = today_str
                else:
                    target_date = get_previous_trading_day(now.date()).isoformat()

                if self.should_run(target_date):
                    # Run daily pipeline for target_date
                    self.run_daily_pipeline(target_date)

            except Exception as exc:
                logger.error(f"Error in DataScheduler run loop: {exc}", exc_info=True)

            self._stop_event.wait(timeout=60.0)

    def run_daily_pipeline(self, target_date: str) -> Dict[str, Any]:
        """Runs the complete post-close ingestion & market analysis pipeline with strict validation."""
        with self._lock:
            if self.is_syncing:
                return {"status": "ALREADY_RUNNING", "target_date": target_date}
            self.is_syncing = True
            if self.last_attempt_date != target_date:
                self._retry_failures = 0
            self.last_attempt_date = target_date

        logger.info(f"=== Starting Daily Market Data Pipeline for {target_date} ===")
        t0 = time.time()
        task_id = f"pipeline_{target_date}_{int(time.time())}"
        completed_at = datetime.datetime.now().isoformat()
        try:
            # 1. Sync daily increment
            sync_res = (self.service.sync_daily_increment(target_date, symbols=list(self.sync_symbols))
                        if self.sync_symbols is not None else
                        self.service.sync_daily_increment(target_date))
            logger.info(f"Daily sync completed: {sync_res}")
            sync_status = sync_res.get("status", "FAILED")
            fail_count = sync_res.get("fail_count", 0)

            # Check actual universe stock data coverage
            get_univ = getattr(self.service, "get_universe_symbols", getattr(self.service, "get_stock_pool", lambda: []))
            universe_syms = self.sync_symbols if self.sync_symbols is not None else get_univ()
            total_syms = len(universe_syms)
            if "coverage" in sync_res:
                coverage = float(sync_res["coverage"])
            elif total_syms > 0:
                coverage = round(max(0.0, (total_syms - fail_count) / total_syms), 4)
            else:
                synced = sync_res.get("synced_symbols", 0)
                coverage = round(synced / (synced + fail_count), 4) if (synced + fail_count) > 0 else 1.0

            # 2. Compute MarketSnapshot
            get_snap = getattr(self.service, "compute_market_snapshot", getattr(self.service, "get_market_snapshot", None))
            snapshot = (get_snap(target_date, watchlist=list(self.sync_symbols))
                        if self.sync_symbols is not None else get_snap(target_date))
            logger.info(f"MarketSnapshot computed: Regime={snapshot.market_regime}, Breadth={snapshot.market_breadth}, Quality={snapshot.quality}")

            # 3. Validate benchmark index completeness (HS300 >= 120 bars ending on target_date)
            bench_bars = snapshot.benchmark_bars
            bench_valid = bool(bench_bars and len(bench_bars) >= 120 and getattr(bench_bars[-1], "date", getattr(bench_bars[-1], "trade_date", "")) == target_date)

            # 4. Strict Quality & Success Evaluation
            # Requirements:
            # - sync_status must be COMPLETED (not PARTIAL, not FAILED)
            # - snapshot.quality must be VALID (not INVALID or DEGRADED)
            # - benchmark bars valid & current
            # - coverage ratio >= 70% (0.70)
            trading_ready = (
                sync_status in ("COMPLETED", "PARTIAL") and
                snapshot.quality == DataQuality.VALID and
                bench_valid and
                coverage >= 0.70
            )
            # Completeness and usability are separate. A missing individual
            # security is excluded from buys, while a valid shared market
            # snapshot can serve both accounts. Incomplete ingestion retries.
            quality_passed = sync_status == "COMPLETED" and trading_ready
            excluded = sorted(set(sync_res.get("excluded_symbols", [])) |
                              set(getattr(snapshot, "source_lineage", {}).get("excluded_symbols", [])))

            # Status classification:
            # SUCCESS: 100% of checks passed
            # PARTIAL: sync completed partially or coverage < 70%
            # FAILED: critical quality failure, missing benchmark, or exception
            if quality_passed:
                pipeline_status = "SUCCESS"
            elif getattr(snapshot, "quality", None) == DataQuality.INVALID or not bench_valid:
                pipeline_status = "FAILED"
            elif sync_status == "PARTIAL" or coverage < 0.70:
                pipeline_status = "PARTIAL"
            else:
                pipeline_status = "FAILED"

            completed_at = datetime.datetime.now().isoformat()
            self._record_outcome(target_date, quality_passed)
            newly_usable = trading_ready and self.last_usable_date != target_date
            if trading_ready:
                self.latest_snapshot = snapshot
                self.last_usable_date = target_date

            result = {
                "task_id": task_id,
                "trade_date": target_date,
                "data_version": getattr(snapshot, "universe_version", "v3.0"),
                "sync_res": sync_res,
                "sync_status": sync_status,
                "status": pipeline_status,
                "quality_passed": quality_passed,
                "trading_ready": trading_ready,
                "data_complete": quality_passed,
                "excluded_symbols": excluded,
                "missing_symbols": sync_res.get("missing_symbols", []),
                "nontrading_statuses": sync_res.get("nontrading_statuses", {}),
                "quality": snapshot.quality.value if hasattr(snapshot.quality, "value") else str(snapshot.quality),
                "coverage": coverage,
                "benchmark_valid": bench_valid,
                "regime": snapshot.market_regime.value if hasattr(snapshot.market_regime, "value") else str(snapshot.market_regime),
                "breadth": snapshot.market_breadth,
                "duration_seconds": round(time.time() - t0, 2),
                "completed_at": completed_at
            }
            self.last_sync_result = result

            # 5. Callback to trigger strategy portfolio update if registered
            if newly_usable and self.on_pipeline_completed:
                try:
                    self.on_pipeline_completed(target_date, snapshot)
                except Exception as exc:
                    logger.error(f"Callback on_pipeline_completed error: {exc}", exc_info=True)

            return result
        except Exception as exc:
            logger.error(f"Daily pipeline failed for {target_date}: {exc}", exc_info=True)
            self._record_outcome(target_date, False)
            self.last_sync_result = {
                "task_id": task_id,
                "trade_date": target_date,
                "error": str(exc),
                "status": "FAILED",
                "quality_passed": False,
                "trading_ready": False,
                "data_complete": False,
                "coverage": 0.0,
                "completed_at": datetime.datetime.now().isoformat()
            }
            return self.last_sync_result
        finally:
            with self._lock:
                self.is_syncing = False

    def trigger_manual_sync(self, target_date: Optional[str] = None, trade_date: Optional[str] = None) -> Dict[str, Any]:
        """Manually trigger sync pipeline in a separate thread."""
        t_date = trade_date or target_date or datetime.datetime.now(BEIJING).date().isoformat()
        thread = threading.Thread(target=self.run_daily_pipeline, args=(t_date,), name=f"ManualSync_{t_date}")
        thread.start()
        return {"status": "TRIGGERED", "target_date": t_date}

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_running": self._running,
            "is_syncing": self.is_syncing,
            "last_sync_date": self.last_sync_date,
            "last_usable_date": self.last_usable_date,
            "last_attempt_date": self.last_attempt_date,
            "retry_failures": self._retry_failures,
            "next_retry_seconds": max(0, round(self._next_retry_at - time.monotonic())) if self._next_retry_at else 0,
            "sync_scope_count": len(self.sync_symbols) if self.sync_symbols is not None else None,
            "last_sync_result": self.last_sync_result
        }
