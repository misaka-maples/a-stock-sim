import pytest
from product_ab_engine import ProductABConfig, ProductABEngine
from strategy_rules import MomentumConfig

def test_product_ab_config_defaults():
    cfg = ProductABConfig()
    assert cfg.initial_cash == 100000.0
    assert "RISK_OFF" in cfg.regime_weights
    assert cfg.regime_weights["RISK_OFF"] == (1.0, 0.0)
    assert cfg.regime_weights["NEUTRAL"] == (1.0, 0.0)
    assert cfg.regime_weights["RISK_ON"] == (0.20, 0.80)
    assert cfg.regime_weights["STRONG_RISK_ON"] == (0.50, 0.50)

def test_product_ab_engine_instantiation():
    engine = ProductABEngine(start_date="2024-01-01", end_date="2024-03-31")
    assert engine.start_date == "2024-01-01"
    assert engine.end_date == "2024-03-31"
    assert engine.config_a.dynamic_universe_enabled is False
    assert engine.config_b.dynamic_universe_enabled is True
