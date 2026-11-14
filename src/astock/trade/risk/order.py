# -*- coding: utf-8 -*-
"""
A-Stock Order Risk Layer
Enforces pre-trade execution rules:
- Limit-up / limit-down price boundaries
- Stock suspension and ST checks
- Minimum lot size (multiples of 100 shares for BUY)
- Capital and available share sufficiency
- Pre-trade validation
"""

from typing import Dict, Optional, Tuple, Any
from ..models import (
    Order, OrderSide, RiskLayer, RiskSeverity, RiskAuditResult, Position
)
import uuid
import datetime


class OrderRiskRule:
    """Pre-trade risk validator before order submission to broker."""

    @staticmethod
    def get_limit_ratio(symbol: str) -> float:
        """Returns price limit ratio: 20% for 300/688, 10% for mainboard."""
        code = symbol[-6:]
        if code.startswith(("300", "301", "688")):
            return 0.20
        return 0.10

    def validate_order(
        self,
        order: Order,
        available_cash: float,
        current_position: Optional[Position] = None,
        prev_close: Optional[float] = None,
        is_suspended: bool = False,
        is_st: bool = False
    ) -> Tuple[bool, RiskAuditResult]:
        """
        Validates order against all pre-trade rules.
        """
        audit_id = f"RA_ORD_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now().isoformat()

        # 1. Suspension Check
        if is_suspended:
            return False, RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
                layer=RiskLayer.ORDER, rule_name="SUSPENSION_CHECK",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ORDER",
                message=f"Stock {order.symbol} is suspended from trading",
                details={"symbol": order.symbol}
            )

        # 2. ST Check (Buy only)
        if order.side == OrderSide.BUY and is_st:
            return False, RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
                layer=RiskLayer.ORDER, rule_name="ST_RESTRICTION",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ORDER",
                message=f"Stock {order.symbol} is marked as ST/*ST; new buying prohibited",
                details={"symbol": order.symbol}
            )

        # 3. Lot Size Check
        if order.shares <= 0:
            return False, RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
                layer=RiskLayer.ORDER, rule_name="LOT_SIZE_CHECK",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ORDER",
                message=f"Order shares {order.shares} must be > 0",
                details={"shares": order.shares}
            )

        if order.side == OrderSide.BUY and order.shares % 100 != 0:
            return False, RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
                layer=RiskLayer.ORDER, rule_name="LOT_SIZE_CHECK",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ORDER",
                message=f"Buy order shares ({order.shares}) must be an exact multiple of 100",
                details={"shares": order.shares}
            )

        # 4. Limit-Up / Limit-Down Check
        if prev_close and prev_close > 0 and order.price > 0:
            ratio = self.get_limit_ratio(order.symbol)
            limit_up = round(prev_close * (1.0 + ratio), 2)
            limit_down = round(prev_close * (1.0 - ratio), 2)

            if order.side == OrderSide.BUY and order.price >= limit_up:
                return False, RiskAuditResult(
                    audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
                    layer=RiskLayer.ORDER, rule_name="LIMIT_PRICE_CHECK",
                    passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ORDER",
                    message=f"Buy price ¥{order.price:.2f} hits limit-up ¥{limit_up:.2f}",
                    details={"symbol": order.symbol, "price": order.price, "limit_up": limit_up}
                )

            if order.side == OrderSide.SELL and order.price <= limit_down:
                return False, RiskAuditResult(
                    audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
                    layer=RiskLayer.ORDER, rule_name="LIMIT_PRICE_CHECK",
                    passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ORDER",
                    message=f"Sell price ¥{order.price:.2f} hits limit-down ¥{limit_down:.2f}",
                    details={"symbol": order.symbol, "price": order.price, "limit_down": limit_down}
                )

        # 5. Capital & Share Sufficiency
        if order.side == OrderSide.BUY:
            est_cost = (order.price * order.shares) * 1.0005  # Buffer for commission
            if available_cash < est_cost - 1e-4:
                return False, RiskAuditResult(
                    audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
                    layer=RiskLayer.ORDER, rule_name="CASH_SUFFICIENCY",
                    passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ORDER",
                    message=f"Available cash ¥{available_cash:,.2f} insufficient for estimated cost ¥{est_cost:,.2f}",
                    details={"available_cash": available_cash, "est_cost": est_cost}
                )
        else: # SELL
            avail_shares = current_position.available_shares if current_position else 0
            if avail_shares < order.shares:
                return False, RiskAuditResult(
                    audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
                    layer=RiskLayer.ORDER, rule_name="SHARE_SUFFICIENCY",
                    passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ORDER",
                    message=f"Available shares ({avail_shares}) insufficient for sell order ({order.shares})",
                    details={"available_shares": avail_shares, "order_shares": order.shares}
                )

        return True, RiskAuditResult(
            audit_id=audit_id, timestamp=now_iso, account_id=order.account_id,
            layer=RiskLayer.ORDER, rule_name="PRE_TRADE_ORDER_APPROVAL",
            passed=True, severity=RiskSeverity.INFO, action_taken="APPROVE_ORDER",
            message="Order passed all pre-trade risk checks",
            details={"symbol": order.symbol, "side": order.side.value, "shares": order.shares, "price": order.price}
        )
