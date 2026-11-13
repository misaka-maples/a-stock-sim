# tests/test_config_feasibility.py
import pytest
import yaml
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from astock.domain.errors import InfeasibleConfigError
from astock.portfolio.config_validator import (
    check_portfolio_feasibility,
    validate_strategy_config
)


def test_infeasible_config_rejection():
    # Legacy Balanced: 3 positions * 15% = 45% < 80%
    regime_caps = {"RISK_OFF": 0.30, "NEUTRAL": 0.45, "RISK_ON": 0.65, "STRONG_RISK_ON": 0.80}
    with pytest.raises(InfeasibleConfigError) as exc_info:
        check_portfolio_feasibility(
            max_positions=3,
            max_stock_weight=0.15,
            regime_caps=regime_caps,
            strict=True
        )
    assert exc_info.value.desired_cap == 0.80
    assert exc_info.value.feasible_cap == 0.45
    assert "INFEASIBLE_CONFIG" in str(exc_info.value)


def test_infeasible_config_non_strict_report():
    regime_caps = {"RISK_OFF": 0.30, "NEUTRAL": 0.45, "RISK_ON": 0.65, "STRONG_RISK_ON": 0.80}
    res = check_portfolio_feasibility(
        max_positions=3,
        max_stock_weight=0.15,
        regime_caps=regime_caps,
        strict=False
    )
    assert res["is_feasible"] is False
    assert res["feasible_cap"] == 0.45
    assert res["desired_cap"] == 0.80
    assert len(res["limit_reasons"]) > 0


def test_feasible_balanced_and_aggressive_configurations():
    # V3 Balanced: 6 positions * 15% = 90% >= 80%
    res_b = check_portfolio_feasibility(
        max_positions=6,
        max_stock_weight=0.15,
        regime_caps={"RISK_OFF": 0.30, "NEUTRAL": 0.45, "RISK_ON": 0.65, "STRONG_RISK_ON": 0.80},
        strict=True
    )
    assert res_b["is_feasible"] is True
    assert res_b["feasible_cap"] == 0.90

    # V3 Aggressive: 5 positions * 25% = 125% >= 100%
    res_a = check_portfolio_feasibility(
        max_positions=5,
        max_stock_weight=0.25,
        regime_caps={"RISK_OFF": 0.50, "NEUTRAL": 0.70, "RISK_ON": 0.90, "STRONG_RISK_ON": 1.00},
        strict=True
    )
    assert res_a["is_feasible"] is True
    assert res_a["feasible_cap"] == 1.25


def test_validate_strategy_yaml_files():
    base_dir = Path(__file__).resolve().parent.parent
    b_path = base_dir / "configs/strategies/balanced.yaml"
    a_path = base_dir / "configs/strategies/aggressive.yaml"

    assert b_path.exists(), f"Missing {b_path}"
    assert a_path.exists(), f"Missing {a_path}"

    b_cfg = yaml.safe_load(b_path.read_text())
    a_cfg = yaml.safe_load(a_path.read_text())

    val_b = validate_strategy_config(b_cfg, strict=True)
    assert val_b["feasibility_report"]["is_feasible"] is True

    val_a = validate_strategy_config(a_cfg, strict=True)
    assert val_a["feasibility_report"]["is_feasible"] is True
