# src/astock/portfolio/config_validator.py
from typing import Any, Dict, List, Tuple
from ..domain.errors import InfeasibleConfigError


def check_portfolio_feasibility(
    max_positions: int,
    max_stock_weight: float,
    regime_caps: Dict[str, float],
    strict: bool = True
) -> Dict[str, Any]:
    """
    Validates whether max_positions * max_stock_weight can mathematically reach
    the highest regime cap target.

    Infeasible invariant:
    If max_positions * max_stock_weight < max(regime_target_weights),
    the strategy can NEVER reach the desired regime exposure regardless of stock signals.
    """
    feasible_cap = round(max_positions * max_stock_weight, 4)
    max_target = max(regime_caps.values()) if regime_caps else 0.0

    limit_reasons = []
    is_feasible = True

    if feasible_cap < max_target - 1e-5:
        is_feasible = False
        reason = (
            f"INFEASIBLE_CONFIG: max_positions ({max_positions}) * max_stock_weight ({max_stock_weight}) "
            f"= {feasible_cap:.2%}, which is less than maximum regime cap target {max_target:.2%}. "
            f"Strategy will be mathematically prevented from reaching desired risk exposure."
        )
        limit_reasons.append(reason)
        if strict:
            min_positions_needed = int(-(-max_target // max_stock_weight))
            raise InfeasibleConfigError(
                message=reason,
                desired_cap=max_target,
                feasible_cap=feasible_cap,
                reasons=[
                    reason,
                    f"Recommended fix: increase max_positions to at least {min_positions_needed} "
                    f"(e.g., 6 positions for 15% weight to reach 80% cap), or raise max_stock_weight."
                ]
            )

    return {
        "is_feasible": is_feasible,
        "feasible_cap": feasible_cap,
        "desired_cap": max_target,
        "limit_reasons": limit_reasons
    }


def validate_strategy_config(config: Dict[str, Any], strict: bool = True) -> Dict[str, Any]:
    """Full validation of strategy configuration parameters and risk bounds."""
    params = config.get("parameters", config)
    max_pos = int(params.get("max_positions", 3))
    max_stock = float(params.get("max_stock_weight", 0.15))
    max_sector = float(params.get("max_sector_weight", 0.30))

    if max_stock <= 0 or max_stock > 1.0:
        raise InfeasibleConfigError("max_stock_weight must be between 0 and 1.0")
    if max_sector < max_stock:
        raise InfeasibleConfigError("max_sector_weight cannot be less than max_stock_weight")

    # Regime caps
    regime_caps = {
        "RISK_OFF": float(params.get("regime_riskoff_weight", params.get("regime_weight_riskoff", 0.30))),
        "NEUTRAL": float(params.get("regime_neutral_weight", 0.45)),
        "RISK_ON": float(params.get("regime_riskon_weight", 0.65)),
        "STRONG_RISK_ON": float(params.get("regime_strong_weight", 0.80)),
    }

    report = check_portfolio_feasibility(max_pos, max_stock, regime_caps, strict=strict)
    
    validated = dict(config)
    validated["feasibility_report"] = report
    return validated
