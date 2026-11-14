# src/astock/data/scheduler.py
import datetime
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from .service import AStockDataService
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
                 on_pipeline_completed: Optional[Callable[[str, Any], None]] = None):
        self.service = data_service
        self.on_pipeline_completed = on_pipeline_completed
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.last_sync_date: Optional[str] = None
        self.last_sync_result: Dict[str, Any] = {}
        self.is_syncing = False

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

                if self.last_sync_date != target_date and not self.is_syncing:
                    # Run daily pipeline for target_date
                    self.run_daily_pipeline(target_date)

            except Exception as exc:
                logger.error(f"Error in DataScheduler run loop: {exc}", exc_info=True)

            self._stop_event.wait(timeout=60.0)

    def run_daily_pipeline(self, target_date: str) -> Dict[str, Any]:
        """Runs the complete post-close ingestion & market analysis pipeline."""
        with self._lock:
            if self.is_syncing:
                return {"status": "ALREADY_RUNNING", "target_date": target_date}
            self.is_syncing = True

        logger.info(f"=== Starting Daily Market Data Pipeline for {target_date} ===")
        t0 = time.time()
        try:
            # 1. Sync daily increment
            sync_res = self.service.sync_daily_increment(target_date)
            logger.info(f"Daily sync completed: {sync_res}")

            # 2. Compute MarketSnapshot
            snapshot = self.service.compute_market_snapshot(target_date)
            logger.info(f"MarketSnapshot computed: Regime={snapshot.market_regime}, Breadth={snapshot.market_breadth}")

            # 3. Callback to trigger strategy portfolio update if registered
            if self.on_pipeline_completed:
                try:
                    self.on_pipeline_completed(target_date, snapshot)
                except Exception as exc:
                    logger.error(f"Callback on_pipeline_completed error: {exc}", exc_info=True)

            self.last_sync_date = target_date
            self.last_sync_result = {
                "target_date": target_date,
                "sync_res": sync_res,
                "regime": snapshot.market_regime.value,
                "breadth": snapshot.market_breadth,
                "duration_seconds": round(time.time() - t0, 2),
                "status": "SUCCESS"
            }
            return self.last_sync_result
        except Exception as exc:
            logger.error(f"Daily pipeline failed for {target_date}: {exc}", exc_info=True)
            self.last_sync_result = {
                "target_date": target_date,
                "error": str(exc),
                "status": "FAILED"
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
            "last_sync_result": self.last_sync_result
        }
