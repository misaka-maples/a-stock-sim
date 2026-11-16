# src/astock/trade/daily_audit_recorder.py
# -*- coding: utf-8 -*-
"""
Daily Acceptance Audit Recorder for A-Stock-Sim V3.0
Automatically tracks consecutive trading day metrics without manual intervention:
- Real verified market sync completion (no forged system time)
- Market regime & breadth confirmation
- Balanced & Aggressive equity, cash, positions
- Strategy status & execution success
- Orders generated, executed, rejected (with reasons)
- SQLite ledger reconciliation audit (0.00 discrepancy required)
- Systemd restart count
- Peak resident memory (MB)
- Error / warning log summary
- Strict 5-day continuous A-share trading day acceptance calculation
"""

import os
import sys
import json
import logging
import datetime
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

try:
    import trade_calendar
except ImportError:
    root_dir = Path(__file__).resolve().parent.parent.parent.parent
    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))
    import trade_calendar

from .config import get_audit_path

logger = logging.getLogger(__name__)


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


def validate_acceptance_record(r: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validates that a daily acceptance record passes all strict health checks:
    - trade_date is a valid A-share trading day (skips weekends/holidays)
    - market_sync_completed_at is verified by real data task (cannot be unverified or forged)
    - sqlite_reconciliation has MATCH (0 discrepancy)
    - balanced and aggressive accounts exist, ran successfully, and have positive equity
    - strategy_status is not ERROR or FAILED
    - no fatal unhandled error in error_summary
    """
    reasons = []
    trade_date = r.get("trade_date")
    if not trade_date:
        reasons.append("Missing trade_date")
    else:
        try:
            if not trade_calendar.is_trading_day(trade_date):
                reasons.append(f"{trade_date} is not a valid A-share trading day (weekend or public holiday)")
        except Exception as e:
            reasons.append(f"Trade calendar error for {trade_date}: {e}")

    # Real data sync verification
    sync_time = r.get("market_sync_completed_at")
    if not sync_time or sync_time in ("UNVERIFIED", "None", ""):
        reasons.append("Market sync completion is unverified or missing (no real data task confirmation)")

    strat_status = r.get("strategy_status")
    if strat_status in ("ERROR", "FAILED"):
        reasons.append(f"Strategy execution failed with status: {strat_status}")

    recon = str(r.get("sqlite_reconciliation", ""))
    if "MATCH" not in recon or "DISCREPANCY" in recon or "ERROR" in recon:
        reasons.append(f"SQLite reconciliation failed: {recon}")

    b_acc = r.get("balanced_account", {})
    a_acc = r.get("aggressive_account", {})
    if not b_acc or b_acc.get("equity", 0) <= 0:
        reasons.append("Balanced account strategy did not execute successfully or equity <= 0")
    if not a_acc or a_acc.get("equity", 0) <= 0:
        reasons.append("Aggressive account strategy did not execute successfully or equity <= 0")

    err = str(r.get("error_summary", ""))
    if err and err != "None" and "error" in err.lower():
        reasons.append(f"Unhandled critical execution error: {err}")

    return len(reasons) == 0, reasons


def is_valid_acceptance_record(r: Dict[str, Any]) -> bool:
    """Convenience boolean check for acceptance record validity."""
    valid, _ = validate_acceptance_record(r)
    return valid


def compute_consecutive_valid_trading_days(records: List[Dict[str, Any]]) -> Tuple[int, List[str]]:
    """
    Finds consecutive sequence of valid A-share trading days.
    Returns (consecutive_count, sequence_of_dates).
    If multiple sequences exist, checks if any sequence has reached 5 days;
    otherwise returns the length of the latest continuous streak ending at the latest valid date.
    """
    if not records:
        return 0, []

    # Filter and deduplicate records by trade_date (keep latest record per date)
    by_date: Dict[str, Dict[str, Any]] = {}
    for r in records:
        td = r.get("trade_date")
        if td:
            by_date[td] = r

    sorted_dates = sorted(by_date.keys())
    valid_dates = [d for d in sorted_dates if is_valid_acceptance_record(by_date[d])]
    if not valid_dates:
        return 0, []

    chains: List[List[str]] = []
    current_chain = [valid_dates[0]]

    for i in range(1, len(valid_dates)):
        prev_d = valid_dates[i - 1]
        curr_d = valid_dates[i]
        try:
            expected_next = trade_calendar.get_next_trading_day(prev_d).isoformat()
            if curr_d == expected_next:
                current_chain.append(curr_d)
            else:
                chains.append(current_chain)
                current_chain = [curr_d]
        except Exception:
            chains.append(current_chain)
            current_chain = [curr_d]

    chains.append(current_chain)

    completed_chains = [c for c in chains if len(c) >= 5]
    if completed_chains:
        best_chain = max(completed_chains, key=len)
        return len(best_chain), best_chain

    latest_chain = chains[-1]
    return len(latest_chain), latest_chain


class DailyAuditRecorder:
    """Manages lightweight persistence and summary of daily acceptance logs."""

    def __init__(self, log_path: Optional[Union[str, Path]] = None):
        self.log_path = Path(log_path) if log_path is not None else get_audit_path()
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
        target_days = 5
        consecutive_count, streak_dates = compute_consecutive_valid_trading_days(records)
        is_completed = consecutive_count >= target_days
        status_text = (
            "COMPLETED" if is_completed
            else f"IN_PROGRESS ({consecutive_count}/{target_days} trading days verified)"
        )

        data = {
            "version": "v3.0",
            "target_consecutive_days": target_days,
            "recorded_trading_days": len(records),
            "consecutive_valid_days": consecutive_count,
            "streak_dates": streak_dates,
            "is_completed": is_completed,
            "acceptance_status": status_text,
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

        valid, failure_reasons = validate_acceptance_record(record)
        record["is_valid"] = valid
        record["failure_reasons"] = failure_reasons

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
        target_days = 5
        consecutive_count, streak_dates = compute_consecutive_valid_trading_days(records)
        is_completed = consecutive_count >= target_days

        status_text = (
            "COMPLETED" if is_completed
            else f"IN_PROGRESS ({consecutive_count}/{target_days} trading days verified)"
        )

        return {
            "target_consecutive_days": target_days,
            "recorded_trading_days": len(records),
            "consecutive_valid_days": consecutive_count,
            "streak_dates": streak_dates,
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
        Strictly requires verified market sync completion status (no forged system time).
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

        # Verified sync completion lookup: never forge with datetime.now()
        sync_time = sync_completed_time or getattr(runtime, "last_sync_completed_time", "")
        if not sync_time and hasattr(runtime, "data_scheduler") and runtime.data_scheduler:
            try:
                sched_status = runtime.data_scheduler.get_status()
                if sched_status.get("last_sync_date") == trade_date and sched_status.get("last_sync_result", {}).get("status") == "SUCCESS":
                    sync_time = sched_status["last_sync_result"].get("sync_res", {}).get("completed_at", "VERIFIED")
            except Exception:
                pass

        if not sync_time:
            sync_time = "UNVERIFIED"

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
