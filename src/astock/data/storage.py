# src/astock/data/storage.py
import os
import math
import statistics
from dataclasses import replace
import sqlite3
import threading
import tempfile
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .models import DailyBar, StockInfo, SyncProgress, SyncTaskRecord, normalize_symbol
from .quality import DataQualityValidator


# Parquet Schema for DailyBars
BAR_SCHEMA = pa.schema([
    ("symbol", pa.string()),
    ("trade_date", pa.string()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("amount", pa.float64()),
    ("source", pa.string()),
    ("adjust_type", pa.string()),
    ("updated_at", pa.string()),
    ("is_final", pa.bool_()),
    ("raw_open", pa.float64()),
    ("raw_high", pa.float64()),
    ("raw_low", pa.float64()),
    ("raw_close", pa.float64()),
    ("raw_pre_close", pa.float64()),
    ("adjustment_factor", pa.float64()),
    ("turnover_rate", pa.float64()),
    ("amplitude_pct", pa.float64()),
    ("change_pct", pa.float64()),
    ("is_suspended", pa.bool_()),
    ("is_st", pa.bool_()),
])


class BoundedMemoryCache:
    """Thread-safe LRU cache with capacity limit to prevent memory bloat."""
    def __init__(self, max_items: int = 500):
        self.max_items = max_items
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            self._hits += 1
            self._cache.move_to_end(key)
            return self._cache[key]

    def put(self, key: str, value: Any):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            if len(self._cache) > self.max_items:
                self._cache.popitem(last=False)

    def invalidate(self, key: str):
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            ratio = (self._hits / total) if total > 0 else 0.0
            return {
                "current_size": len(self._cache),
                "max_items": self.max_items,
                "hits": self._hits,
                "misses": self._misses,
                "hit_ratio": round(ratio, 4)
            }


class LocalStorageManager:
    """
    Unified local persistence engine:
    - SQLite: stocks metadata, sync progress, job records, provider metrics
    - Parquet: scalable columnar storage for daily bars with atomic safe write
    - In-memory cache: bounded LRU for hot queries
    """

    def __init__(self, base_dir: Optional[Path] = None, mem_cache_size: int = 400):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parents[3] / "data" / "storage"
        self.sqlite_dir = self.base_dir / "sqlite"
        self.parquet_dir = self.base_dir / "parquet"
        self.sqlite_dir.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.sqlite_dir / "metadata.db"
        self._db_lock = threading.Lock()
        self.mem_cache = BoundedMemoryCache(max_items=mem_cache_size)
        self._init_sqlite()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_sqlite(self):
        with self._db_lock, self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS stocks (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                industry TEXT DEFAULT '',
                area TEXT DEFAULT '',
                market TEXT DEFAULT '',
                list_date TEXT DEFAULT '',
                status TEXT DEFAULT 'L',
                is_st INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS sync_progress (
                symbol TEXT PRIMARY KEY,
                last_trade_date TEXT NOT NULL,
                total_bars INTEGER DEFAULT 0,
                first_trade_date TEXT DEFAULT '',
                last_source TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                failed_attempts INTEGER DEFAULT 0,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                target_date TEXT NOT NULL,
                total_symbols INTEGER DEFAULT 0,
                completed_symbols INTEGER DEFAULT 0,
                failed_symbols INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS provider_metrics (
                provider TEXT PRIMARY KEY,
                total_requests INTEGER DEFAULT 0,
                successful_requests INTEGER DEFAULT 0,
                failed_requests INTEGER DEFAULT 0,
                rate_limit_hits INTEGER DEFAULT 0,
                circuit_trips INTEGER DEFAULT 0,
                total_duration_ms REAL DEFAULT 0.0,
                last_error TEXT,
                last_updated TEXT DEFAULT ''
            );
            """)

    # --------------------------------------------------------------------------
    # Metadata & Progress Management
    # --------------------------------------------------------------------------

    def save_stocks(self, stocks: List[StockInfo]):
        if not stocks:
            return
        now_str = datetime.utcnow().isoformat()
        with self._db_lock, self._get_connection() as conn:
            conn.executemany("""
            INSERT INTO stocks (symbol, name, industry, area, market, list_date, status, is_st, updated_at)
            VALUES (:symbol, :name, :industry, :area, :market, :list_date, :status, :is_st, :updated_at)
            ON CONFLICT(symbol) DO UPDATE SET
                name=excluded.name,
                industry=CASE WHEN excluded.industry != '' THEN excluded.industry ELSE stocks.industry END,
                area=excluded.area,
                market=excluded.market,
                list_date=excluded.list_date,
                status=excluded.status,
                is_st=excluded.is_st,
                updated_at=excluded.updated_at;
            """, [
                {
                    "symbol": normalize_symbol(s.symbol),
                    "name": s.name,
                    "industry": s.industry or "",
                    "area": s.area or "",
                    "market": s.market or "",
                    "list_date": s.list_date or "",
                    "status": s.status or "L",
                    "is_st": 1 if s.is_st else 0,
                    "updated_at": s.updated_at or now_str
                } for s in stocks
            ])

    def get_stocks(self, status: str = "L") -> List[StockInfo]:
        with self._db_lock, self._get_connection() as conn:
            if status:
                rows = conn.execute("SELECT * FROM stocks WHERE status = ? ORDER BY symbol", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM stocks ORDER BY symbol").fetchall()
        return [
            StockInfo(
                symbol=r["symbol"],
                name=r["name"],
                industry=r["industry"],
                area=r["area"],
                market=r["market"],
                list_date=r["list_date"],
                status=r["status"],
                is_st=bool(r["is_st"]),
                updated_at=r["updated_at"]
            ) for r in rows
        ]

    def get_stock_info(self, symbol: str) -> Optional[StockInfo]:
        sym = normalize_symbol(symbol)
        with self._db_lock, self._get_connection() as conn:
            r = conn.execute("SELECT * FROM stocks WHERE symbol = ?", (sym,)).fetchone()
        if not r:
            return None
        return StockInfo(
            symbol=r["symbol"],
            name=r["name"],
            industry=r["industry"],
            area=r["area"],
            market=r["market"],
            list_date=r["list_date"],
            status=r["status"],
            is_st=bool(r["is_st"]),
            updated_at=r["updated_at"]
        )

    def update_sync_progress(self, symbol: str, last_trade_date: str, total_bars: int,
                             first_trade_date: str = "", last_source: str = "",
                             failed_attempts: int = 0, last_error: Optional[str] = None):
        sym = normalize_symbol(symbol)
        now_str = datetime.utcnow().isoformat()
        with self._db_lock, self._get_connection() as conn:
            conn.execute("""
            INSERT INTO sync_progress (symbol, last_trade_date, total_bars, first_trade_date, last_source, updated_at, failed_attempts, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                last_trade_date = CASE WHEN excluded.last_trade_date > sync_progress.last_trade_date THEN excluded.last_trade_date ELSE sync_progress.last_trade_date END,
                total_bars = excluded.total_bars,
                first_trade_date = CASE WHEN sync_progress.first_trade_date = '' THEN excluded.first_trade_date ELSE sync_progress.first_trade_date END,
                last_source = excluded.last_source,
                updated_at = excluded.updated_at,
                failed_attempts = excluded.failed_attempts,
                last_error = excluded.last_error;
            """, (sym, last_trade_date, total_bars, first_trade_date, last_source, now_str, failed_attempts, last_error))

    def get_sync_progress(self, symbol: str) -> Optional[SyncProgress]:
        sym = normalize_symbol(symbol)
        with self._db_lock, self._get_connection() as conn:
            r = conn.execute("SELECT * FROM sync_progress WHERE symbol = ?", (sym,)).fetchone()
        if not r:
            return None
        return SyncProgress(
            symbol=r["symbol"],
            last_trade_date=r["last_trade_date"],
            total_bars=r["total_bars"],
            first_trade_date=r["first_trade_date"],
            last_source=r["last_source"],
            updated_at=r["updated_at"],
            failed_attempts=r["failed_attempts"],
            last_error=r["last_error"]
        )

    def get_all_sync_progress(self) -> Dict[str, SyncProgress]:
        with self._db_lock, self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM sync_progress").fetchall()
        return {
            r["symbol"]: SyncProgress(
                symbol=r["symbol"],
                last_trade_date=r["last_trade_date"],
                total_bars=r["total_bars"],
                first_trade_date=r["first_trade_date"],
                last_source=r["last_source"],
                updated_at=r["updated_at"],
                failed_attempts=r["failed_attempts"],
                last_error=r["last_error"]
            ) for r in rows
        }

    # --------------------------------------------------------------------------
    # Sync Task Logging
    # --------------------------------------------------------------------------

    def create_sync_task(self, task_id: str, task_type: str, target_date: str, total_symbols: int) -> SyncTaskRecord:
        now_str = datetime.utcnow().isoformat()
        with self._db_lock, self._get_connection() as conn:
            conn.execute("""
            INSERT INTO sync_tasks (task_id, task_type, target_date, total_symbols, completed_symbols, failed_symbols, status, started_at)
            VALUES (?, ?, ?, ?, 0, 0, 'RUNNING', ?)
            """, (task_id, task_type, target_date, total_symbols, now_str))
        return SyncTaskRecord(
            task_id=task_id,
            task_type=task_type,
            target_date=target_date,
            total_symbols=total_symbols,
            completed_symbols=0,
            failed_symbols=0,
            status="RUNNING",
            started_at=now_str
        )

    def update_sync_task(self, task_id: str, completed_symbols: int, failed_symbols: int,
                         status: str, error_message: Optional[str] = None):
        now_str = datetime.utcnow().isoformat() if status in ("COMPLETED", "FAILED", "PARTIAL") else None
        with self._db_lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE sync_tasks SET
                completed_symbols = ?,
                failed_symbols = ?,
                status = ?,
                finished_at = COALESCE(?, finished_at),
                error_message = COALESCE(?, error_message)
            WHERE task_id = ?
            """, (completed_symbols, failed_symbols, status, now_str, error_message, task_id))

    def get_latest_tasks(self, limit: int = 10) -> List[SyncTaskRecord]:
        with self._db_lock, self._get_connection() as conn:
            rows = conn.execute("""
            SELECT * FROM sync_tasks ORDER BY started_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [
            SyncTaskRecord(
                task_id=r["task_id"],
                task_type=r["task_type"],
                target_date=r["target_date"],
                total_symbols=r["total_symbols"],
                completed_symbols=r["completed_symbols"],
                failed_symbols=r["failed_symbols"],
                status=r["status"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                error_message=r["error_message"]
            ) for r in rows
        ]

    # --------------------------------------------------------------------------
    # Parquet Time-Series Daily Bars Persistence
    # --------------------------------------------------------------------------

    def _parquet_path(self, symbol: str, adjust_type: str = "qfq") -> Path:
        sym = normalize_symbol(symbol)
        return self.parquet_dir / f"{sym}_{adjust_type}.parquet"

    def write_daily_bars(self, symbol: str, new_bars: List[DailyBar], adjust_type: str = "qfq") -> int:
        """
        Idempotent atomic safe write:
        1. Reads existing bars (if any) from Parquet.
        2. Merges new_bars, deduplicating on trade_date (latest/final wins).
        3. Validates OHLC and data quality.
        4. Writes to a temporary Parquet file in the same filesystem.
        5. Atomically renames temporary file to target path (os.replace).
        6. Updates sync_progress in SQLite.
        7. Invalidates memory cache for this symbol.
        Returns total bars stored.
        """
        if not new_bars:
            return 0

        sym = normalize_symbol(symbol)
        target_path = self._parquet_path(sym, adjust_type)

        existing_bars = self.read_daily_bars(sym, adjust_type=adjust_type, use_cache=False)
        # Independently downloaded qfq windows can use different adjustment
        # bases. Prove the common basis before appending, then rebase history.
        if (adjust_type == "qfq" and existing_bars and
                max(b.trade_date for b in new_bars) > existing_bars[-1].trade_date):
            old = {b.trade_date: b for b in existing_bars}
            new = {b.trade_date: b for b in new_bars}
            overlap = sorted(set(old) & set(new))[-3:]
            sparse_suspension_bridge = existing_bars[-1].trade_date not in overlap
            if sparse_suspension_bridge:
                # BaoStock keeps verified zero-volume suspension placeholders;
                # Tencent omits those dates. Bridge that representation difference
                # only with three actual trading observations on both sources.
                trading = [b for b in existing_bars if b.volume > 0 and b.is_suspended is not True]
                required = trading[-3:]
                if len(required) < 3 or any(b.trade_date not in new for b in required):
                    raise ValueError("qfq_increment_missing_overlap")
                anchor = required[-1]
                suspended_tail = [b for b in existing_bars if b.trade_date > anchor.trade_date]
                if not suspended_tail:
                    raise ValueError("qfq_increment_missing_overlap")
                for b in suspended_tail:
                    if (b.source != "baostock" or b.is_suspended is not True
                            or b.volume != 0 or b.amount != 0
                            or not math.isclose(b.adjustment_factor, anchor.adjustment_factor,
                                                rel_tol=1e-6, abs_tol=1e-8)
                            or any(not math.isclose(getattr(b, f), anchor.raw_close,
                                                    rel_tol=1e-6, abs_tol=1e-6)
                                   for f in ("raw_open", "raw_high", "raw_low", "raw_close"))
                            or any(not math.isclose(getattr(b, f), anchor.close,
                                                    rel_tol=1e-6, abs_tol=1e-6)
                                   for f in ("open", "high", "low", "close"))):
                        raise ValueError("qfq_increment_unverified_suspension_gap")
                    # A new response contradicting a verified suspended session
                    # requires investigation, never a silent overwrite.
                    if b.trade_date in new:
                        incoming = new[b.trade_date]
                        if incoming.volume != 0 or incoming.is_suspended is not True:
                            raise ValueError("qfq_increment_suspension_history_changed")
                overlap = [b.trade_date for b in required]
            elif len(overlap) < min(3, len(existing_bars)):
                raise ValueError("qfq_increment_missing_overlap")
            ratios = []
            for day in overlap:
                before, after = old[day], new[day]
                for field in ("raw_open", "raw_high", "raw_low", "raw_close", "volume"):
                    if not math.isclose(getattr(before, field), getattr(after, field),
                                        rel_tol=1e-4, abs_tol=(0.500001 if field == "volume" else 1e-4)):
                        raise ValueError("qfq_increment_raw_history_changed")
                ratios.append(after.close / before.close)
                ratios.append(after.adjustment_factor / before.adjustment_factor)
                if sparse_suspension_bridge:
                    ratios.extend(getattr(after, field) / getattr(before, field)
                                  for field in ("open", "high", "low"))
            ratio = statistics.median(ratios)
            if not math.isfinite(ratio) or ratio <= 0 or any(
                    not math.isclose(r, ratio, rel_tol=.001, abs_tol=1e-6) for r in ratios):
                raise ValueError("qfq_increment_inconsistent_adjustment_basis")
            existing_bars = [replace(b, open=b.open * ratio, high=b.high * ratio,
                low=b.low * ratio, close=b.close * ratio,
                adjustment_factor=b.adjustment_factor * ratio) for b in existing_bars]
        # Tencent reports integer lots; preserve a more precise overlapping
        # observation when the difference is only nearest-lot rounding.
        prior_by_date = {b.trade_date: b for b in existing_bars}
        new_bars = [replace(b, volume=prior_by_date[b.trade_date].volume)
                    if b.source == "tencent" and b.trade_date in prior_by_date
                    and b.volume == int(b.volume)
                    and abs(b.volume - prior_by_date[b.trade_date].volume) <= .500001
                    else b for b in new_bars]
        known_flags = {b.trade_date: b.is_st for b in existing_bars if b.is_st is not None}
        new_bars = [replace(b, is_st=known_flags[b.trade_date])
                    if b.is_st is None and b.trade_date in known_flags else b for b in new_bars]
        combined = existing_bars + new_bars
        cleaned = DataQualityValidator.clean_and_sort_bars(combined)
        if not cleaned:
            return 0

        # Build PyArrow Table
        data_dict = {
            "symbol": [b.symbol for b in cleaned],
            "trade_date": [b.trade_date for b in cleaned],
            "open": [b.open for b in cleaned],
            "high": [b.high for b in cleaned],
            "low": [b.low for b in cleaned],
            "close": [b.close for b in cleaned],
            "volume": [b.volume for b in cleaned],
            "amount": [b.amount for b in cleaned],
            "source": [b.source for b in cleaned],
            "adjust_type": [b.adjust_type for b in cleaned],
            "updated_at": [b.updated_at for b in cleaned],
            "is_final": [b.is_final for b in cleaned],
            "raw_open": [b.raw_open for b in cleaned],
            "raw_high": [b.raw_high for b in cleaned],
            "raw_low": [b.raw_low for b in cleaned],
            "raw_close": [b.raw_close for b in cleaned],
            "raw_pre_close": [b.raw_pre_close if b.raw_pre_close is not None else float("nan") for b in cleaned],
            "adjustment_factor": [b.adjustment_factor for b in cleaned],
            "turnover_rate": [b.turnover_rate if b.turnover_rate is not None else float("nan") for b in cleaned],
            "amplitude_pct": [b.amplitude_pct if b.amplitude_pct is not None else float("nan") for b in cleaned],
            "change_pct": [b.change_pct if b.change_pct is not None else float("nan") for b in cleaned],
            "is_suspended": [b.is_suspended for b in cleaned],
            "is_st": [b.is_st for b in cleaned],
        }
        table = pa.Table.from_pydict(data_dict, schema=BAR_SCHEMA)

        # Atomic Safe Write: write to .tmp then os.replace
        temp_fd, temp_path = tempfile.mkstemp(dir=str(self.parquet_dir), prefix=f".{sym}_", suffix=".tmp")
        os.close(temp_fd)
        try:
            pq.write_table(table, temp_path, compression="SNAPPY")
            os.replace(temp_path, str(target_path))
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

        # Invalidate memory cache
        cache_key = f"{sym}_{adjust_type}"
        self.mem_cache.invalidate(cache_key)

        # Update sync_progress
        last_date = cleaned[-1].trade_date
        first_date = cleaned[0].trade_date
        last_source = cleaned[-1].source
        self.update_sync_progress(
            symbol=sym,
            last_trade_date=last_date,
            total_bars=len(cleaned),
            first_trade_date=first_date,
            last_source=last_source,
            failed_attempts=0,
            last_error=None
        )

        return len(cleaned)

    def read_daily_bars(self, symbol: str, start_date: str = "", end_date: str = "",
                        adjust_type: str = "qfq", use_cache: bool = True) -> List[DailyBar]:
        """
        Reads daily bars for a symbol within [start_date, end_date].
        Uses LRU memory cache if requested.
        """
        sym = normalize_symbol(symbol)
        cache_key = f"{sym}_{adjust_type}"
        path = self._parquet_path(sym, adjust_type)
        try:
            stat = path.stat()
            version = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            self.mem_cache.invalidate(cache_key)
            return []

        all_bars = None
        if use_cache:
            cached = self.mem_cache.get(cache_key)
            if cached is not None and cached[0] == version:
                all_bars = cached[1]

        if all_bars is None:
            path = self._parquet_path(sym, adjust_type)
            if not path.exists():
                return []
            try:
                table = pq.read_table(str(path))
                df = table.to_pandas()
                bars = []
                for _, row in df.iterrows():
                    bars.append(DailyBar(
                        symbol=sym,
                        trade_date=str(row["trade_date"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        amount=float(row["amount"]),
                        source=str(row["source"]),
                        adjust_type=str(row["adjust_type"]),
                        updated_at=str(row["updated_at"]),
                        is_final=bool(row["is_final"]),
                        raw_open=float(row["raw_open"]),
                        raw_high=float(row["raw_high"]),
                        raw_low=float(row["raw_low"]),
                        raw_close=float(row["raw_close"]),
                        raw_pre_close=float(row["raw_pre_close"]) if pd.notna(row["raw_pre_close"]) else None,
                        adjustment_factor=float(row["adjustment_factor"]),
                        turnover_rate=float(row["turnover_rate"]) if pd.notna(row["turnover_rate"]) else None,
                        amplitude_pct=float(row["amplitude_pct"]) if pd.notna(row["amplitude_pct"]) else None,
                        change_pct=float(row["change_pct"]) if pd.notna(row["change_pct"]) else None,
                        is_suspended=bool(row["is_suspended"]),
                        is_st=bool(row["is_st"]) if pd.notna(row["is_st"]) else None,
                    ))
                all_bars = bars
                if use_cache:
                    self.mem_cache.put(cache_key, (version, all_bars))
            except Exception:
                return []

        # Filter by date range
        if not start_date and not end_date:
            return all_bars

        start = start_date or "1990-01-01"
        end = end_date or "9999-12-31"
        return [b for b in all_bars if start <= b.trade_date <= end]

    def read_universe_bars(self, symbols: List[str], start_date: str = "", end_date: str = "",
                           adjust_type: str = "qfq") -> Dict[str, List[DailyBar]]:
        """Batch query for multiple symbols."""
        result = {}
        for s in symbols:
            result[normalize_symbol(s)] = self.read_daily_bars(s, start_date, end_date, adjust_type)
        return result

    def get_latest_bar_date(self, symbol: str, adjust_type: str = "qfq") -> Optional[str]:
        prog = self.get_sync_progress(symbol)
        if prog and prog.last_trade_date:
            return prog.last_trade_date
        bars = self.read_daily_bars(symbol, adjust_type=adjust_type)
        return bars[-1].trade_date if bars else None

    def record_sync_task(self, task: SyncTaskRecord):
        """Idempotently records or updates a sync task record in SQLite."""
        with self._db_lock, self._get_connection() as conn:
            conn.execute("""
                INSERT INTO sync_tasks
                (task_id, task_type, target_date, total_symbols, completed_symbols,
                 failed_symbols, status, started_at, finished_at, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    completed_symbols=excluded.completed_symbols,
                    failed_symbols=excluded.failed_symbols,
                    status=excluded.status,
                    finished_at=excluded.finished_at,
                    error_message=excluded.error_message
            """, (task.task_id, task.task_type, task.target_date, task.total_symbols,
                  task.completed_symbols, task.failed_symbols, task.status,
                  task.started_at, task.finished_at, task.error_message))
            conn.commit()

    # Aliases for convenience and backward compatibility
    save_daily_bars = write_daily_bars
    get_daily_bars = read_daily_bars

    def get_storage_stats(self) -> Dict[str, Any]:
        """Summary of local storage assets."""
        with self._db_lock, self._get_connection() as conn:
            stock_cnt = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
            progress_cnt = conn.execute("SELECT COUNT(*) FROM sync_progress").fetchone()[0]
            latest_date = conn.execute("SELECT MAX(last_trade_date) FROM sync_progress").fetchone()[0]
        parquet_files = list(self.parquet_dir.glob("*.parquet"))
        total_parquet_bytes = sum(f.stat().st_size for f in parquet_files)
        return {
            "stocks_in_db": stock_cnt,
            "synced_symbols_count": progress_cnt,
            "latest_available_date": latest_date,
            "parquet_files_count": len(parquet_files),
            "parquet_storage_mb": round(total_parquet_bytes / (1024 * 1024), 2),
            "sqlite_db_path": str(self.db_path),
            "parquet_dir": str(self.parquet_dir)
        }
