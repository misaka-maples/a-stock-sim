# -*- coding: utf-8 -*-
"""
A-Stock Quant Trading Core Domain Models
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any
import datetime
import uuid


class MarketRegime(str, Enum):
    RISK_OFF = "RISK_OFF"
    NEUTRAL = "NEUTRAL"
    RISK_ON = "RISK_ON"
    STRONG_RISK_ON = "STRONG_RISK_ON"
    UNKNOWN = "UNKNOWN"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PROPOSED = "PROPOSED"
    RISK_APPROVED = "RISK_APPROVED"
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class RiskLayer(str, Enum):
    MARKET = "MARKET"
    PORTFOLIO = "PORTFOLIO"
    POSITION = "POSITION"
    ORDER = "ORDER"


class RiskSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCK = "BLOCK"
    FREEZE = "FREEZE"


@dataclass(frozen=True)
class Signal:
    run_id: str
    signal_asof: str
    symbol: str
    name: str = ""
    rank: int = 1
    score: float = 0.0
    sector: str = "其他"
    price: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    ma120: float = 0.0
    atr: float = 0.0
    ret20: float = 0.0
    ret60: float = 0.0
    features: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    target_weight: float
    target_shares: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class TargetPortfolio:
    account_id: str
    target_date: str
    target_positions: Dict[str, TargetPosition]
    regime: MarketRegime
    regime_cap: float
    total_weight: float
    cash_weight: float
    constraints: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


@dataclass
class RiskAuditResult:
    audit_id: str
    timestamp: str
    account_id: str
    layer: RiskLayer
    rule_name: str
    passed: bool
    severity: RiskSeverity
    action_taken: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["layer"] = self.layer.value if isinstance(self.layer, RiskLayer) else self.layer
        d["severity"] = self.severity.value if isinstance(self.severity, RiskSeverity) else self.severity
        return d


@dataclass
class Order:
    order_id: str
    client_order_id: str
    account_id: str
    symbol: str
    side: OrderSide
    shares: int
    name: str = ""
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    filled_shares: int = 0
    filled_amount: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    status: OrderStatus = OrderStatus.PROPOSED
    created_at: str = ""
    updated_at: str = ""
    reason: str = ""
    risk_audit_id: Optional[str] = None
    rejection_reason: Optional[str] = None

    @property
    def remaining_shares(self) -> int:
        return max(0, self.shares - self.filled_shares)

    @property
    def is_active(self) -> bool:
        return self.status in (
            OrderStatus.PROPOSED,
            OrderStatus.RISK_APPROVED,
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.ACKED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_CANCEL
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value if isinstance(self.side, OrderSide) else self.side
        d["order_type"] = self.order_type.value if isinstance(self.order_type, OrderType) else self.order_type
        d["status"] = self.status.value if isinstance(self.status, OrderStatus) else self.status
        return d


@dataclass(frozen=True)
class TradeFill:
    fill_id: str
    order_id: str
    client_order_id: str
    account_id: str
    symbol: str
    name: str
    side: OrderSide
    price: float
    shares: int
    amount: float
    commission: float
    stamp_tax: float
    transfer_fee: float
    slippage: float
    realized_pnl: float
    timestamp: str

    @property
    def total_fee(self) -> float:
        return round(self.commission + self.stamp_tax + self.transfer_fee, 2)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value if isinstance(self.side, OrderSide) else self.side
        d["total_fee"] = self.total_fee
        return d


@dataclass
class Position:
    symbol: str
    shares: int
    available_shares: int  # T+1 tradable
    cost_price: float
    name: str = ""
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    highest_price: float = 0.0
    entry_date: str = ""
    entry_regime: str = ""
    entry_reason: str = ""
    holding_days: int = 0
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None

    def update_price(self, price: float):
        if price <= 0:
            return
        self.current_price = price
        self.highest_price = max(self.highest_price, price)
        self.market_value = round(self.shares * price, 2)
        if self.cost_price > 0:
            self.unrealized_pnl = round((price - self.cost_price) * self.shares, 2)
            self.unrealized_pnl_pct = round((price - self.cost_price) / self.cost_price * 100.0, 2)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AccountBalance:
    account_id: str
    cash: float
    frozen_cash: float
    stock_market_value: float
    total_equity: float
    initial_capital: float = 100000.0
    peak_equity: float = 100000.0
    max_drawdown_pct: float = 0.0
    current_drawdown_pct: float = 0.0
    is_frozen: bool = False
    updated_at: str = ""

    def update_metrics(self, market_value: float):
        self.stock_market_value = round(market_value, 2)
        self.total_equity = round(self.cash + self.frozen_cash + self.stock_market_value, 2)
        self.peak_equity = max(self.peak_equity, self.total_equity)
        if self.peak_equity > 0:
            dd = (self.peak_equity - self.total_equity) / self.peak_equity * 100.0
            self.current_drawdown_pct = round(max(0.0, dd), 2)
            self.max_drawdown_pct = max(self.max_drawdown_pct, self.current_drawdown_pct)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
