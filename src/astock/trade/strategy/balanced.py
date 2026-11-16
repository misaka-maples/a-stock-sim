# -*- coding: utf-8 -*-
"""
A-Stock Balanced V3 Strategy
Combines Product A (Core Blue Chips) and Product B (Dynamic Sector Momentum).
Risk Profile: Medium, steady growth.
Caps: RISK_OFF=30%, NEUTRAL=45%, RISK_ON=65%, STRONG_RISK_ON=80%.
"""

from typing import Dict, List, Optional, Any
import uuid
from .base import BaseStrategy
from .signal_inputs import candidate_signals
from ..models import (
    Signal, TargetPosition, TargetPortfolio, MarketRegime, Position, AccountBalance
)
from ..risk.portfolio import PortfolioRiskRule, BALANCED_REGIME_CAPS


PRODUCT_A_CORE = [
    "sh600519", "sh601318", "sz300750", "sz002594", "sh600036",
    "sh601899", "sh600900", "sh601138", "sz002475", "sz000001",
    "sh600237", "sh600105", "sz003035", "sh603578", "sh601127",
    "sz002230", "sz002460"
]

BALANCED_AB_BUDGETS = {
    MarketRegime.RISK_OFF: (0.70, 0.30),
    MarketRegime.NEUTRAL: (0.60, 0.40),
    MarketRegime.RISK_ON: (0.20, 0.80),
    MarketRegime.STRONG_RISK_ON: (0.50, 0.50),
}


class BalancedStrategy(BaseStrategy):
    """Balanced dual-pool quantitative momentum strategy."""

    def __init__(self, account_id: str = "balanced"):
        super().__init__(name="Product_AB_Balanced_V3", account_id=account_id)
        self.risk_rule = PortfolioRiskRule(account_type="balanced")
        self.max_positions = 6
        self.max_stock_weight = 0.15
        self.max_sector_weight = 0.30

    def generate_signals(
        self,
        as_of: str,
        market_data: Dict[str, Any],
        regime: MarketRegime
    ) -> List[Signal]:
        """
        Extracts candidate signals from precomputed momentum metrics.
        """
        run_id = f"SIG_{uuid.uuid4().hex[:8]}"
        return candidate_signals(as_of, market_data, run_id, "momentum_score")

    def plan_targets(
        self,
        as_of: str,
        regime: MarketRegime,
        signals: List[Signal],
        balance: AccountBalance,
        positions: Dict[str, Position],
        is_frozen: bool = False
    ) -> TargetPortfolio:
        """
        Builds TargetPortfolio observing:
        - Drawdown freeze (if active -> 0% new buys)
        - Regime total position cap (30%/45%/65%/80%)
        - A/B budget distribution
        - Single stock 15% and sector 30% concentration limits
        """
        max_regime_cap = self.risk_rule.get_effective_regime_cap(regime, is_frozen)
        total_equity = balance.total_equity

        curr_stock_val = sum(p.market_value for p in positions.values())
        curr_stock_wt = curr_stock_val / total_equity if total_equity > 0 else 0.0
        remaining_allowed_wt = max(0.0, max_regime_cap - curr_stock_wt)

        target_positions: Dict[str, TargetPosition] = {}
        for sym, pos in positions.items():
            p_wt = pos.market_value / total_equity if total_equity > 0 else 0.0
            target_positions[sym] = TargetPosition(symbol=sym, target_weight=round(p_wt, 4), reason="HOLD_EXISTING")
        allocated_weight = 0.0

        if remaining_allowed_wt >= 0.02 and not is_frozen:
            w_a, w_b = BALANCED_AB_BUDGETS.get(regime, (0.70, 0.30))

            # Split signals into A (core) and B (dynamic)
            sigs_a = [s for s in signals if s.symbol in PRODUCT_A_CORE]
            sigs_b = [s for s in signals if s.symbol not in PRODUCT_A_CORE]

            available_slots = max(0, self.max_positions - len(positions))
            target_weights: Dict[str, float] = {}
            budget_a = remaining_allowed_wt * w_a
            budget_b = remaining_allowed_wt * w_b

            alloc_a = 0.0
            if w_a > 0 and sigs_a:
                for s in sigs_a[:available_slots]:
                    wt = min(self.max_stock_weight, max(0.0, budget_a - alloc_a))
                    if wt >= 0.02:
                        target_weights[s.symbol] = round(wt, 4)
                        alloc_a += wt

            avail_b_budget = budget_b + max(0.0, budget_a - alloc_a)
            alloc_b = 0.0
            if w_b > 0 and sigs_b:
                rem_slots = max(0, available_slots - len(target_weights))
                for s in sigs_b[:rem_slots]:
                    wt = min(self.max_stock_weight, max(0.0, avail_b_budget - alloc_b))
                    if wt >= 0.02:
                        target_weights[s.symbol] = round(wt, 4)
                        alloc_b += wt

            # Enforce concentration
            sector_wts: Dict[str, float] = {}
            for p in positions.values():
                sec = getattr(p, "sector", "其他")
                p_wt = p.market_value / total_equity if total_equity > 0 else 0.0
                sector_wts[sec] = sector_wts.get(sec, 0.0) + p_wt

            for sym, raw_wt in sorted(target_weights.items(), key=lambda x: -x[1]):
                if sym in positions:
                    continue
                if len(positions) + len(target_positions) >= self.max_positions:
                    break
                if allocated_weight >= remaining_allowed_wt - 1e-4:
                    break

                sig = next((s for s in signals if s.symbol == sym), None)
                sec = sig.sector if sig else "其他"
                curr_sec = sector_wts.get(sec, 0.0)
                sec_headroom = max(0.0, self.max_sector_weight - curr_sec)

                final_wt = min(raw_wt, self.max_stock_weight, remaining_allowed_wt - allocated_weight, sec_headroom)
                final_wt = round(final_wt, 4)

                if final_wt >= 0.02:
                    target_positions[sym] = TargetPosition(
                        symbol=sym,
                        target_weight=final_wt,
                        reason=f"Product_AB_Balanced_{regime.value}(wA:{w_a*100:.0f}%/wB:{w_b*100:.0f}%)"
                    )
                    allocated_weight += final_wt
                    sector_wts[sec] = curr_sec + final_wt

        total_target_wt = round(curr_stock_wt + allocated_weight, 4)
        cash_wt = round(max(0.0, 1.0 - total_target_wt), 4)

        return TargetPortfolio(
            account_id=self.account_id,
            target_date=as_of,
            target_positions=target_positions,
            regime=regime,
            regime_cap=max_regime_cap,
            total_weight=total_target_wt,
            cash_weight=cash_wt,
            constraints={
                "max_positions": self.max_positions,
                "max_stock_weight": self.max_stock_weight,
                "max_sector_weight": self.max_sector_weight,
                "is_frozen": is_frozen
            },
            reasons=[f"Balanced V3 plan for regime {regime.value}"]
        )
