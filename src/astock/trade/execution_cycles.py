"""Persistent entry-cycle discipline, independent of trading and financial state.

A cycle freezes the first scheduled set of new targets. Later polls can retry an
unfilled original target, but cannot replace it or enlarge it using released cash.
"""
import datetime as dt
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from trade_calendar import is_trading_day
from .models import TargetPosition


def _setting(policy, name, default):
    return policy.get(name, default) if isinstance(policy, dict) else getattr(policy, name, default)


def _sessions_between(start, end):
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if first > last:
        return -1
    count = 0
    while first < last:
        first += dt.timedelta(days=1)
        count += bool(is_trading_day(first))
    return count


def _positive(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if not isinstance(value, bool) and math.isfinite(result) and result > 0 else 0.0


def restrict_entry_cycle(ledger, account_id, signal_day, execution_day,
                         target_portfolio, positions, policy, *, persist=True):
    """Return a restricted plan and diagnostics without submitting any orders.

    ``policy=None`` preserves legacy API behavior. ``persist=False`` opens the
    ledger strictly read-only and never creates tables or updates cycle state.
    Existing holdings are always retained in the returned plan.
    """
    if policy is None:
        return target_portfolio, {"policy_applied": False, "reason": "legacy_passthrough"}
    dt.date.fromisoformat(signal_day)
    dt.date.fromisoformat(execution_day)
    if signal_day > execution_day:
        raise ValueError("signal_day must not follow execution_day")
    rebalance_every = max(1, int(_setting(policy, "rebalance_every", 5)))
    cooldown_days = max(0, int(_setting(policy, "cooldown_days", 3)))
    refill = bool(_setting(policy, "refill_vacancies", False))
    diag = {"policy_applied": True, "persist": bool(persist),
            "signal_day": signal_day, "execution_day": execution_day,
            "refill_vacancies": refill, "cycle_started": False,
            "blocked_symbols": {}, "allowed_new_symbols": []}

    if persist:
        conn = ledger.connect()
    else:
        conn = sqlite3.connect(Path(ledger.db_path).resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    with closing(conn), conn:
        if persist:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS execution_cycles (
                account_id TEXT NOT NULL,
                signal_day TEXT NOT NULL,
                execution_day TEXT NOT NULL,
                targets_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, signal_day)
            )""")
        has_table = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_cycles'").fetchone())
        last = (conn.execute("""SELECT * FROM execution_cycles WHERE account_id = ?
                    ORDER BY signal_day DESC, created_at DESC LIMIT 1""", (account_id,)).fetchone()
                if has_table else None)
        account = conn.execute("SELECT total_cash_cents FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
        if account is None:
            raise ValueError("Account not found: " + account_id)
        equity = float(account["total_cash_cents"]) / 100.0 + sum(float(p.market_value) for p in positions.values())
        held = {sym: TargetPosition(symbol=sym,
                    target_weight=round(float(pos.market_value) / equity, 4) if equity > 0 else 0.0,
                    reason="HOLD_EXISTING") for sym, pos in positions.items()}
        proposed = {sym: tp for sym, tp in target_portfolio.target_positions.items()
                    if sym not in positions and _positive(tp.target_weight)}
        sells_today = {r["symbol"] for r in conn.execute("""SELECT DISTINCT symbol FROM fills
            WHERE account_id = ? AND side = 'SELL' AND trade_date = ?""", (account_id, execution_day))}
        sold_dates = {r["symbol"]: r["last_sell"] for r in conn.execute("""SELECT symbol, MAX(trade_date) AS last_sell
            FROM fills WHERE account_id = ? AND side = 'SELL' AND trade_date <= ? GROUP BY symbol""",
            (account_id, execution_day))}
        bought_today = {r["symbol"] for r in conn.execute("""SELECT DISTINCT symbol FROM fills
            WHERE account_id = ? AND side = 'BUY' AND trade_date = ?""", (account_id, execution_day))}

        if target_portfolio.constraints.get("is_frozen", False):
            diag["reason"] = "risk_freeze"
            permitted = {}
        elif sells_today and not refill:
            diag["reason"] = "same_day_sell_blocks_new_entries"
            diag["sold_today"] = sorted(sells_today)
            permitted = {}
        elif last and signal_day < last["signal_day"]:
            diag["reason"] = "signal_precedes_last_cycle"
            permitted = {}
        else:
            same_cycle = bool(last and last["signal_day"] == signal_day)
            elapsed = _sessions_between(last["signal_day"], signal_day) if last else None
            diag["sessions_since_cycle"] = elapsed
            diag["last_cycle_signal_day"] = last["signal_day"] if last else None
            due = last is None or (elapsed is not None and elapsed >= rebalance_every)
            if same_cycle and last["execution_day"] == execution_day:
                # Reuse only currently eligible members, never add a replacement.
                stored = json.loads(last["targets_json"])
                permitted = {sym: replace(proposed[sym],
                                target_weight=min(_positive(row["target_weight"]), proposed[sym].target_weight),
                                target_shares=row.get("target_shares"))
                             for sym, row in stored.items() if sym in proposed}
                diag["reason"] = "retry_original_cycle_targets"
                for sym in proposed.keys() - permitted.keys():
                    diag["blocked_symbols"][sym] = "not_in_original_cycle"
            elif same_cycle:
                permitted = {}
                diag["reason"] = "cycle_execution_day_expired"
            elif due or refill:
                permitted = dict(proposed)
                diag["reason"] = "scheduled_cycle" if due else "explicit_refill_enabled"
                # Do not advance the normal clock because data/freeze produced no targets.
                if due and permitted:
                    frozen_targets = {sym: tp.to_dict() for sym, tp in permitted.items()}
                    if persist:
                        conn.execute("""INSERT INTO execution_cycles
                            (account_id, signal_day, execution_day, targets_json, created_at)
                            VALUES (?, ?, ?, ?, ?)""", (account_id, signal_day, execution_day,
                            json.dumps(frozen_targets, ensure_ascii=False), dt.datetime.now().isoformat()))
                        diag["cycle_started"] = True
                    else:
                        diag["cycle_preview"] = True
            else:
                permitted = {}
                diag["reason"] = "waiting_for_scheduled_cycle"

        for sym in list(permitted):
            if sym in bought_today:
                diag["blocked_symbols"][sym] = "cycle_target_already_bought_today"
                permitted.pop(sym)
            elif sym in sold_dates and _sessions_between(sold_dates[sym], execution_day) <= cooldown_days:
                diag["blocked_symbols"][sym] = "sell_cooldown"
                permitted.pop(sym)
        diag["allowed_new_symbols"] = sorted(permitted)
        targets = dict(held)
        targets.update(permitted)
        total = round(sum(tp.target_weight for tp in targets.values()), 4)
        return replace(target_portfolio, target_positions=targets, total_weight=total,
                       cash_weight=round(max(0.0, 1.0 - total), 4),
                       reasons=list(target_portfolio.reasons) + ["entry_cycle:" + diag["reason"]]), diag
