# src/astock/data/migration.py
import json
import logging
import os
from pathlib import Path
from typing import Optional

from .models import DailyBar, normalize_symbol
from .storage import LocalStorageManager

logger = logging.getLogger(__name__)


def migrate_legacy_json_cache(json_dir: Path, storage: LocalStorageManager,
                              batch_size: int = 200, limit: Optional[int] = None) -> int:
    """
    Migrates existing data/history_cache/*.json files into Parquet and SQLite metadata.
    Avoids re-downloading years of historical bars from external APIs.
    """
    json_path = Path(json_dir)
    if not json_path.exists():
        return 0

    json_files = list(json_path.glob("*_qfq.json"))
    if limit:
        json_files = json_files[:limit]

    migrated_count = 0
    logger.info(f"Starting migration of {len(json_files)} legacy JSON cache files...")

    for fpath in json_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)

            symbol = data.get("symbol")
            if not symbol:
                continue

            sym = normalize_symbol(symbol)
            bars_data = data.get("bars", [])
            if not bars_data:
                continue

            # Convert to DailyBar instances
            daily_bars = []
            for b in bars_data:
                daily_bars.append(DailyBar(
                    symbol=sym,
                    trade_date=str(b["date"]),
                    open=float(b["open"]),
                    high=float(b["high"]),
                    low=float(b["low"]),
                    close=float(b["close"]),
                    volume=float(b["volume"]),
                    amount=float(b.get("amount", 0.0)),
                    source=str(b.get("source", "baostock")),
                    adjust_type="qfq",
                    updated_at=str(b.get("updated_at") or data.get("metadata", {}).get("fetched_at", "")),
                    is_final=True,
                    raw_open=float(b.get("raw_open", b["open"])),
                    raw_high=float(b.get("raw_high", b["high"])),
                    raw_low=float(b.get("raw_low", b["low"])),
                    raw_close=float(b.get("raw_close", b["close"])),
                    raw_pre_close=float(b["raw_pre_close"]) if b.get("raw_pre_close") is not None else None,
                    adjustment_factor=float(b.get("adjustment_factor", 1.0)),
                    is_suspended=bool(b.get("is_suspended", False)),
                    is_st=bool(b.get("is_st", False))
                ))

            storage.write_daily_bars(sym, daily_bars, adjust_type="qfq")
            migrated_count += 1
        except Exception as exc:
            logger.warning(f"Failed to migrate {fpath.name}: {exc}")
            continue

    logger.info(f"Migration completed: {migrated_count} symbols converted to Parquet.")
    return migrated_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    base = Path(__file__).resolve().parents[4]
    jdir = base / "data" / "history_cache"
    mgr = LocalStorageManager(base / "data" / "storage")
    res = migrate_legacy_json_cache(jdir, mgr)
    print(f"Migrated {res} stocks. Stats: {mgr.get_storage_stats()}")
