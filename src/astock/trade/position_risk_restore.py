"""Restore evidenced legacy protection state without changing financial state.

This is an explicit migration helper, never an automatic runtime fallback. The
caller chooses one legacy strategy namespace and persists the returned audit.
"""
import datetime as dt
import hashlib
import json
import math
import os
from collections import defaultdict
from contextlib import closing
from pathlib import Path


def _number(value, *, zero=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and (value >= 0 if zero else value > 0) else None


def _shares(value):
    return value if type(value) is int and value > 0 else None


def _same_cost(first, second):
    # Legacy position averages and SQLite averages are stored at four decimals.
    return abs(first - second) <= 0.000050000001


def _lot_identity(rows, *, legacy):
    by_day = defaultdict(int)
    gross = 0.0
    fill_ids = set()
    for row in rows:
        count = _shares(row.get("shares" if legacy else "remaining_shares"))
        day = row.get("date" if legacy else "buy_date")
        price = _number(row.get("price" if legacy else "cost_price"))
        if not count or not isinstance(day, str) or not price:
            raise ValueError("missing_or_invalid_lot_evidence")
        if dt.date.fromisoformat(day).isoformat() != day:
            raise ValueError("invalid_lot_date")
        by_day[day] += count
        gross += count * price
        fill_id = row.get("fill_id")
        if fill_id and fill_id != "MIGRATION_LOT":
            fill_ids.add(str(fill_id))
    if not by_day:
        raise ValueError("missing_remaining_lots")
    return dict(by_day), gross, fill_ids


def restore_legacy_position_risk(ledger, account_id, snapshot_path, strategy_id):
    """Raise evidenced peak/stop fields only, returning a complete source audit.

    Missing or inconsistent evidence skips that holding. Financial columns,
    timestamps, lots and trade history are untouched. All checks and protection
    updates share one SQLite write transaction to avoid checking an old holding
    and subsequently updating a newly opened holding with the same symbol.
    """
    path = Path(snapshot_path).expanduser().resolve()
    audit = {"schema_version": 1, "account_id": account_id, "strategy_id": strategy_id,
             "source_path": str(path), "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
             "status": "SKIPPED", "updated_symbols": [], "positions": []}
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            payload = source.read()
            after = os.fstat(source.fileno())
        audit.update(source_sha256=hashlib.sha256(payload).hexdigest(),
                     source_mtime_ns=after.st_mtime_ns, source_size_bytes=len(payload),
                     source_mtime=dt.datetime.fromtimestamp(after.st_mtime, dt.timezone.utc).isoformat())
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            audit["reason"] = "source_changed_while_reading"
            return audit
        snapshot = json.loads(payload)
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot_is_not_an_object")
    except (OSError, ValueError, UnicodeError) as exc:
        audit["reason"] = "source_unavailable_or_invalid"
        audit["source_error"] = str(exc)
        return audit

    audit["source_updated_at"] = snapshot.get("updated_at")
    source_positions = snapshot.get("positions")
    states = snapshot.get("strategy_state")
    state = states.get(strategy_id) if isinstance(states, dict) else None
    monitors = state.get("position_monitors") if isinstance(state, dict) else None
    if not isinstance(source_positions, dict) or not isinstance(monitors, dict):
        audit["reason"] = "missing_positions_or_selected_strategy_monitors"
        return audit

    with closing(ledger.connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        holdings = conn.execute("SELECT * FROM positions WHERE account_id = ? ORDER BY symbol", (account_id,)).fetchall()
        for current in holdings:
            sym = current["symbol"]
            item = {"symbol": sym, "status": "SKIPPED"}
            audit["positions"].append(item)
            source = source_positions.get(sym)
            monitor = monitors.get(sym)
            if not isinstance(source, dict) or not isinstance(monitor, dict):
                item["reason"] = "missing_snapshot_position_or_selected_monitor"
                continue
            quantity = _shares(source.get("total_shares"))
            average = _number(source.get("cost_price"))
            monitor_cost = _number(monitor.get("cost_price"))
            current_cost = _number(current["avg_cost"])
            if quantity != current["total_shares"] or not quantity:
                item["reason"] = "quantity_mismatch"
                continue
            if not average or not monitor_cost or not current_cost or not (
                    _same_cost(average, current_cost) and _same_cost(monitor_cost, average)):
                item["reason"] = "average_or_monitor_cost_mismatch"
                continue
            if source.get("symbol", sym) != sym or monitor.get("symbol", sym) != sym:
                item["reason"] = "symbol_identity_mismatch"
                continue
            basis = source.get("cost_basis_cents")
            if basis is not None and (type(basis) is not int or basis <= 0 or
                    abs(basis / 100.0 - quantity * current_cost) > quantity * .00005 + .005):
                item["reason"] = "cost_basis_mismatch"
                continue
            lots = [dict(r) for r in conn.execute("""SELECT * FROM position_lots
                WHERE account_id = ? AND symbol = ? AND remaining_shares > 0
                ORDER BY buy_date, lot_id""", (account_id, sym))]
            try:
                source_lots = source.get("buy_lots")
                if not isinstance(source_lots, list):
                    raise ValueError("missing_source_buy_lots")
                source_days, source_gross, source_ids = _lot_identity(source_lots, legacy=True)
                current_days, current_gross, current_ids = _lot_identity(lots, legacy=False)
                if sum(source_days.values()) != quantity or sum(current_days.values()) != quantity:
                    raise ValueError("remaining_lot_quantity_mismatch")
                if min(source_days) != min(current_days):
                    raise ValueError("earliest_remaining_buy_date_mismatch")
                if source_days != current_days:
                    raise ValueError("lot_date_and_quantity_identity_mismatch")
                if source_ids and source_ids != current_ids:
                    raise ValueError("source_fill_ids_mismatch")
                if abs(source_gross - current_gross) > quantity * .00000001:
                    # Some verified migrations store the fee-inclusive position
                    # average in every lot instead of its raw execution price.
                    # Accept only that explicit representation, evidenced by the
                    # original cost basis; never invent a fee estimate.
                    if not (basis is not None and source_gross <= basis / 100.0 + .005 and
                            all(_same_cost(r["cost_price"], average) for r in lots)):
                        raise ValueError("unexplained_lot_execution_cost_mismatch")
                    item["lot_cost_representation"] = "verified_source_average_cost"
            except (ValueError, TypeError, AttributeError) as exc:
                item["reason"] = str(exc)
                continue
            peak = _number(monitor.get("peak_price"))
            stop = _number(monitor.get("trailing_stop_line"), zero=True)
            if peak is None or stop is None or stop > peak:
                item["reason"] = "invalid_or_unsupported_peak_and_stop"
                continue
            item["evidence"] = {"shares": quantity, "snapshot_average_cost": average,
                "monitor_cost": monitor_cost, "ledger_average_cost": current_cost,
                "cost_basis_cents": basis, "earliest_remaining_buy_date": min(current_days),
                "source_peak_price": peak, "source_trailing_stop_line": stop}
            old_peak = _number(current["highest_price"], zero=True) or 0.0
            old_stop = _number(current["trailing_stop_price"], zero=True) or 0.0
            new_peak, new_stop = max(old_peak, peak), max(old_stop, stop)
            if new_stop > new_peak:
                item["reason"] = "existing_stop_exceeds_verified_peak"
                continue
            item.update(before={"highest_price": old_peak, "trailing_stop_price": old_stop},
                        after={"highest_price": new_peak, "trailing_stop_price": new_stop})
            if new_peak == old_peak and new_stop == old_stop:
                item["reason"] = "already_at_or_above_verified_protection"
                continue
            conn.execute("""UPDATE positions SET highest_price = ?, trailing_stop_price = ?
                WHERE account_id = ? AND symbol = ?""", (new_peak, new_stop, account_id, sym))
            item.update(status="RESTORED", reason="verified_same_legacy_holding")
            audit["updated_symbols"].append(sym)
    audit["status"] = "RESTORED" if audit["updated_symbols"] else "SKIPPED"
    audit["reason"] = "verified_protection_restored" if audit["updated_symbols"] else "no_eligible_protection_to_raise"
    return audit
