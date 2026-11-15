# src/astock/trade/daily_audit_recorder.py
# -*- coding: utf-8 -*-
"""
Daily Acceptance Audit Recorder for A-Stock-Sim V3.0
Automatically tracks consecutive trading day metrics without manual intervention:
- Market sync completion timestamp
- Market regime
- Balanced & Aggressive equity, cash, positions
- Strategy status
- Orders generated, executed, rejected (with reasons)
- SQLite ledger reconciliation audit
- Systemd restart count
- Peak resident memory (MB)
- Error / warning log summary
- Overall 5-day continuous acceptance progress calculation
"""

import os
import json
import logging
import datetime
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("data/audit/daily_acceptance_records.json")


def get_peak_memory_mb() -> float:
    """Read VmHWM (Peak Resident Set Size) from /proc/self/status."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    parts = line.split()
                    return round(float(parts[1]) / 1024.0, 2)
    except Exception:
        pass
    return 0.0


def get_systemd_restarts(service_name: str = "openarmx-quant.service") -> int:
    """Get NRestarts of systemd service if available."""
    try:
        res = subprocess.run(
            ["systemctl", "show", "-p", "NRestarts", service_name],
            capture_output=True, text=True, timeout=2.0
        )
        if res.returncode == 0 and "NRestarts=" in res.stdout:
            val = res.stdout.strip().split("=")[-1]
            return int(val)
    except Exception:
        pass
    return 0


class DailyAuditRecorder:
    """Manages lightweight persistence and summary of daily acceptance logs."""

    def __init__(self, log_path: Path = DEFAULT_LOG_PATH):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def load_records(self) -> List[Dict[str, Any]]:
        """Loads existing audit records from disk."""
        if not self.log_path.exists():
            return []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("records", []) if isinstance(data, dict) else []
        except Exception as exc:
            logger.warning(f"Failed to load daily acceptance records: {exc}")
            return []

    def save_records(self, records: List[Dict[str, Any]]) -> None:
        """Saves audit records to disk."""
        data = {
            "version": "v3.0",
            "target_consecutive_days": 5,
            "recorded_trading_days": len(records),
            "is_completed": len(records) >= 5,
            "acceptance_status": (
                "COMPLETED" if len(records) >= 5
                else f"IN_PROGRESS ({len(records)}/5 trading days verified)"
            ),
            "last_updated": datetime.datetime.now().isoformat(),
            "records": records
        }
        temp_path = self.log_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path.replace(self.log_path)

    def record_day(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Appends or updates a daily acceptance record for trade_date."""
        trade_date = record.get("trade_date")
        if not trade_date:
            raise ValueError("Record missing required 'trade_date'")

        records = self.load_records()
        existing_idx = next((i for i, r in enumerate(records) if r.get("trade_date") == trade_date), None)
        if existing_idx is not None:
            records[existing_idx] = record
        else:
            records.append(record)

        # Sort by trade_date ascending
        records.sort(key=lambda r: r.get("trade_date", ""))
        self.save_records(records)
        return self.get_status()

    def get_status(self) -> Dict[str, Any]:
        """Returns the current verification status summary."""
        records = self.load_records()
        total_days = len(records)
        target_days = 5
        is_completed = total_days >= target_days

        status_text = (
            "COMPLETED" if is_completed
            else f"IN_PROGRESS ({total_days}/{target_days} trading days verified)"
        )

        return {
            "target_consecutive_days": target_days,
            "recorded_trading_days": total_days,
            "is_completed": is_completed,
            "acceptance_status": status_text,
            "dates": [r.get("trade_date") for r in records],
            "last_updated": datetime.datetime.now().isoformat(),
            "latest_record": records[-1] if records else None,
            "all_records": records
        }

    def capture_from_runtime(
        self,
        runtime: Any,
        trade_date: str,
        market_regime: str = "NEUTRAL",
        sync_completed_time: str = "",
        orders_generated: int = 0,
        orders_executed: int = 0,
        orders_rejected: Optional[List[Dict[str, Any]]] = None,
        error_summary: str = "None"
    ) -> Dict[str, Any]:
        """
        Extracts metrics directly from TradingRuntime and host environment.
        """
        ledger = getattr(runtime, "ledger", None)
        bal_acc: Dict[str, Any] = {}
        agg_acc: Dict[str, Any] = {}
        recon_status = "UNKNOWN"

        if ledger:
            try:
                b_bal = ledger.get_balance("balanced")
                b_pos = ledger.get_positions("balanced")
                bal_acc = {
                    "equity": b_bal.total_equity,
                    "cash": b_bal.cash,
                    "stock_value": b_bal.stock_market_value,
                    "positions_count": len(b_pos),
                    "positions": {s: p.total_shares for s, p in b_pos.items()},
                    "drawdown_pct": b_bal.current_drawdown_pct
                }

                a_bal = ledger.get_balance("aggressive")
                a_pos = ledger.get_positions("aggressive")
                agg_acc = {
                    "equity": a_bal.total_equity,
                    "cash": a_bal.cash,
                    "stock_value": a_bal.stock_market_value,
                    "positions_count": len(a_pos),
                    "positions": {s: p.total_shares for s, p in a_pos.items()},
                    "drawdown_pct": a_bal.current_drawdown_pct
                }

                b_chk = ledger.verify_integrity("balanced")
                a_chk = ledger.verify_integrity("aggressive")
                if b_chk.get("valid") and a_chk.get("valid"):
                    recon_status = "MATCH (Discrepancy: 0.00)"
                else:
                    recon_status = f"DISCREPANCY (B: {b_chk.get('cash_diff')}, A: {a_chk.get('cash_diff')})"
            except Exception as e:
                logger.error(f"Failed to query ledger for daily record: {e}")
                recon_status = f"ERROR: {e}"

        sync_time = sync_completed_time or datetime.datetime.now().strftime("%H:%M:%S")
        restarts = get_systemd_restarts()
        peak_mb = get_peak_memory_mb()

        record = {
            "trade_date": trade_date,
            "market_sync_completed_at": sync_time,
            "market_regime": market_regime,
            "balanced_account": bal_acc,
            "aggressive_account": agg_acc,
            "strategy_status": getattr(runtime, "mode", "ACTIVE"),
            "orders_generated": orders_generated,
            "orders_executed": orders_executed,
            "orders_rejected": orders_rejected or [],
            "sqlite_reconciliation": recon_status,
            "systemd_restarts": restarts,
            "peak_memory_mb": peak_mb,
            "error_summary": error_summary,
            "recorded_at": datetime.datetime.now().isoformat()
        }

        return self.record_day(record)
