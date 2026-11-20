"""Load the existing dual-account execution rules without touching account state.

Strategy YAML is authoritative for each account. Saved primary parameters may
supply recognized Balanced risk/execution overrides only. Missing YAML fields
have no general fallback: the explicit compatibility defaults below come
from the already deployed v2 strategy and are identified in the returned audit
metadata. Missing configuration files or invalid policies raise immediately.
"""
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


POLICY_PARAMETERS = (
    "rebalance_every", "refill_vacancies", "min_holding_days", "cooldown_days",
    "rank_buffer", "participation_rate", "stop_loss_pct", "stop_loss_mode",
    "risk_per_trade", "trailing_stop_enabled", "trailing_stop_mode",
    "trailing_stop_activation_pct", "atr_trailing_multiplier",
    "profit_floor_enabled", "profit_floor_pct", "realtime_ma20_exit",
    "ma20_exit_consecutive_days", "ma20_exit_buffer_pct",
    "max_entry_gap_down_pct", "max_entry_gap_up_pct", "max_entry_intraday_drop_pct",
    "entry_require_ma20", "protect_limit_up",
)

# These compatibility values are existing rules, not a new risk calibration.
_LEGACY_DEFAULTS = {
    "trailing_stop_activation_pct": (20.0, "product_ab_v2_strategies.py:both_strategy_initializers"),
    "ma20_exit_buffer_pct": (1.0, "product_ab_v2_strategies.py:MA20_exit_price_times_0.99"),
    "profit_floor_pct": (3.0, "strategy_rules.py:MomentumConfig.profit_floor_pct"),
    "max_entry_gap_down_pct": (3.0, "strategy_rules.py:MomentumConfig.max_entry_gap_down_pct"),
    "max_entry_gap_up_pct": (5.0, "strategy_rules.py:MomentumConfig.max_entry_gap_up_pct"),
    "max_entry_intraday_drop_pct": (3.0, "strategy_rules.py:MomentumConfig.max_entry_intraday_drop_pct"),
    "entry_require_ma20": (True, "strategy_rules.py:MomentumConfig.entry_require_ma20"),
    "protect_limit_up": (True, "strategy_rules.py:MomentumConfig.protect_limit_up"),
}
_BOOL_KEYS = {"refill_vacancies", "trailing_stop_enabled", "profit_floor_enabled", "realtime_ma20_exit",
              "entry_require_ma20", "protect_limit_up"}
_INT_BOUNDS = {
    "rebalance_every": (1, 252), "min_holding_days": (0, 252),
    "cooldown_days": (0, 252), "rank_buffer": (0, 252),
    "ma20_exit_consecutive_days": (1, 20),
}
_NUMBER_BOUNDS = {
    "stop_loss_pct": (-50.0, 0.0), "risk_per_trade": (0.0, .1),
    "participation_rate": (0.0, 1.0), "trailing_stop_activation_pct": (0.0, 100.0),
    "atr_trailing_multiplier": (0.0, 20.0), "profit_floor_pct": (0.0, 100.0),
    "ma20_exit_buffer_pct": (0.0, 20.0),
    "max_entry_gap_down_pct": (0.0, 20.0), "max_entry_gap_up_pct": (0.0, 20.0),
    "max_entry_intraday_drop_pct": (0.0, 20.0),
    "max_drawdown_warn_pct": (0.0, 100.0), "max_drawdown_freeze_pct": (0.0, 100.0),
}


def _read_yaml(path: Path) -> Dict[str, Any]:
    # read_text intentionally raises FileNotFoundError rather than falling back.
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid execution policy YAML: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Execution policy must be a mapping: {path}")
    return data


