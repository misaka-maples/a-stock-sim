# -*- coding: utf-8 -*-
"""
A-Stock Portfolio Rebalancer
Translates TargetPortfolio specifications into executable Order intents,
respecting lot sizing (multiples of 100 shares for A-shares) and cash buffers.
"""

from typing import Dict, List, Optional, Tuple
from ..models import (
    TargetPortfolio, TargetPosition, Position, Order, OrderSide, OrderType, AccountBalance
)
from ..order.manager import OrderManager


class PortfolioRebalancer:
    """Calculates order intents from target portfolio allocations."""

    @staticmethod
    def generate_orders_from_target(
        target_portfolio: TargetPortfolio,
        current_positions: Dict[str, Position],
        balance: AccountBalance,
        price_map: Dict[str, float],
        order_manager: OrderManager
    ) -> List[Order]:
        """
        Converts TargetPortfolio into buy and sell orders.
        """
        orders: List[Order] = []
        account_id = target_portfolio.account_id
        total_equity = balance.total_equity
        target_positions = target_portfolio.target_positions

        # 1. Generate Sell Orders (for positions not in target or over target)
        for sym, pos in current_positions.items():
            if sym not in target_positions:
                # Liquidate
                if pos.available_shares > 0:
                    px = price_map.get(sym, pos.current_price)
                    order = order_manager.create_order(
                        account_id=account_id,
                        symbol=sym,
                        side=OrderSide.SELL,
                        shares=pos.available_shares,
                        price=px,
                        name=pos.name,
                        reason="REBALANCE_LIQUIDATION"
                    )
                    orders.append(order)

        # 2. Generate Buy Orders (for new targets)
        for sym, t_pos in target_positions.items():
            if sym not in current_positions:
                px = price_map.get(sym, 0.0)
                if px <= 0:
                    continue

                target_capital = total_equity * t_pos.target_weight
                raw_shares = int(target_capital / px)
                # Lot sizing: multiples of 100 shares
                lot_shares = (raw_shares // 100) * 100

                if lot_shares >= 100:
                    order = order_manager.create_order(
                        account_id=account_id,
                        symbol=sym,
                        side=OrderSide.BUY,
                        shares=lot_shares,
                        price=px,
                        reason=t_pos.reason
                    )
                    orders.append(order)

        return orders
