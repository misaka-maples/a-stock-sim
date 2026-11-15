# src/astock/trade/broker/sim_broker.py
# -*- coding: utf-8 -*-
"""
A-Stock Simulated Broker Adapter
Backed by SQLiteAccountLedger as the single authoritative source of truth.
Provides execution simulation with A-share market microstructure:
- T+1 settlement enforcement
- Commission: 0.025% (min 5.0 RMB)
- Stamp tax: 0.05% (SELL only)
- Transfer fee: 0.001%
- Slippage simulation
- Partial fill simulation
- Idempotent execution
"""

import logging
from typing import Dict, List, Optional, Any
import datetime
import uuid
from .base import BaseBrokerAdapter
from ..models import (
    Order, OrderStatus, OrderSide, OrderType, Position, TradeFill, AccountBalance
)
from ..errors import (
    InsufficientCashError, InsufficientSharesError, BrokerError
)
from ..order.state_machine import OrderStateMachine

logger = logging.getLogger(__name__)


class SimulatedBrokerAdapter(BaseBrokerAdapter):
    """
    Production-grade Simulated Broker Adapter backed by SQLiteAccountLedger.
    When ledger is provided:
    - Broker is stateless with respect to account balances and positions.
    - Cash, positions, and tradable shares are queried directly from SQLite.
    - execute_order simulates execution microstructure (fees, slippage, partial fills),
      generates TradeFill, and commits to SQLite ledger transactionally.
    - If SQLite commit fails, no broker in-memory state is retained.
    When ledger is None:
    - Falls back to in-memory dictionaries for standalone unit tests.
    """

    COMMISSION_RATE = 0.00025      # 0.025% (万2.5)
    MIN_COMMISSION = 5.0           # 最低 5 元
    STAMP_TAX_RATE = 0.0005        # 0.05% (千0.5, 仅卖出收取)
    TRANSFER_FEE_RATE = 0.00001    # 0.001% (十万分之1)
    DEFAULT_SLIPPAGE = 0.001       # 0.1% 滑点

    def __init__(
        self,
        ledger: Optional[Any] = None,
        initial_cash: float = 100000.0,
        slippage: float = 0.001
    ):
        self.ledger = ledger
        self._slippage = slippage
        self._balances: Dict[str, AccountBalance] = {}       # account_id -> AccountBalance (fallback only)
        self._positions: Dict[str, Dict[str, Position]] = {} # account_id -> {symbol: Position} (fallback only)
        self._orders: Dict[str, Dict[str, Order]] = {}       # account_id -> {order_id: Order}
        self._fills: Dict[str, List[TradeFill]] = {}         # account_id -> List[TradeFill]
        self._default_initial_cash = initial_cash

    def init_account(self, account_id: str, cash: Optional[float] = None) -> AccountBalance:
        if self.ledger:
            # Check if account already exists in SQLite
            try:
                return self.ledger.get_balance(account_id)
            except Exception:
                c = cash if cash is not None else self._default_initial_cash
                self.ledger.init_account(account_id, account_id, initial_cash=c)
                return self.ledger.get_balance(account_id)

        # Fallback for standalone mock without ledger
        c = cash if cash is not None else self._default_initial_cash
        now_iso = datetime.datetime.now().isoformat()
        bal = AccountBalance(
            account_id=account_id,
            cash=round(c, 2),
            frozen_cash=0.0,
            stock_market_value=0.0,
            total_equity=round(c, 2),
            initial_capital=round(c, 2),
            peak_equity=round(c, 2),
            max_drawdown_pct=0.0,
            current_drawdown_pct=0.0,
            is_frozen=False,
            updated_at=now_iso
        )
        self._balances[account_id] = bal
        self._positions[account_id] = {}
        self._orders[account_id] = {}
        self._fills[account_id] = []
        return bal

    def _ensure_account(self, account_id: str) -> AccountBalance:
        if self.ledger:
            return self.ledger.get_balance(account_id)
        if account_id not in self._balances:
            return self.init_account(account_id)
        return self._balances[account_id]

    def get_cash(self, account_id: str) -> float:
        if self.ledger:
            return self.ledger.get_balance(account_id).available_cash
        bal = self._ensure_account(account_id)
        return bal.cash

    def get_positions(self, account_id: str) -> Dict[str, Position]:
        if self.ledger:
            return self.ledger.get_positions(account_id)
        self._ensure_account(account_id)
        return self._positions.get(account_id, {})

    def get_sellable_shares(self, account_id: str, symbol: str) -> int:
        positions = self.get_positions(account_id)
        pos = positions.get(symbol)
        if not pos:
            return 0
        return getattr(pos, "tradable_shares", getattr(pos, "available_shares", 0))

    def get_open_orders(self, account_id: str) -> List[Order]:
        if self.ledger and hasattr(self.ledger, "get_active_orders"):
            return self.ledger.get_active_orders(account_id)
        self._ensure_account(account_id)
        return [o for o in self._orders.get(account_id, {}).values() if o.is_active]

    def get_order_status(self, account_id: str, order_id: str) -> OrderStatus:
        if self.ledger and hasattr(self.ledger, "get_order"):
            o = self.ledger.get_order(order_id)
            return o.status if o else OrderStatus.UNKNOWN
        self._ensure_account(account_id)
        order = self._orders.get(account_id, {}).get(order_id)
        return order.status if order else OrderStatus.UNKNOWN

    def get_fills(self, account_id: str, order_id: Optional[str] = None) -> List[TradeFill]:
        if self.ledger and hasattr(self.ledger, "get_trades"):
            trades = self.ledger.get_trades(account_id, limit=500)
            res = []
            for t in trades:
                if order_id and t["order_id"] != order_id:
                    continue
                res.append(TradeFill(
                    fill_id=t["trade_id"],
                    order_id=t["order_id"],
                    client_order_id=t.get("client_order_id", t["order_id"]),
                    account_id=account_id,
                    symbol=t["symbol"],
                    name=t.get("name", t["symbol"]),
                    side=OrderSide(t["side"]),
                    price=t["price"],
                    shares=t["shares"],
                    amount=t["amount"],
                    commission=t["fees"],
                    stamp_tax=0.0,
                    transfer_fee=0.0,
                    slippage=t.get("slippage", 0.0),
                    realized_pnl=0.0,
                    timestamp=t["date"]
                ))
            return res
        self._ensure_account(account_id)
        fills = self._fills.get(account_id, [])
        if order_id:
            fills = [f for f in fills if f.order_id == order_id]
        return fills

    def cancel_order(self, account_id: str, order_id: str) -> bool:
        if self.ledger and hasattr(self.ledger, "get_order"):
            order = self.ledger.get_order(order_id)
            if not order or not order.is_active:
                return False
            OrderStateMachine.transition(order, OrderStatus.CANCELLED)
            self.ledger.save_order(order)
            return True

        self._ensure_account(account_id)
        order = self._orders.get(account_id, {}).get(order_id)
        if not order or not order.is_active:
            return False
        OrderStateMachine.transition(order, OrderStatus.CANCELLED)
        order.updated_at = datetime.datetime.now().isoformat()
        return True

    def submit_order(self, order: Order) -> Order:
        """Submits order and executes immediately in simulation."""
        account_id = order.account_id
        if self.ledger:
            self.ledger.save_order(order)
        else:
            self._ensure_account(account_id)
            self._orders[account_id][order.order_id] = order
        OrderStateMachine.transition(order, OrderStatus.SUBMITTED)
        self.execute_order(order)
        return order

    def calculate_fees(self, side: OrderSide, amount: float) -> Dict[str, float]:
        """Calculates Chinese A-share trading fees and taxes."""
        commission = max(self.MIN_COMMISSION, round(amount * self.COMMISSION_RATE, 2))
        stamp_tax = round(amount * self.STAMP_TAX_RATE, 2) if side == OrderSide.SELL else 0.0
        transfer_fee = round(amount * self.TRANSFER_FEE_RATE, 2)
        return {
            "commission": commission,
            "stamp_tax": stamp_tax,
            "transfer_fee": transfer_fee,
            "total_fee": round(commission + stamp_tax + transfer_fee, 2)
        }

    def execute_order(
        self,
        order: Order,
        execution_price: Optional[float] = None,
        partial_ratio: float = 1.0,
        trade_date: Optional[str] = None,
        auto_apply_ledger: bool = True
    ) -> TradeFill:
        """
        Executes an order against simulated market liquidity.
        When backed by SQLiteAccountLedger, directly queries and applies to SQLite transaction.
        """
        account_id = order.account_id
        now_iso = datetime.datetime.now().isoformat()
        trade_time = trade_date or now_iso

        price = execution_price if execution_price is not None else order.price
        if price <= 0:
            raise BrokerError(f"Invalid execution price: {price}")

        # Apply slippage: BUY gets price*(1+slip), SELL gets price*(1-slip)
        raw_price = price
        if order.side == OrderSide.BUY:
            fill_price = round(raw_price * (1.0 + self._slippage), 2)
            slippage_cost = round((fill_price - raw_price) * order.shares * partial_ratio, 2)
        else:
            fill_price = round(raw_price * (1.0 - self._slippage), 2)
            slippage_cost = round((raw_price - fill_price) * order.shares * partial_ratio, 2)

        fill_shares = int(order.shares * partial_ratio)
        if fill_shares <= 0:
            raise BrokerError(f"Fill shares resulted in 0 (order shares: {order.shares})")

        amount = round(fill_price * fill_shares, 2)
        fees = self.calculate_fees(order.side, amount)
        total_fee = fees["total_fee"]
        realized_pnl = 0.0

        if self.ledger:
            # === Ledger-Backed Authoritative Execution ===
            bal = self.ledger.get_balance(account_id)
            positions = self.ledger.get_positions(account_id)

            if order.side == OrderSide.BUY:
                total_required = amount + total_fee
                if bal.available_cash < total_required - 1e-4:
                    raise InsufficientCashError(
                        f"Insufficient cash for account {account_id}: need {total_required:.2f}, available {bal.available_cash:.2f}"
                    )
            else: # SELL
                pos = positions.get(order.symbol)
                avail = getattr(pos, "tradable_shares", getattr(pos, "available_shares", 0)) if pos else 0
                if not pos or avail < fill_shares:
                    raise InsufficientSharesError(
                        f"Insufficient available shares for {order.symbol}: requested {fill_shares}, tradable {avail}"
                    )
                cost_basis = round(pos.cost_price * fill_shares, 2)
                realized_pnl = round(amount - total_fee - cost_basis, 2)

            # Record TradeFill
            fill = TradeFill(
                fill_id=f"FILL_{uuid.uuid4().hex[:12]}",
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                account_id=account_id,
                symbol=order.symbol,
                name=order.name or order.symbol,
                side=order.side,
                price=fill_price,
                shares=fill_shares,
                amount=amount,
                commission=fees["commission"],
                stamp_tax=fees["stamp_tax"],
                transfer_fee=fees["transfer_fee"],
                slippage=slippage_cost,
                realized_pnl=realized_pnl,
                timestamp=trade_time
            )

            # Commit to SQLite ledger
            if auto_apply_ledger:
                applied = self.ledger.apply_fill(fill, name=order.name, sector=getattr(order, "sector", "其他"))
                if not applied:
                    logger.warning(f"Fill {fill.fill_id} already exists or was ignored by ledger.")

            # Update Order Lifecycle Status
            order.filled_shares += fill_shares
            order.filled_amount += amount
            order.fees += total_fee
            order.slippage += slippage_cost
            order.updated_at = trade_time

            if order.filled_shares >= order.shares:
                OrderStateMachine.transition(order, OrderStatus.FILLED)
            else:
                OrderStateMachine.transition(order, OrderStatus.PARTIALLY_FILLED)

            self.ledger.save_order(order)
            return fill

        else:
            # === Standalone Fallback Execution (No SQLite ledger) ===
            bal = self._ensure_account(account_id)
            positions = self._positions[account_id]

            if order.side == OrderSide.BUY:
                total_required = amount + total_fee
                if bal.cash < total_required - 1e-4:
                    raise InsufficientCashError(
                        f"Insufficient cash for account {account_id}: need {total_required:.2f}, available {bal.cash:.2f}"
                    )

                bal.cash = round(bal.cash - total_required, 2)
                pos = positions.get(order.symbol)
                if pos:
                    total_s = pos.shares + fill_shares
                    new_cost = round(((pos.shares * pos.cost_price) + amount + total_fee) / total_s, 3)
                    pos.shares = total_s
                    pos.cost_price = new_cost
                    pos.current_price = fill_price
                    pos.market_value = round(total_s * fill_price, 2)
                else:
                    cost_p = round((amount + total_fee) / fill_shares, 3)
                    pos = Position(
                        symbol=order.symbol,
                        name=order.name or order.symbol,
                        shares=fill_shares,
                        available_shares=0,  # T+1 rule
                        cost_price=cost_p,
                        current_price=fill_price,
                        market_value=amount,
                        unrealized_pnl=0.0,
                        unrealized_pnl_pct=0.0,
                        highest_price=fill_price,
                        entry_date=trade_time[:10] if len(trade_time) >= 10 else "",
                        entry_regime="",
                        entry_reason=order.reason
                    )
                    positions[order.symbol] = pos

            else: # SELL
                pos = positions.get(order.symbol)
                if not pos or pos.available_shares < fill_shares:
                    avail = pos.available_shares if pos else 0
                    raise InsufficientSharesError(
                        f"Insufficient available shares for {order.symbol}: requested {fill_shares}, tradable {avail}"
                    )

                cost_basis = round(pos.cost_price * fill_shares, 2)
                realized_pnl = round(amount - total_fee - cost_basis, 2)
                bal.cash = round(bal.cash + amount - total_fee, 2)
                pos.shares -= fill_shares
                pos.available_shares -= fill_shares
                if pos.shares <= 0:
                    del positions[order.symbol]
                else:
                    pos.market_value = round(pos.shares * fill_price, 2)

            fill = TradeFill(
                fill_id=f"FILL_{uuid.uuid4().hex[:12]}",
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                account_id=account_id,
                symbol=order.symbol,
                name=order.name or order.symbol,
                side=order.side,
                price=fill_price,
                shares=fill_shares,
                amount=amount,
                commission=fees["commission"],
                stamp_tax=fees["stamp_tax"],
                transfer_fee=fees["transfer_fee"],
                slippage=slippage_cost,
                realized_pnl=realized_pnl,
                timestamp=trade_time
            )
            self._fills[account_id].append(fill)

            order.filled_shares += fill_shares
            order.filled_amount += amount
            order.fees += total_fee
            order.slippage += slippage_cost
            order.updated_at = trade_time

            if order.filled_shares >= order.shares:
                OrderStateMachine.transition(order, OrderStatus.FILLED)
            else:
                OrderStateMachine.transition(order, OrderStatus.PARTIALLY_FILLED)

            mv = sum(p.market_value for p in positions.values())
            bal.update_metrics(mv)
            bal.updated_at = trade_time
            return fill

    def roll_trading_day(self, account_id: Optional[str] = None):
        """Rolls T+1 available shares across positions for standalone mode."""
        if self.ledger:
            # When backed by ledger, T+1 rollover is managed directly by self.ledger.roll_t1()
            return
        targets = [account_id] if account_id else list(self._positions.keys())
        for aid in targets:
            pos_dict = self._positions.get(aid, {})
            for sym, pos in pos_dict.items():
                pos.available_shares = pos.shares
