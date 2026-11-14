# -*- coding: utf-8 -*-
"""
A-Stock SQLite Transactional Ledger
Guarantees ACID transactions, idempotency on fills, T+1 lot tracking,
double-entry cash journaling, and historical reconstruction.
"""

import sqlite3
import json
import datetime
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple, Any

from ..models import (
    Order, TradeFill, Position, AccountBalance, OrderStatus, OrderSide,
    RiskAuditResult
)
from ..errors import InsufficientCashError, InsufficientSharesError, DuplicateOrderError


def to_cents(amount: float) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> float:
    return float(Decimal(cents) / 100)


class SQLiteAccountLedger:
    """Production-grade transactional account ledger powered by SQLite."""

    def __init__(self, db_path: str = "data/trading_v3.sqlite"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    account_type TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    total_cash_cents INTEGER NOT NULL,
                    available_cash_cents INTEGER NOT NULL,
                    frozen_cash_cents INTEGER NOT NULL DEFAULT 0,
                    peak_equity_cents INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    client_order_id TEXT UNIQUE,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    filled_shares INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_orders_acc_status ON orders(account_id, status);

                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    fill_price REAL NOT NULL,
                    fill_shares INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    fees_cents INTEGER NOT NULL,
                    slippage_cents INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (order_id) REFERENCES orders(order_id),
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fills_acc_date ON fills(account_id, trade_date);

                CREATE TABLE IF NOT EXISTS positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    sector TEXT,
                    total_shares INTEGER NOT NULL,
                    tradable_shares INTEGER NOT NULL,
                    frozen_shares INTEGER NOT NULL DEFAULT 0,
                    avg_cost REAL NOT NULL,
                    last_price REAL NOT NULL,
                    market_value_cents INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, symbol),
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                );

                CREATE TABLE IF NOT EXISTS position_lots (
                    lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    buy_date TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    remaining_shares INTEGER NOT NULL,
                    cost_price REAL NOT NULL,
                    is_tradable_t1 INTEGER NOT NULL DEFAULT 0,
                    fill_id TEXT,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_lots_acc_sym ON position_lots(account_id, symbol, is_tradable_t1);

                CREATE TABLE IF NOT EXISTS cash_journal (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    delta_cents INTEGER NOT NULL,
                    balance_cents INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    ref_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_journal_acc_date ON cash_journal(account_id, date);

                CREATE TABLE IF NOT EXISTS risk_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_id TEXT UNIQUE,
                    account_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    action_taken TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT
                );

                CREATE TABLE IF NOT EXISTS strategy_runs (
                    run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    target_portfolio_json TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    def init_account(
        self,
        account_id: str,
        account_type: str = "balanced",
        initial_cash: float = 100000.0
    ) -> bool:
        """Initializes account if it does not exist."""
        now_iso = datetime.datetime.now().isoformat()
        cash_cents = to_cents(initial_cash)
        with self.connect() as conn:
            row = conn.execute("SELECT account_id FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
            if not row:
                conn.execute(
                    """INSERT INTO accounts
                       (account_id, account_type, currency, total_cash_cents, available_cash_cents, frozen_cash_cents, peak_equity_cents, updated_at)
                       VALUES (?, ?, 'CNY', ?, ?, 0, ?, ?)""",
                    (account_id, account_type, cash_cents, cash_cents, cash_cents, now_iso)
                )
                conn.execute(
                    """INSERT INTO cash_journal
                       (account_id, date, delta_cents, balance_cents, reason, ref_id, created_at)
                       VALUES (?, ?, ?, ?, 'INITIAL_CAPITAL', 'INIT', ?)""",
                    (account_id, now_iso[:10], cash_cents, cash_cents, now_iso)
                )
                return True
        return False

    def get_balance(self, account_id: str) -> AccountBalance:
        """Loads live account balance and computes equity."""
        with self.connect() as conn:
            acc = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
            if not acc:
                raise ValueError(f"Account {account_id} not found in database")

            pos_rows = conn.execute(
                "SELECT SUM(market_value_cents) as total_mv FROM positions WHERE account_id = ?",
                (account_id,)
            ).fetchone()
            stock_val = from_cents(pos_rows["total_mv"] or 0)
            cash = from_cents(acc["total_cash_cents"])
            avail = from_cents(acc["available_cash_cents"])
            frozen = from_cents(acc["frozen_cash_cents"])

            tot_equity = cash + frozen + stock_val
            peak_equity = max(from_cents(acc["peak_equity_cents"]), tot_equity)
            dd_pct = (peak_equity - tot_equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0

            # Update peak if grown
            if (cash + stock_val) > from_cents(acc["peak_equity_cents"]):
                conn.execute(
                    "UPDATE accounts SET peak_equity_cents = ?, updated_at = ? WHERE account_id = ?",
                    (to_cents(cash + stock_val), datetime.datetime.now().isoformat(), account_id)
                )

            return AccountBalance(
                account_id=account_id,
                cash=cash,
                frozen_cash=frozen,
                stock_market_value=stock_val,
                total_equity=tot_equity,
                initial_capital=100000.0,
                peak_equity=peak_equity,
                max_drawdown_pct=max(0.0, dd_pct),
                current_drawdown_pct=max(0.0, dd_pct),
                is_frozen=(dd_pct >= 12.0),
                updated_at=acc["updated_at"]
            )

    def get_positions(self, account_id: str) -> Dict[str, Position]:
        """Loads all open positions for account."""
        positions = {}
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM positions WHERE account_id = ?", (account_id,)).fetchall()
            for r in rows:
                pos = Position(
                    symbol=r["symbol"],
                    shares=r["total_shares"],
                    available_shares=r["tradable_shares"],
                    cost_price=r["avg_cost"],
                    name=r["name"] or r["symbol"],
                    current_price=r["last_price"],
                    market_value=from_cents(r["market_value_cents"]),
                    sector=r["sector"] or "其他"
                )
                positions[r["symbol"]] = pos
        return positions

    def update_market_prices(self, quotes: Dict[str, Dict[str, Any]]):
        """Updates last_price and market_value for all open positions across all accounts."""
        now_iso = datetime.datetime.now().isoformat()
        with self.connect() as conn:
            rows = conn.execute("SELECT account_id, symbol, total_shares FROM positions").fetchall()
            for r in rows:
                sym = r["symbol"]
                q = quotes.get(sym)
                if q:
                    price = float(q.get("price") or q.get("current") or q.get("close") or 0.0)
                    if price > 0:
                        mv_cents = to_cents(price * r["total_shares"])
                        conn.execute(
                            """UPDATE positions SET last_price = ?, market_value_cents = ?, updated_at = ?
                               WHERE account_id = ? AND symbol = ?""",
                            (price, mv_cents, now_iso, r["account_id"], sym)
                        )

    def save_order(self, order: Order):
        """Inserts or updates an order in database."""
        now_iso = datetime.datetime.now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO orders
                   (order_id, client_order_id, account_id, symbol, side, order_type, price, shares, filled_shares, status, reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET
                   filled_shares = excluded.filled_shares,
                   status = excluded.status,
                   reason = excluded.reason,
                   updated_at = excluded.updated_at""",
                (
                    order.order_id, order.client_order_id, order.account_id, order.symbol,
                    order.side.value, order.order_type.value, order.price, order.shares,
                    order.filled_shares, order.status.value, order.reason,
                    order.created_at or now_iso, now_iso
                )
            )

    def apply_fill(self, fill: TradeFill, name: str = "", sector: str = "其他") -> bool:
        """
        Applies a trade fill transactionally:
        - Fill idempotency (duplicate fill_id -> ignored)
        - Cash deduction/credit and cash_journal entry
        - Position update and FIFO position lot tracking
        - Order filled_shares & status sync
        """
        now_iso = datetime.datetime.now().isoformat()
        fill_price = getattr(fill, "price", getattr(fill, "fill_price", 0.0))
        fill_shares = getattr(fill, "shares", getattr(fill, "fill_shares", 0))
        fees = getattr(fill, "total_fee", getattr(fill, "fees", 0.0))
        slippage = getattr(fill, "slippage", 0.0)
        trade_date = getattr(fill, "trade_date", str(getattr(fill, "timestamp", ""))[:10] or now_iso[:10])
        fill_name = getattr(fill, "name", name) or name

        amount_cents = to_cents(fill.amount if hasattr(fill, "amount") and fill.amount > 0 else fill_price * fill_shares)
        fees_cents = to_cents(fees)
        total_debit_cents = amount_cents + fees_cents if fill.side == OrderSide.BUY else -(amount_cents - fees_cents)

        with self.connect() as conn:
            # 1. Idempotency Check
            existing = conn.execute("SELECT fill_id FROM fills WHERE fill_id = ?", (fill.fill_id,)).fetchone()
            if existing:
                return False  # Already applied

            # 2. Check Account Cash / Shares sufficiency
            acc = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (fill.account_id,)).fetchone()
            if not acc:
                raise ValueError(f"Account {fill.account_id} not found")

            if fill.side == OrderSide.BUY:
                if acc["available_cash_cents"] < total_debit_cents:
                    raise InsufficientCashError(
                        f"Insufficient cash for account {fill.account_id}: need {from_cents(total_debit_cents):.2f}, available {from_cents(acc['available_cash_cents']):.2f}"
                    )
            elif fill.side == OrderSide.SELL:
                pos = conn.execute(
                    "SELECT tradable_shares, total_shares FROM positions WHERE account_id = ? AND symbol = ?",
                    (fill.account_id, fill.symbol)
                ).fetchone()
                if not pos or pos["tradable_shares"] < fill_shares:
                    raise InsufficientSharesError(
                        f"Insufficient tradable shares for {fill.symbol}: need {fill_shares}, tradable {pos['tradable_shares'] if pos else 0}"
                    )

            # 3. Ensure Order exists for FK and Insert Fill
            ord_exists = conn.execute("SELECT order_id FROM orders WHERE order_id = ?", (fill.order_id,)).fetchone()
            if not ord_exists:
                conn.execute(
                    """INSERT INTO orders
                       (order_id, client_order_id, account_id, symbol, side, order_type, price, shares, filled_shares, status, reason, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'LIMIT', ?, ?, 0, 'SUBMITTED', 'AutoCreatedForFill', ?, ?)""",
                    (fill.order_id, getattr(fill, "client_order_id", fill.order_id) or fill.order_id, fill.account_id, fill.symbol, fill.side.value, fill_price, fill_shares, now_iso, now_iso)
                )

            conn.execute(
                """INSERT INTO fills
                   (fill_id, order_id, account_id, symbol, side, fill_price, fill_shares, amount_cents, fees_cents, slippage_cents, trade_date, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill.fill_id, fill.order_id, fill.account_id, fill.symbol,
                    fill.side.value, fill_price, fill_shares,
                    amount_cents, fees_cents, to_cents(slippage),
                    trade_date, now_iso
                )
            )

            # 4. Update Cash & Cash Journal
            new_cash_cents = acc["total_cash_cents"] - total_debit_cents
            new_avail_cents = acc["available_cash_cents"] - total_debit_cents
            conn.execute(
                """UPDATE accounts
                   SET total_cash_cents = ?, available_cash_cents = ?, updated_at = ?
                   WHERE account_id = ?""",
                (new_cash_cents, new_avail_cents, now_iso, fill.account_id)
            )
            conn.execute(
                """INSERT INTO cash_journal
                   (account_id, date, delta_cents, balance_cents, reason, ref_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill.account_id, trade_date, -total_debit_cents, new_cash_cents,
                    f"TRADE_{fill.side.value}_{fill.symbol}", fill.fill_id, now_iso
                )
            )

            # 5. Update Positions & Lots
            if fill.side == OrderSide.BUY:
                pos = conn.execute(
                    "SELECT * FROM positions WHERE account_id = ? AND symbol = ?",
                    (fill.account_id, fill.symbol)
                ).fetchone()

                lot_cost = fill_price + (fees / fill_shares if fill_shares > 0 else 0.0)
                # T+1 rule: lot is untradable on the buy date (is_tradable_t1 = 0)
                conn.execute(
                    """INSERT INTO position_lots
                       (account_id, symbol, buy_date, shares, remaining_shares, cost_price, is_tradable_t1, fill_id)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                    (fill.account_id, fill.symbol, trade_date, fill_shares, fill_shares, lot_cost, fill.fill_id)
                )

                if pos:
                    new_shares = pos["total_shares"] + fill_shares
                    new_avg = ((pos["avg_cost"] * pos["total_shares"]) + (lot_cost * fill_shares)) / new_shares
                    new_mv = to_cents(fill_price * new_shares)
                    conn.execute(
                        """UPDATE positions
                           SET total_shares = ?, avg_cost = ?, last_price = ?, market_value_cents = ?, updated_at = ?
                           WHERE account_id = ? AND symbol = ?""",
                        (new_shares, new_avg, fill_price, new_mv, now_iso, fill.account_id, fill.symbol)
                    )
                else:
                    new_mv = to_cents(fill_price * fill_shares)
                    conn.execute(
                        """INSERT INTO positions
                           (account_id, symbol, name, sector, total_shares, tradable_shares, frozen_shares, avg_cost, last_price, market_value_cents, updated_at)
                           VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)""",
                        (fill.account_id, fill.symbol, fill_name or fill.symbol, sector, fill_shares, lot_cost, fill_price, new_mv, now_iso)
                    )

            elif fill.side == OrderSide.SELL:
                # Deduct FIFO from position_lots
                lots = conn.execute(
                    """SELECT lot_id, remaining_shares FROM position_lots
                       WHERE account_id = ? AND symbol = ? AND is_tradable_t1 = 1 AND remaining_shares > 0
                       ORDER BY buy_date ASC, lot_id ASC""",
                    (fill.account_id, fill.symbol)
                ).fetchall()

                needed = fill_shares
                for lot in lots:
                    take = min(needed, lot["remaining_shares"])
                    conn.execute(
                        "UPDATE position_lots SET remaining_shares = remaining_shares - ? WHERE lot_id = ?",
                        (take, lot["lot_id"])
                    )
                    needed -= take
                    if needed <= 0:
                        break

                pos = conn.execute(
                    "SELECT * FROM positions WHERE account_id = ? AND symbol = ?",
                    (fill.account_id, fill.symbol)
                ).fetchone()
                rem_total = pos["total_shares"] - fill_shares
                rem_tradable = pos["tradable_shares"] - fill_shares

                if rem_total <= 0:
                    conn.execute(
                        "DELETE FROM positions WHERE account_id = ? AND symbol = ?",
                        (fill.account_id, fill.symbol)
                    )
                else:
                    new_mv = to_cents(fill_price * rem_total)
                    conn.execute(
                        """UPDATE positions
                           SET total_shares = ?, tradable_shares = ?, last_price = ?, market_value_cents = ?, updated_at = ?
                           WHERE account_id = ? AND symbol = ?""",
                        (rem_total, rem_tradable, fill_price, new_mv, now_iso, fill.account_id, fill.symbol)
                    )

            # 6. Update Order Status
            ord_row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (fill.order_id,)).fetchone()
            if ord_row:
                new_filled = ord_row["filled_shares"] + fill_shares
                new_status = OrderStatus.FILLED.value if new_filled >= ord_row["shares"] else OrderStatus.PARTIALLY_FILLED.value
                conn.execute(
                    "UPDATE orders SET filled_shares = ?, status = ?, updated_at = ? WHERE order_id = ?",
                    (new_filled, new_status, now_iso, fill.order_id)
                )

            return True

    def roll_t1(self, trade_date: str, account_id: Optional[str] = None):
        """
        Rolls T+1 locks: Any lots bought before trade_date become tradable.
        Re-synchronizes tradable_shares in positions.
        """
        with self.connect() as conn:
            # Unlock lots bought strictly before trade_date
            if account_id:
                conn.execute(
                    """UPDATE position_lots
                       SET is_tradable_t1 = 1
                       WHERE account_id = ? AND buy_date < ? AND is_tradable_t1 = 0""",
                    (account_id, trade_date)
                )
            else:
                conn.execute(
                    """UPDATE position_lots
                       SET is_tradable_t1 = 1
                       WHERE buy_date < ? AND is_tradable_t1 = 0""",
                    (trade_date,)
                )

            # Re-sum tradable shares per position
            positions = conn.execute("SELECT account_id, symbol FROM positions").fetchall()
            for p in positions:
                tot_tradable = conn.execute(
                    """SELECT SUM(remaining_shares) as s FROM position_lots
                       WHERE account_id = ? AND symbol = ? AND is_tradable_t1 = 1""",
                    (p["account_id"], p["symbol"])
                ).fetchone()["s"] or 0
                conn.execute(
                    "UPDATE positions SET tradable_shares = ? WHERE account_id = ? AND symbol = ?",
                    (tot_tradable, p["account_id"], p["symbol"])
                )

    def accrue_cash_interest(self, account_id: str, date: str, annual_rate: float = 0.015):
        """Credits overnight cash interest on idle cash balances."""
        with self.connect() as conn:
            acc = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
            if not acc:
                return
            cash_cents = acc["total_cash_cents"]
            if cash_cents <= 0:
                return

            interest_cents = int((Decimal(cash_cents) * Decimal(str(annual_rate)) / Decimal(365)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            if interest_cents > 0:
                now_iso = datetime.datetime.now().isoformat()
                new_cash_cents = cash_cents + interest_cents
                conn.execute(
                    """UPDATE accounts
                       SET total_cash_cents = ?, available_cash_cents = available_cash_cents + ?, updated_at = ?
                       WHERE account_id = ?""",
                    (new_cash_cents, interest_cents, now_iso, account_id)
                )
                conn.execute(
                    """INSERT INTO cash_journal
                       (account_id, date, delta_cents, balance_cents, reason, ref_id, created_at)
                       VALUES (?, ?, ?, ?, 'OVERNIGHT_CASH_INTEREST', 'INTEREST', ?)""",
                    (account_id, date, interest_cents, new_cash_cents, now_iso)
                )

    def record_risk_event(self, audit: RiskAuditResult):
        """Records explainable risk evaluation event."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO risk_events
                   (audit_id, account_id, timestamp, layer, rule_name, severity, action_taken, message, details_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(audit_id) DO NOTHING""",
                (
                    audit.audit_id, audit.account_id, audit.timestamp, audit.layer.value,
                    audit.rule_name, audit.severity.value, audit.action_taken,
                    audit.message, json.dumps(audit.details or {}, ensure_ascii=False)
                )
            )

    def record_strategy_run(
        self,
        run_id: str,
        account_id: str,
        strategy_name: str,
        as_of_date: str,
        regime: str,
        target_portfolio: Dict[str, Any],
        status: str = "COMPLETED"
    ):
        """Records strategy execution snapshot."""
        now_iso = datetime.datetime.now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO strategy_runs
                   (run_id, account_id, strategy_name, as_of_date, regime, target_portfolio_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                   status = excluded.status,
                   target_portfolio_json = excluded.target_portfolio_json""",
                (
                    run_id, account_id, strategy_name, as_of_date, regime,
                    json.dumps(target_portfolio, ensure_ascii=False), status, now_iso
                )
            )

    def verify_integrity(self, account_id: str) -> Dict[str, Any]:
        """
        Reconstructs cash and shares from journals and lot histories.
        Validates accounting invariants:
        - Current cash == Sum(cash_journal deltas)
        - Current position shares == Sum(remaining_shares of position_lots)
        """
        with self.connect() as conn:
            acc = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
            if not acc:
                return {"valid": False, "error": f"Account {account_id} not found"}

            journal_cash_cents = conn.execute(
                "SELECT SUM(delta_cents) as total FROM cash_journal WHERE account_id = ?",
                (account_id,)
            ).fetchone()["total"] or 0

            cash_discrepancy = acc["total_cash_cents"] - journal_cash_cents

            # Check positions vs lots
            pos_rows = conn.execute("SELECT symbol, total_shares FROM positions WHERE account_id = ?", (account_id,)).fetchall()
            lot_discrepancies = []
            for p in pos_rows:
                sym = p["symbol"]
                lot_shares = conn.execute(
                    "SELECT SUM(remaining_shares) as s FROM position_lots WHERE account_id = ? AND symbol = ?",
                    (account_id, sym)
                ).fetchone()["s"] or 0
                if p["total_shares"] != lot_shares:
                    lot_discrepancies.append({
                        "symbol": sym,
                        "position_shares": p["total_shares"],
                        "lot_shares": lot_shares
                    })

            valid = (cash_discrepancy == 0 and len(lot_discrepancies) == 0)
            return {
                "valid": valid,
                "account_id": account_id,
                "recorded_cash": from_cents(acc["total_cash_cents"]),
                "journal_cash": from_cents(journal_cash_cents),
                "cash_discrepancy": from_cents(cash_discrepancy),
                "lot_discrepancies": lot_discrepancies
            }

    def get_summary(self, account_id: str) -> Dict[str, Any]:
        """Web API / UI compatible summary."""
        bal = self.get_balance(account_id)
        positions = self.get_positions(account_id)
        with self.connect() as conn:
            trades_count = conn.execute("SELECT COUNT(*) as c FROM fills WHERE account_id = ?", (account_id,)).fetchone()["c"]
            init_row = conn.execute(
                "SELECT delta_cents FROM cash_journal WHERE account_id = ? AND reason = 'INITIAL_CAPITAL'",
                (account_id,)
            ).fetchone()
            init_cash = from_cents(init_row["delta_cents"] if init_row else to_cents(100000.0))

        pnl = bal.total_equity - init_cash
        pnl_pct = (pnl / init_cash * 100.0) if init_cash > 0 else 0.0

        return {
            "account_id": account_id,
            "total_equity": round(bal.total_equity, 2),
            "cash": round(bal.total_cash, 2),
            "available_cash": round(bal.available_cash, 2),
            "stock_value": round(bal.stock_value, 2),
            "peak_equity": round(bal.peak_equity, 2),
            "drawdown_pct": round(bal.current_drawdown_pct, 2),
            "total_profit_loss": round(pnl, 2),
            "profit_loss_pct": round(pnl_pct, 2),
            "positions_count": len(positions),
            "total_trades": trades_count
        }

    def get_trades(self, account_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Returns trade list for API."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT f.*, o.reason as order_reason
                   FROM fills f
                   LEFT JOIN orders o ON f.order_id = o.order_id
                   WHERE f.account_id = ?
                   ORDER BY f.created_at DESC LIMIT ?""",
                (account_id, limit)
            ).fetchall()

            trades = []
            for r in rows:
                trades.append({
                    "trade_id": r["fill_id"],
                    "order_id": r["order_id"],
                    "date": r["trade_date"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "price": r["fill_price"],
                    "shares": r["fill_shares"],
                    "amount": from_cents(r["amount_cents"]),
                    "fees": from_cents(r["fees_cents"]),
                    "slippage": from_cents(r["slippage_cents"]),
                    "reason": r["order_reason"] or "",
                    "created_at": r["created_at"]
                })
            return trades
