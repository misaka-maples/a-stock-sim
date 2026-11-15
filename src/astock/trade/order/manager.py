# -*- coding: utf-8 -*-
"""
A-Stock Order Manager
Orchestrates order creation, lifecycle state transitions, idempotency, and persistence.
"""

from typing import Dict, List, Optional, Any
import datetime
import uuid
from ..models import Order, OrderStatus, OrderSide, OrderType, TradeFill
from .state_machine import OrderStateMachine
from ..errors import DuplicateOrderError, OrderValidationError, OrderStateError


class OrderManager:
    """Manages full order lifecycle for all trading accounts."""

    def __init__(self):
        self._orders: Dict[str, Order] = {}               # order_id -> Order
        self._client_orders: Dict[str, Order] = {}        # client_order_id -> Order
        self._order_counter: int = 1

    def create_order(
        self,
        account_id: str,
        symbol: str,
        side: OrderSide,
        shares: int,
        price: float,
        client_order_id: Optional[str] = None,
        name: str = "",
        order_type: OrderType = OrderType.LIMIT,
        reason: str = "",
        risk_audit_id: Optional[str] = None
    ) -> Order:
        """Creates a new order in PROPOSED state with idempotency check."""
        if shares <= 0:
            raise OrderValidationError(f"Invalid order shares: {shares} (must be > 0)")
        if price <= 0:
            raise OrderValidationError(f"Invalid order price: {price} (must be > 0)")

        now_iso = datetime.datetime.now().isoformat()
        if client_order_id and client_order_id in self._client_orders:
            existing = self._client_orders[client_order_id]
            if existing.is_active:
                raise DuplicateOrderError(f"Active order with client_order_id {client_order_id} already exists")
            return existing

        cid = client_order_id or f"CID_{uuid.uuid4().hex[:12]}"
        oid = f"ORD_{datetime.datetime.now().strftime('%Y%m%d')}_{self._order_counter:06d}"
        self._order_counter += 1

        order = Order(
            order_id=oid,
            client_order_id=cid,
            account_id=account_id,
            symbol=symbol,
            name=name,
            side=side,
            order_type=order_type,
            price=round(price, 3),
            shares=shares,
            filled_shares=0,
            filled_amount=0.0,
            fees=0.0,
            slippage=0.0,
            status=OrderStatus.PROPOSED,
            created_at=now_iso,
            updated_at=now_iso,
            reason=reason,
            risk_audit_id=risk_audit_id
        )

        self._orders[oid] = order
        self._client_orders[cid] = order
        return order

    def submit_order(self, order: Order) -> Order:
        if order.order_id not in self._orders:
            self._orders[order.order_id] = order
            if order.client_order_id:
                self._client_orders[order.client_order_id] = order
        OrderStateMachine.transition(order, OrderStatus.SUBMITTED)
        order.updated_at = datetime.datetime.now().isoformat()
        return order

    def update_status(self, order_id: str, new_status: OrderStatus, reason: str = "") -> Order:
        order = self.get_order(order_id)
        if not order:
            raise OrderValidationError(f"Order {order_id} not found")
        OrderStateMachine.transition(order, new_status, reason)
        order.updated_at = datetime.datetime.now().isoformat()
        return order

    def record_partial_fill(
        self,
        order_id: str,
        fill_shares: int,
        fill_price: float,
        fee: float,
        slippage: float = 0.0
    ) -> Order:
        order = self.get_order(order_id)
        if not order:
            raise OrderValidationError(f"Order {order_id} not found")

        if fill_shares <= 0 or fill_shares > order.remaining_shares:
            raise OrderValidationError(
                f"Invalid fill shares {fill_shares}, remaining: {order.remaining_shares}"
            )

        order.filled_shares += fill_shares
        order.filled_amount += round(fill_shares * fill_price, 2)
        order.fees += round(fee, 2)
        order.slippage += round(slippage, 2)
        order.updated_at = datetime.datetime.now().isoformat()

        if order.filled_shares >= order.shares:
            OrderStateMachine.transition(order, OrderStatus.FILLED)
        else:
            OrderStateMachine.transition(order, OrderStatus.PARTIALLY_FILLED)

        return order

    def mark_rejected(self, order_id: str, rejection_reason: str) -> Order:
        order = self.get_order(order_id)
        if not order:
            raise OrderValidationError(f"Order {order_id} not found")
        order.rejection_reason = rejection_reason
        OrderStateMachine.transition(order, OrderStatus.REJECTED)
        order.updated_at = datetime.datetime.now().isoformat()
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_by_client_id(self, client_order_id: str) -> Optional[Order]:
        return self._client_orders.get(client_order_id)

    def get_active_orders(self, account_id: Optional[str] = None) -> List[Order]:
        orders = [o for o in self._orders.values() if o.is_active]
        if account_id:
            orders = [o for o in orders if o.account_id == account_id]
        return orders

    def get_all_orders(self, account_id: Optional[str] = None) -> List[Order]:
        orders = list(self._orders.values())
        if account_id:
            orders = [o for o in orders if o.account_id == account_id]
        return orders

    def load_from_records(self, records: List[Any]):
        """Restores orders from persisted storage on system startup."""
        for r in records:
            if isinstance(r, Order):
                order = r
                oid = order.order_id
                cid = order.client_order_id or oid
            elif isinstance(r, dict):
                oid = r.get("order_id")
                cid = r.get("client_order_id") or oid
                if not oid:
                    continue
                order = Order(
                    order_id=oid,
                    client_order_id=cid,
                    account_id=r.get("account_id", "default"),
                    symbol=r.get("symbol", ""),
                    name=r.get("name", ""),
                    side=OrderSide(r.get("side", "BUY")),
                    order_type=OrderType(r.get("order_type", "LIMIT")),
                    price=float(r.get("price", 0.0)),
                    shares=int(r.get("shares", 0)),
                    filled_shares=int(r.get("filled_shares", 0)),
                    filled_amount=float(r.get("filled_amount", 0.0)),
                    fees=float(r.get("fees", 0.0)),
                    slippage=float(r.get("slippage", 0.0)),
                    status=OrderStatus(r.get("status", "SUBMITTED")),
                    created_at=r.get("created_at", ""),
                    updated_at=r.get("updated_at", ""),
                    reason=r.get("reason", ""),
                    risk_audit_id=r.get("risk_audit_id"),
                    rejection_reason=r.get("rejection_reason")
                )
            else:
                continue

            self._orders[oid] = order
            if cid:
                self._client_orders[cid] = order
            # Update counter
            try:
                num = int(oid.split("_")[-1])
                self._order_counter = max(self._order_counter, num + 1)
            except Exception:
                pass

    def restore_from_ledger(self, ledger: Any) -> int:
        """Loads all orders from SQLite ledger into in-memory indices."""
        if not ledger:
            return 0
        orders = []
        if hasattr(ledger, "get_orders"):
            orders = ledger.get_orders()
        elif hasattr(ledger, "get_active_orders"):
            orders = ledger.get_active_orders()
        self.load_from_records(orders)
        return len(orders)

    def mark_stale_orders_unknown(self, timeout_seconds: int = 86400 * 3) -> List[Order]:
        """Flags old unconfirmed open orders as UNKNOWN to prevent ghost states."""
        now = datetime.datetime.now()
        flagged = []
        for o in self.get_active_orders():
            try:
                created_dt = datetime.datetime.fromisoformat(o.created_at)
                if (now - created_dt).total_seconds() > timeout_seconds:
                    OrderStateMachine.transition(o, OrderStatus.UNKNOWN, "Timeout on recovery")
                    flagged.append(o)
            except Exception:
                pass
        return flagged
