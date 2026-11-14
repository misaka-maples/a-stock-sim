# -*- coding: utf-8 -*-
"""
A-Stock Unified Broker Port Interface
All simulated and real broker implementations must implement this standard protocol.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from ..models import Order, OrderStatus, Position, TradeFill, AccountBalance


class BaseBrokerAdapter(ABC):
    """Abstract Base Broker Adapter."""

    @abstractmethod
    def get_cash(self, account_id: str) -> float:
        """Returns available unencumbered cash balance."""
        pass

    @abstractmethod
    def get_positions(self, account_id: str) -> Dict[str, Position]:
        """Returns all open positions for an account."""
        pass

    @abstractmethod
    def get_sellable_shares(self, account_id: str, symbol: str) -> int:
        """Returns T+1 available sellable shares for a symbol."""
        pass

    @abstractmethod
    def get_open_orders(self, account_id: str) -> List[Order]:
        """Returns all active, unfulfilled orders."""
        pass

    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """Submits an order to the execution broker/market."""
        pass

    @abstractmethod
    def cancel_order(self, account_id: str, order_id: str) -> bool:
        """Requests cancellation of an active order."""
        pass

    @abstractmethod
    def get_order_status(self, account_id: str, order_id: str) -> OrderStatus:
        """Queries the current broker status of an order."""
        pass

    @abstractmethod
    def get_fills(self, account_id: str, order_id: Optional[str] = None) -> List[TradeFill]:
        """Returns trade fills/executions."""
        pass
