# src/astock/domain/protocols.py
from typing import Any, Dict, List, Optional, Protocol
from .models import (
    MarketSnapshot,
    Signal,
    TargetPortfolio,
    OrderIntent,
    ExecutionReport,
    AccountView
)
from .enums import MarketRegime


class StrategyPort(Protocol):
    """Pure strategy interface: inputs immutable snapshot, outputs signals."""
    def generate(self, snapshot: MarketSnapshot, config: Dict[str, Any]) -> List[Signal]:
        ...


class PortfolioAllocator(Protocol):
    """Allocates portfolio weights based on signals, account status, and regime."""
    def allocate(
        self,
        signals: List[Signal],
        account: AccountView,
        regime: MarketRegime,
        policy: Dict[str, Any]
    ) -> TargetPortfolio:
        ...


class OrderManagerPort(Protocol):
    """Translates TargetPortfolio into actionable OrderIntents with idempotency keys."""
    def submit_target(self, target: TargetPortfolio, account: AccountView) -> List[OrderIntent]:
        ...


class BrokerPort(Protocol):
    """Broker interface for order submission, cancellation, and balance reconciliation."""
    def submit(self, order: OrderIntent) -> ExecutionReport:
        ...

    def cancel(self, client_order_id: str) -> bool:
        ...

    def reconcile(self, account_id: str) -> Dict[str, Any]:
        ...


class LedgerRepository(Protocol):
    """Transactional persistence contract for cash, positions, orders, and fills."""
    def record_fill(self, report: ExecutionReport) -> None:
        ...

    def get_account_view(self, account_id: str) -> AccountView:
        ...
