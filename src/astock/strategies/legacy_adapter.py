# src/astock/strategies/legacy_adapter.py
from typing import Any, Dict, List, Optional
import uuid
import datetime

from ..domain.enums import MarketRegime, OrderSide, OrderType, OrderState
from ..domain.models import (
    MarketSnapshot,
    Signal,
    TargetPosition,
    TargetPortfolio,
    OrderIntent,
    AccountView
)
from ..portfolio.config_validator import check_portfolio_feasibility


class LegacyStrategyAdapter:
    """
    Adapter that wraps existing legacy strategy logic (DailyMomentumStrategy,
    ProductABv2DualCoordinator) into the V3 StrategyPort / TargetPortfolio interface
    without modifying underlying legacy trading execution.
    """

    def __init__(self, legacy_strategy: Any, account_id: str = "balanced"):
        self.legacy_strategy = legacy_strategy
        self.account_id = account_id

    def extract_signals_from_plan(
        self,
        plan: Dict[str, Any],
        asof_date: str,
        run_id: Optional[str] = None
    ) -> List[Signal]:
        """Convert legacy candidate/buy_orders into domain Signals."""
        run_id = run_id or str(uuid.uuid4())
        signals = []
        entries = plan.get("buy_orders", plan.get("target_entries", []))
        for rank, entry in enumerate(entries, 1):
            symbol = entry.get("symbol", "")
            if not symbol:
                continue
            sig = Signal(
                run_id=run_id,
                signal_asof=asof_date,
                symbol=symbol,
                rank=rank,
                momentum_score=float(entry.get("score", entry.get("momentum_score", 0.0))),
                sector_score=float(entry.get("sector_score", 0.0)),
                sector=str(entry.get("sector", entry.get("industry", "UNKNOWN"))),
                features={
                    "price": float(entry.get("price", 0.0)),
                    "atr": float(entry.get("atr", 0.0)),
                    "shares": int(entry.get("shares", 0)),
                },
                reasons=[f"legacy_plan_entry_rank_{rank}"],
                rule_version="v2_adapter"
            )
            signals.append(sig)
        return signals

    def build_target_portfolio(
        self,
        signals: List[Signal],
        account: AccountView,
        regime: MarketRegime,
        policy: Dict[str, Any],
        target_date: str
    ) -> TargetPortfolio:
        """Constructs an immutable TargetPortfolio conforming to regime limits."""
        params = policy.get("parameters", policy)
        max_pos = int(params.get("max_positions", 3))
        max_stock_wt = float(params.get("max_stock_weight", 0.15))
        regime_caps = {
            "RISK_OFF": float(params.get("regime_riskoff_weight", 0.30)),
            "NEUTRAL": float(params.get("regime_neutral_weight", 0.45)),
            "RISK_ON": float(params.get("regime_riskon_weight", 0.65)),
            "STRONG_RISK_ON": float(params.get("regime_strong_weight", 0.80)),
        }
        regime_name = regime.value if isinstance(regime, MarketRegime) else str(regime)
        regime_cap = regime_caps.get(regime_name, 0.30)

        feasibility = check_portfolio_feasibility(max_pos, max_stock_wt, regime_caps, strict=False)

        target_positions: Dict[str, TargetPosition] = {}
        allocated_weight = 0.0

        for sig in signals[:max_pos]:
            if allocated_weight + max_stock_wt > regime_cap + 1e-5:
                # Cap reached
                remaining = max(0.0, regime_cap - allocated_weight)
                if remaining >= 0.05:  # Minimum 5% allocation threshold
                    target_positions[sig.symbol] = TargetPosition(
                        symbol=sig.symbol,
                        target_weight=round(remaining, 4),
                        reason=f"regime_cap_partial_allocation_{sig.rank}"
                    )
                    allocated_weight += remaining
                break
            target_positions[sig.symbol] = TargetPosition(
                symbol=sig.symbol,
                target_weight=max_stock_wt,
                reason=f"momentum_rank_{sig.rank}"
            )
            allocated_weight += max_stock_wt

        allocated_weight = round(allocated_weight, 4)
        cash_weight = round(max(0.0, 1.0 - allocated_weight), 4)

        return TargetPortfolio(
            account_id=self.account_id,
            target_date=target_date,
            target_positions=target_positions,
            target_equity_weight=allocated_weight,
            cash_weight=cash_weight,
            regime=regime,
            regime_cap=regime_cap,
            constraints={
                "max_positions": max_pos,
                "max_stock_weight": max_stock_wt,
                "feasibility": feasibility
            },
            rejected_targets=[]
        )

    def generate_read_only_report(
        self,
        target: TargetPortfolio,
        account: AccountView
    ) -> Dict[str, Any]:
        """
        Outputs a non-mutating diagnosis report:
        desired_cap, feasible_cap, actual_exposure, limit_reasons.
        """
        feasibility = target.constraints.get("feasibility", {})
        return {
            "account_id": self.account_id,
            "target_date": target.target_date,
            "regime": target.regime.value if isinstance(target.regime, MarketRegime) else str(target.regime),
            "desired_cap": target.regime_cap,
            "feasible_cap": feasibility.get("feasible_cap", target.regime_cap),
            "actual_exposure": target.target_equity_weight,
            "cash_exposure": target.cash_weight,
            "allocated_position_count": len(target.target_positions),
            "is_mathematically_feasible": feasibility.get("is_feasible", True),
            "limit_reasons": feasibility.get("limit_reasons", [])
        }
