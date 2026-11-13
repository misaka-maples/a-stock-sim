# src/astock/domain/errors.py

class AStockError(Exception):
    """Base exception for all A-Stock-Sim domain errors."""
    pass


class InfeasibleConfigError(AStockError):
    """Raised when portfolio parameters are mathematically contradictory or infeasible."""
    def __init__(self, message: str, desired_cap: float = 0.0, feasible_cap: float = 0.0, reasons: list = None):
        super().__init__(message)
        self.desired_cap = desired_cap
        self.feasible_cap = feasible_cap
        self.reasons = reasons or []


class DataQualityError(AStockError):
    """Raised when market data does not meet validity/completeness thresholds."""
    pass


class RiskRejectionError(AStockError):
    """Raised when an order or allocation is blocked by pre-trade or portfolio risk guards."""
    pass


class OrderLifecycleError(AStockError):
    """Raised when an illegal order state transition or duplicate idempotency key occurs."""
    pass


class AccountReconciliationError(AStockError):
    """Raised when ledger and broker/feed balances, positions, or cash mismatch."""
    pass
