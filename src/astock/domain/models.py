# src/astock/domain/models.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from decimal import Decimal

from .enums import (
    MarketRegime,
    OrderState,
    OrderSide,
    OrderType,
    DataQuality,
    AccountMode,
    ReasonCode
)


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float  # reported in lots (100 shares)
    amount: float  # RMB
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    adjustment_factor: float
    source: str
    raw_pre_close: Optional[float] = None
    is_suspended: bool = False
    is_st: bool = False


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: str
    bid1: float
    bid1_vol: int
    ask1: float
    ask1_vol: int
    last_price: float
    source: str
    is_stale: bool = False


@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_id: str
    signal_asof_date: str
    available_at: str
    universe_version: str
    sector_version: str
    bars_by_symbol: Dict[str, List[Bar]]
    benchmark_bars: List[Bar]
    market_regime: MarketRegime
    quality: DataQuality
    market_breadth: Optional[float] = None
    sector_map: Dict[str, str] = field(default_factory=dict)
    source_lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    run_id: str
    signal_asof: str
    symbol: str
    rank: int
    momentum_score: float
    sector_score: float
    sector: str
    features: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    rule_version: str = "v3.0"


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    target_weight: float  # e.g., 0.15 for 15%
    target_shares: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class TargetPortfolio:
    account_id: str
    target_date: str
    target_positions: Dict[str, TargetPosition]
    target_equity_weight: float
    cash_weight: float
    regime: MarketRegime
    regime_cap: float
    constraints: Dict[str, Any] = field(default_factory=dict)
    rejected_targets: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    client_order_id: str
    account_id: str
    strategy_run_id: str
    symbol: str
    side: OrderSide
    shares: int
    order_type: OrderType = OrderType.LIMIT
    limit_price: Optional[float] = None
    deadline: Optional[str] = None
    reason: str = ""
    status: OrderState = OrderState.PROPOSED
    created_at: str = ""


@dataclass(frozen=True)
class ExecutionReport:
    broker_order_id: str
    client_order_id: str
    account_id: str
    symbol: str
    side: OrderSide
    shares: int
    price: float
    fee: float
    status: OrderState
    event_time: str
    execution_id: str


@dataclass(frozen=True)
class PositionLot:
    lot_id: str
    account_id: str
    symbol: str
    shares: int
    available_shares: int  # T+1 tradable shares
    cost_price: float
    bought_at: str


@dataclass(frozen=True)
class CashJournalEntry:
    event_id: str
    account_id: str
    delta_cents: int  # preserved in cents
    reason: str
    ref_fill_id: Optional[str] = None
    created_at: str = ""


@dataclass(frozen=True)
class AccountView:
    account_id: str
    mode: AccountMode
    cash: float
    positions: Dict[str, Any]
    total_equity: float
    peak_equity: float
    max_drawdown_pct: float
    current_drawdown_pct: float
    initial_cash: float = 100000.0