def _validate(policy: Dict[str, Any], account: str) -> None:
    for key in _BOOL_KEYS:
        if type(policy[key]) is not bool:
            raise ValueError(f"{account}.{key} must be boolean")
    for key, (low, high) in _INT_BOUNDS.items():
        value = policy[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{account}.{key} must be an integer in [{low}, {high}]")
    for key, (low, high) in _NUMBER_BOUNDS.items():
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{account}.{key} must be a finite number")
        if not low <= value <= high:
            raise ValueError(f"{account}.{key} must be in [{low}, {high}]")
        policy[key] = float(value)
    for key in ("participation_rate", "atr_trailing_multiplier", "max_drawdown_freeze_pct"):
        if policy[key] <= 0:
            raise ValueError(f"{account}.{key} must be positive")
    if policy["stop_loss_pct"] >= 0:
        raise ValueError(f"{account}.stop_loss_pct must be negative")
    if policy["max_drawdown_warn_pct"] >= policy["max_drawdown_freeze_pct"]:
        raise ValueError(f"{account} drawdown warning must be below freeze threshold")
    if policy["trailing_stop_mode"] != "atr":
        raise ValueError(f"{account}.trailing_stop_mode must retain the supported ATR policy")
    if policy["stop_loss_mode"] not in ("intraday", "daily_close"):
        raise ValueError(f"{account}.stop_loss_mode is unsupported")


def load_execution_policies(base_dir, primary_parameters: Optional[Mapping] = None) -> Dict[str, Dict[str, Any]]:
    """Return validated Balanced/Aggressive policies and per-field source metadata.

    Does not read or write ledgers, broker state, or data caches. The account
    profile paths are resolved from accounts.yaml and must remain inside base_dir.
    Only POLICY_PARAMETERS in primary_parameters can override Balanced; Aggressive
    and both account drawdown thresholds retain their own configuration sources.
    """
    root = Path(base_dir).expanduser().resolve()
    if primary_parameters is not None and not isinstance(primary_parameters, Mapping):
        raise ValueError("primary_parameters must be a mapping")
    primary = primary_parameters or {}
    accounts_path = root / "configs" / "accounts.yaml"
    accounts = _read_yaml(accounts_path).get("accounts")
    if not isinstance(accounts, dict):
        raise ValueError("accounts.yaml must contain the dual accounts mapping")
    policies = {}
    for account in ("balanced", "aggressive"):
        config = accounts.get(account)
        if not isinstance(config, dict) or config.get("account_id") != account:
            raise ValueError(f"Missing or mismatched {account} account definition")
        path_value = config.get("strategy_config_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError(f"Missing {account} strategy_config_path")
        strategy_path = (root / path_value).resolve()
        if not strategy_path.is_relative_to(root):
            raise ValueError(f"{account} strategy path is outside the project")
        strategy = _read_yaml(strategy_path)
        if not strategy.get("strategy_id") or strategy["strategy_id"] != config.get("strategy_id"):
            raise ValueError(f"{account} strategy identity does not match accounts.yaml")
        parameters = strategy.get("parameters")
        limits = config.get("risk_limits")
        if not isinstance(parameters, dict) or not isinstance(limits, dict):
            raise ValueError(f"Missing {account} parameters or account risk_limits")
        policy = {"account_id": account}
        fields, defaults, overridden = {}, {}, []
        for key in POLICY_PARAMETERS:
            if key in parameters:
                value = parameters[key]
                source = f"{strategy_path}#parameters.{key}"
            elif key in _LEGACY_DEFAULTS:
                value, reference = _LEGACY_DEFAULTS[key]
                source = f"legacy_rule:{reference}"
                defaults[key] = {"value": value, "reference": reference}
            else:
                raise ValueError(f"Missing {account} execution parameter: {key}")
            if account == "balanced" and key in primary:
                value = primary[key]
                source = f"primary_parameters.{key}"
                overridden.append(key)
            policy[key], fields[key] = value, source
        for key in ("max_drawdown_warn_pct", "max_drawdown_freeze_pct"):
            if key not in limits:
                raise ValueError(f"Missing {account} risk limit: {key}")
            policy[key] = limits[key]
            fields[key] = f"{accounts_path}#accounts.{account}.risk_limits.{key}"
        _validate(policy, account)
        policy["sources"] = {
            "accounts_config": str(accounts_path), "strategy_config": str(strategy_path),
            "strategy_id": strategy["strategy_id"], "fields": fields,
            "legacy_defaults": defaults, "primary_overrides": overridden,
        }
        policies[account] = policy
    return policies
