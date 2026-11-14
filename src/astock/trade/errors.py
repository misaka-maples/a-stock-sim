# -*- coding: utf-8 -*-
"""
A-Stock Quant Trading Core Exceptions
"""

class TradeError(Exception):
    """Base exception for all trading operations."""
    pass

class RiskCheckError(TradeError):
    """Raised when an order or portfolio state violates risk control rules."""
    def __init__(self, rule_name: str, message: str, details: dict = None):
        super().__init__(f"[{rule_name}] {message}")
        self.rule_name = rule_name
        self.details = details or {}

class OrderValidationError(TradeError):
    """Raised when order parameters are invalid."""
    pass

class OrderStateError(TradeError):
    """Raised when an invalid state transition is attempted on an order."""
    pass

class BrokerError(TradeError):
    """Raised when the broker adapter encounters an execution or connectivity error."""
    pass

class InsufficientCashError(BrokerError):
    """Raised when account cash is insufficient to fulfill a buy order."""
    pass

class InsufficientSharesError(BrokerError):
    """Raised when available tradable shares are insufficient to fulfill a sell order."""
    pass

class DuplicateOrderError(OrderValidationError):
    """Raised when an order with the same client_order_id has already been submitted."""
    pass

class LedgerError(TradeError):
    """Raised on ledger accounting inconsistencies."""
    pass
