# -*- coding: utf-8 -*-
"""
A-Stock Account Ledger
Maintains independent, segregated peer ledgers for Balanced and Aggressive accounts.
Supports double-entry bookkeeping, daily interest accounting, T+1 tracking, and atomic persistence.
"""

from typing import Dict, List, Optional, Any
from pathlib import Path
import json
import datetime
import uuid
from ..models import Position, AccountBalance, TradeFill, OrderSide
from .journal import CashJournalEntry, PositionJournalEntry
from ..errors import LedgerError


class AccountLedger:
    """Independent single-account quantitative ledger."""

    DAILY_INTEREST_RATE = 0.015 / 365.0  # 1.5% annual rate on idle cash

    def __init__(self, account_id: str, initial_cash: float = 100000.0, storage_path: Optional[Path] = None):
        self.account_id = account_id
        self.initial_capital = initial_cash
        self.storage_path = storage_path

        # State
        self.cash_cents = int(round(initial_cash * 100))
        self.frozen_cash_cents = 0
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.cash_journal: List[CashJournalEntry] = []
        self.position_journal: List[PositionJournalEntry] = []

        # Peak & Drawdown
        self.peak_equity = initial_cash
        self.max_drawdown_pct = 0.0
        self.current_drawdown_pct = 0.0
        self.total_interest_earned = 0.0

        # Initial journal entry
        self._record_cash(self.cash_cents, "INITIAL_DEPOSIT")

    @property
    def cash(self) -> float:
        return round(self.cash_cents / 100.0, 2)

    @property
    def frozen_cash(self) -> float:
        return round(self.frozen_cash_cents / 100.0, 2)

    @property
    def stock_market_value(self) -> float:
        return round(sum(p.market_value for p in self.positions.values()), 2)

    @property
    def total_equity(self) -> float:
        return round(self.cash + self.frozen_cash + self.stock_market_value, 2)

    def _record_cash(self, delta_cents: int, reason: str, ref_id: Optional[str] = None):
        entry = CashJournalEntry(
            entry_id=f"CJ_{uuid.uuid4().hex[:12]}",
            account_id=self.account_id,
            delta_cents=delta_cents,
            balance_cents=self.cash_cents,
            reason=reason,
            ref_id=ref_id,
            created_at=datetime.datetime.now().isoformat()
        )
        self.cash_journal.append(entry)

    def record_fill(self, fill: TradeFill):
        """Updates ledger cash, positions, and closed trade statistics from a TradeFill."""
        amount_cents = int(round(fill.amount * 100))
        fee_cents = int(round(fill.total_fee * 100))

        if fill.side == OrderSide.BUY:
            total_cost_cents = amount_cents + fee_cents
            if self.cash_cents < total_cost_cents:
                raise LedgerError(
                    f"Account {self.account_id} cash {self.cash:.2f} insufficient for fill cost {total_cost_cents/100:.2f}"
                )
            self.cash_cents -= total_cost_cents
            self._record_cash(-total_cost_cents, f"BUY_{fill.symbol}", ref_id=fill.fill_id)

            pos = self.positions.get(fill.symbol)
            if pos:
                tot_s = pos.shares + fill.shares
                new_cost = round(((pos.shares * pos.cost_price) + fill.amount + fill.total_fee) / tot_s, 3)
                pos.shares = tot_s
                pos.cost_price = new_cost
                pos.current_price = fill.price
                pos.market_value = round(tot_s * fill.price, 2)
            else:
                cost_p = round((fill.amount + fill.total_fee) / fill.shares, 3)
                pos = Position(
                    symbol=fill.symbol,
                    name=fill.name,
                    shares=fill.shares,
                    available_shares=0,  # T+1 tradable next day
                    cost_price=cost_p,
                    current_price=fill.price,
                    market_value=fill.amount,
                    unrealized_pnl=0.0,
                    unrealized_pnl_pct=0.0,
                    highest_price=fill.price,
                    entry_date=fill.timestamp[:10],
                    entry_regime="",
                    entry_reason="FILLED"
                )
                self.positions[fill.symbol] = pos

        else: # SELL
            pos = self.positions.get(fill.symbol)
            if not pos or pos.shares < fill.shares:
                raise LedgerError(
                    f"Account {self.account_id} shares insufficient for {fill.symbol}: {pos.shares if pos else 0} < {fill.shares}"
                )

            net_proceeds_cents = amount_cents - fee_cents
            self.cash_cents += net_proceeds_cents
            self._record_cash(net_proceeds_cents, f"SELL_{fill.symbol}", ref_id=fill.fill_id)

            # Record closed trade
            cost_basis = round(pos.cost_price * fill.shares, 2)
            pnl = round(fill.amount - fill.total_fee - cost_basis, 2)
            pnl_pct = round(pnl / cost_basis * 100.0, 2) if cost_basis > 0 else 0.0

            trade_record = {
                "trade_id": fill.fill_id,
                "order_id": fill.order_id,
                "date": fill.timestamp[:10],
                "symbol": fill.symbol,
                "name": fill.name,
                "side": "SELL",
                "shares": fill.shares,
                "price": fill.price,
                "amount": fill.amount,
                "fees": fill.total_fee,
                "slippage": fill.slippage,
                "realized_pnl": pnl,
                "pnl_pct": pnl_pct,
                "holding_days": pos.holding_days,
                "reason": "CLOSED"
            }
            self.closed_trades.append(trade_record)

            pos.shares -= fill.shares
            pos.available_shares = max(0, pos.available_shares - fill.shares)
            if pos.shares <= 0:
                del self.positions[fill.symbol]
            else:
                pos.market_value = round(pos.shares * fill.price, 2)

        self.update_nav()

    def update_prices(self, price_map: Dict[str, float]):
        """Updates current price and market value for all positions."""
        for sym, pos in self.positions.items():
            if sym in price_map:
                pos.update_price(price_map[sym])
        self.update_nav()

    def update_nav(self):
        eq = self.total_equity
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity > 0:
            dd = (self.peak_equity - eq) / self.peak_equity * 100.0
            self.current_drawdown_pct = round(max(0.0, dd), 2)
            self.max_drawdown_pct = max(self.max_drawdown_pct, self.current_drawdown_pct)

    def apply_overnight_interest(self, date_str: str) -> float:
        """Applies daily overnight idle cash interest (1.5% annual)."""
        interest = round(self.cash * self.DAILY_INTEREST_RATE, 2)
        if interest > 0:
            interest_cents = int(round(interest * 100))
            self.cash_cents += interest_cents
            self.total_interest_earned += interest
            self._record_cash(interest_cents, f"INTEREST_{date_str}")
            self.update_nav()
        return interest

    def roll_trading_day(self):
        """Unlocks all positions for T+1 tradability."""
        for pos in self.positions.values():
            pos.available_shares = pos.shares
            pos.holding_days += 1

    def get_summary(self) -> Dict[str, Any]:
        """Generates comprehensive account performance summary."""
        eq = self.total_equity
        total_pnl = round(eq - self.initial_capital, 2)
        total_pnl_pct = round(total_pnl / self.initial_capital * 100.0, 2) if self.initial_capital > 0 else 0.0

        wins = [t for t in self.closed_trades if t["realized_pnl"] > 0]
        losses = [t for t in self.closed_trades if t["realized_pnl"] <= 0]
        total_trades = len(self.closed_trades)
        win_rate = round(len(wins) / total_trades * 100.0, 2) if total_trades > 0 else 0.0

        total_gain = sum(t["realized_pnl"] for t in wins)
        total_loss = abs(sum(t["realized_pnl"] for t in losses))
        pl_ratio = round(total_gain / total_loss, 2) if total_loss > 0 else (99.0 if total_gain > 0 else 1.0)

        total_fees = round(sum(t.get("fees", 0.0) for t in self.closed_trades), 2)

        return {
            "account_id": self.account_id,
            "total_equity": eq,
            "cash": self.cash,
            "stock_market_value": self.stock_market_value,
            "initial_capital": self.initial_capital,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "peak_equity": round(self.peak_equity, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "current_drawdown_pct": round(self.current_drawdown_pct, 2),
            "positions_count": len(self.positions),
            "total_trades": total_trades,
            "win_rate_pct": win_rate,
            "profit_loss_ratio": pl_ratio,
            "total_fees": total_fees,
            "total_interest_earned": round(self.total_interest_earned, 2),
            "positions": {sym: p.to_dict() for sym, p in self.positions.items()}
        }

    def save(self, file_path: Optional[Path] = None):
        """Atomic write to JSON file."""
        target = file_path or self.storage_path
        if not target:
            return
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        doc = {
            "account_id": self.account_id,
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "cash_cents": self.cash_cents,
            "peak_equity": self.peak_equity,
            "max_drawdown_pct": self.max_drawdown_pct,
            "current_drawdown_pct": self.current_drawdown_pct,
            "total_interest_earned": self.total_interest_earned,
            "positions": {sym: p.to_dict() for sym, p in self.positions.items()},
            "closed_trades": self.closed_trades,
            "updated_at": datetime.datetime.now().isoformat()
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        tmp.replace(target)

    def load(self, file_path: Optional[Path] = None):
        """Loads state from JSON file."""
        target = file_path or self.storage_path
        if not target or not Path(target).exists():
            return
        with open(target, "r", encoding="utf-8") as f:
            d = json.load(f)
        self.account_id = d.get("account_id", self.account_id)
        self.initial_capital = float(d.get("initial_capital", self.initial_capital))
        self.cash_cents = int(d.get("cash_cents", round(float(d.get("cash", 100000.0)) * 100)))
        self.peak_equity = float(d.get("peak_equity", self.cash))
        self.max_drawdown_pct = float(d.get("max_drawdown_pct", 0.0))
        self.current_drawdown_pct = float(d.get("current_drawdown_pct", 0.0))
        self.total_interest_earned = float(d.get("total_interest_earned", 0.0))
        self.closed_trades = d.get("closed_trades", [])

        self.positions = {}
        for sym, pdata in d.get("positions", {}).items():
            pos = Position(
                symbol=sym,
                name=pdata.get("name", sym),
                shares=int(pdata.get("shares", pdata.get("total_shares", 0))),
                available_shares=int(pdata.get("available_shares", pdata.get("shares", 0))),
                cost_price=float(pdata.get("cost_price", 0.0)),
                current_price=float(pdata.get("current_price", pdata.get("price", 0.0))),
                market_value=float(pdata.get("market_value", 0.0)),
                unrealized_pnl=float(pdata.get("unrealized_pnl", 0.0)),
                unrealized_pnl_pct=float(pdata.get("unrealized_pnl_pct", 0.0)),
                highest_price=float(pdata.get("highest_price", 0.0)),
                entry_date=pdata.get("entry_date", ""),
                entry_regime=pdata.get("entry_regime", ""),
                entry_reason=pdata.get("entry_reason", ""),
                holding_days=int(pdata.get("holding_days", 0))
            )
            self.positions[sym] = pos
        self.update_nav()
