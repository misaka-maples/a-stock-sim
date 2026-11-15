# -*- coding: utf-8 -*-
"""
A-Stock Aggressive V3 Strategy
Pure dynamic sector momentum offense.
Risk Profile: High risk, aggressive capital utilization.
Caps: RISK_OFF=50%, NEUTRAL=70%, RISK_ON=90%, STRONG_RISK_ON=100%.
Max positions: 5, Single stock cap: 25%, Sector cap: 40%.
"""

from typing import Dict, List, Optional, Any
import uuid
from .base import BaseStrategy
from ..models import (
    Signal, TargetPosition, TargetPortfolio, MarketRegime, Position, AccountBalance
)
from ..risk.portfolio import PortfolioRiskRule, AGGRESSIVE_REGIME_CAPS


class AggressiveStrategy(BaseStrategy):
    """Aggressive quantitative sector momentum offensive strategy."""

    def __init__(self, account_id: str = "aggressive"):
        super().__init__(name="Product_B_Aggressive_V3", account_id=account_id)
        self.risk_rule = PortfolioRiskRule(account_type="aggressive")
        self.max_positions = 5
        self.max_stock_weight = 0.25
        self.max_sector_weight = 0.40

    def generate_signals(
        self,
        as_of: str,
        market_data: Dict[str, Any],
        regime: MarketRegime
    ) -> List[Signal]:
        run_id = f"SIG_AGG_{uuid.uuid4().hex[:8]}"
        candidates = market_data.get("candidates", [])
        signals = []
        for rank, c in enumerate(candidates, 1):
            sig = Signal(
                run_id=run_id,
                signal_asof=as_of,
                symbol=c.get("symbol", ""),
                name=c.get("name", ""),
                rank=rank,
                score=float(c.get("score", 0.0)),
                sector=c.get("sector", "其他"),
                price=float(c.get("price", c.get("close", 0.0))),
                ma20=float(c.get("ma20", 0.0)),
                ma60=float(c.get("ma60", 0.0)),
                ma120=float(c.get("ma120", 0.0)),
                atr=float(c.get("atr", 0.0)),
                ret20=float(c.get("ret20", 0.0)),
                ret60=float(c.get("ret60", 0.0)),
                reasons=[f"aggressive_momentum_{c.get('score', 0):.2f}"]
            )
            signals.append(sig)
        return signals

    def plan_targets(
        self,
        as_of: str,
        regime: MarketRegime,
        signals: List[Signal],
        balance: AccountBalance,
        positions: Dict[str, Position],
        is_frozen: bool = False
    ) -> TargetPortfolio:
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
            available_slots = max(0, self.max_positions - len(positions))

            sector_wts: Dict[str, float] = {}
            for p in positions.values():
                sec = getattr(p, "sector", "其他")
                p_wt = p.market_value / total_equity if total_equity > 0 else 0.0
                sector_wts[sec] = sector_wts.get(sec, 0.0) + p_wt

            for sig in signals[:available_slots]:
                sym = sig.symbol
                if sym in positions:
                    continue
                if len(positions) + len(target_positions) >= self.max_positions:
                    break
                if allocated_weight >= remaining_allowed_wt - 1e-4:
                    break

                sec = sig.sector
                curr_sec = sector_wts.get(sec, 0.0)
                sec_headroom = max(0.0, self.max_sector_weight - curr_sec)

                final_wt = min(self.max_stock_weight, remaining_allowed_wt - allocated_weight, sec_headroom)
                final_wt = round(final_wt, 4)

                if final_wt >= 0.02:
                    target_positions[sym] = TargetPosition(
                        symbol=sym,
                        target_weight=final_wt,
                        reason=f"Product_B_Aggressive_{regime.value}(rank:{sig.rank})"
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
            reasons=[f"Aggressive V3 plan for regime {regime.value}"]
        )
