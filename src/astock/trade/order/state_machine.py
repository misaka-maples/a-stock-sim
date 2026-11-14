# -*- coding: utf-8 -*-
"""
A-Stock Order State Machine
Enforces strict lifecycle transitions and handling of terminal/unknown states.
"""

from typing import Dict, Set
from ..models import OrderStatus, Order
from ..errors import OrderStateError


VALID_TRANSITIONS: Dict[OrderStatus, Set[OrderStatus]] = {
    OrderStatus.PROPOSED: {
        OrderStatus.RISK_APPROVED,
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED
    },
    OrderStatus.RISK_APPROVED: {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED
    },
    OrderStatus.PENDING_SUBMIT: {
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.UNKNOWN
    },
    OrderStatus.SUBMITTED: {
        OrderStatus.ACKED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.PENDING_CANCEL,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.UNKNOWN
    },
    OrderStatus.ACKED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.PENDING_CANCEL,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.UNKNOWN
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.PENDING_CANCEL,
        OrderStatus.CANCELLED,
        OrderStatus.UNKNOWN
    },
    OrderStatus.PENDING_CANCEL: {
        OrderStatus.CANCELLED,
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.UNKNOWN
    },
    # Recoverable UNKNOWN state: on reconnect/reconcile, broker state is restored
    OrderStatus.UNKNOWN: {
        OrderStatus.SUBMITTED,
        OrderStatus.ACKED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED
    },
    # Terminal states
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set()
}


class OrderStateMachine:
    """Manages order state transitions with validation."""

    @staticmethod
    def transition(order: Order, new_status: OrderStatus, reason: str = "") -> Order:
        """
        Transitions an order to a new status.
        Raises OrderStateError if transition is invalid.
        """
        current_status = order.status
        if current_status == new_status:
            return order

        allowed = VALID_TRANSITIONS.get(current_status, set())
        if new_status not in allowed:
            raise OrderStateError(
                f"Invalid order transition from {current_status.value} to {new_status.value} for order {order.order_id}"
            )

        order.status = new_status
        if reason:
            order.updated_at = reason
        return order

    @staticmethod
    def is_terminal(status: OrderStatus) -> bool:
        return status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED
        )
