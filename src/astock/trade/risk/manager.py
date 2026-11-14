# -*- coding: utf-8 -*-
"""
A-Stock Unified Risk Manager
Central orchestrator across Market, Portfolio, Position, and Order risk layers.
Maintains full audit trail for explainability and regulatory compliance.
"""

from typing import Dict, List, Optional, Tuple, Any
import datetime
from .market import MarketRiskRule
from .portfolio import PortfolioRiskRule
from .position import PositionRiskRule
from .order import OrderRiskRule
from ..models import (
    RiskAuditResult, RiskLayer, RiskSeverity, MarketRegime, Order, Position, AccountBalance
)


class RiskManager:
    """Unified quantitative risk manager with 4-layer hierarchy."""

    def __init__(self, account_type: str = "balanced"):
        self.account_type = account_type.lower()
        self.market_rule = MarketRiskRule()
        self.portfolio_rule = PortfolioRiskRule(account_type=self.account_type)
        self.position_rule = PositionRiskRule()
        self.order_rule = OrderRiskRule()
        self.audit_trail: List[RiskAuditResult] = []

    def log_audit(self, result: RiskAuditResult):
        self.audit_trail.append(result)

    def evaluate_market_regime(
        self,
        bench_bars: List[Dict[str, Any]],
        as_of: str,
        candidate_bars: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ) -> Tuple[MarketRegime, float]:
        regime, breadth, audit = self.market_rule.determine_regime(bench_bars, as_of, candidate_bars)
        self.log_audit(audit)
        return regime, breadth

    def check_portfolio_drawdown(
        self,
        balance: AccountBalance
    ) -> Tuple[bool, bool]:
        is_alert, is_freeze, audit = self.portfolio_rule.check_drawdown_circuit_breaker(balance)
        self.log_audit(audit)
        return is_alert, is_freeze

    def check_position_exit(
        self,
        pos: Position,
        current_price: float,
        atr: float,
        ma20: float,
        consecutive_below_ma20: int = 0
    ) -> Tuple[bool, str, RiskAuditResult]:
        should_exit, reason, audit = self.position_rule.check_position_exit(
            pos, current_price, atr, ma20, consecutive_below_ma20
        )
        self.log_audit(audit)
        return should_exit, reason, audit

    def validate_target_allocation(
        self,
        symbol: str,
        target_weight: float,
        sector: str,
        current_positions: Dict[str, Position],
        total_equity: float,
        regime: MarketRegime,
        is_frozen: bool = False
    ) -> Tuple[float, RiskAuditResult]:
        final_wt, audit = self.portfolio_rule.validate_allocation(
            symbol, target_weight, sector, current_positions, total_equity, regime, is_frozen
        )
        self.log_audit(audit)
        return final_wt, audit

    def validate_pre_trade_order(
        self,
        order: Order,
        available_cash: float,
        current_position: Optional[Position] = None,
        prev_close: Optional[float] = None,
        is_suspended: bool = False,
        is_st: bool = False
    ) -> Tuple[bool, RiskAuditResult]:
        ok, audit = self.order_rule.validate_order(
            order, available_cash, current_position, prev_close, is_suspended, is_st
        )
        self.log_audit(audit)
        if ok:
            order.risk_audit_id = audit.audit_id
        return ok, audit

    def get_audit_trail(
        self,
        layer: Optional[RiskLayer] = None,
        passed: Optional[bool] = None,
        limit: int = 100
    ) -> List[RiskAuditResult]:
        """Queries audit history with optional filters."""
        results = self.audit_trail
        if layer:
            results = [r for r in results if r.layer == layer]
        if passed is not None:
            results = [r for r in results if r.passed == passed]
        return results[-limit:]
