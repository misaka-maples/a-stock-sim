# -*- coding: utf-8 -*-
"""
A-Stock Portfolio Manager
Coordinates capital allocation across Balanced and Aggressive peer portfolios.
Enforces that Balanced and Aggressive are autonomous peers without lifecycle coupling.
"""

from typing import Dict, List, Optional, Any
from ..models import (
    TargetPortfolio, Signal, MarketRegime, Position, AccountBalance, Order
)
from ..risk.manager import RiskManager
from ..order.manager import OrderManager
from ..broker.base import BaseBrokerAdapter
from .rebalancer import PortfolioRebalancer


class PortfolioManager:
    """Multi-account quantitative portfolio management orchestrator."""

    def __init__(
        self,
        risk_manager: RiskManager,
        order_manager: OrderManager,
        broker: BaseBrokerAdapter
    ):
        self.risk_manager = risk_manager
        self.order_manager = order_manager
        self.broker = broker
        self.rebalancer = PortfolioRebalancer()

    def execute_rebalance(
        self,
        target_portfolio: TargetPortfolio,
        price_map: Dict[str, float],
        stock_info_map: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> List[Order]:
        """
        Translates TargetPortfolio into orders, validates through pre-trade risk,
        and submits to broker.
        """
        account_id = target_portfolio.account_id
        positions = self.broker.get_positions(account_id)
        cash = self.broker.get_cash(account_id)

        # Temporary balance view
        mv = sum(p.market_value for p in positions.values())
        balance = AccountBalance(
            account_id=account_id,
            cash=cash,
            frozen_cash=0.0,
            stock_market_value=mv,
            total_equity=cash + mv
        )

        orders = self.rebalancer.generate_orders_from_target(
            target_portfolio=target_portfolio,
            current_positions=positions,
            balance=balance,
            price_map=price_map,
            order_manager=self.order_manager
        )

        executed_orders: List[Order] = []
        for order in orders:
            s_info = stock_info_map.get(order.symbol, {}) if stock_info_map else {}
            prev_close = s_info.get("raw_pre_close") or s_info.get("pre_close")
            is_susp = s_info.get("is_suspended", False)
            is_st = s_info.get("is_st", False)

            curr_pos = positions.get(order.symbol)
            passed, audit = self.risk_manager.validate_pre_trade_order(
                order=order,
                available_cash=cash,
                current_position=curr_pos,
                prev_close=prev_close,
                is_suspended=is_susp,
                is_st=is_st
            )

            if passed:
                self.broker.submit_order(order)
                executed_orders.append(order)
            else:
                self.order_manager.mark_rejected(order.order_id, audit.message)

        return executed_orders
