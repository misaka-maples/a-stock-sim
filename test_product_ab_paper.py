import pytest
import datetime as dt
from pathlib import Path
import tempfile

from sim_trader import SimAccount, create_strategy, STRATEGY_REGISTRY
from product_ab_strategy import ProductABHybridStrategy, FROZEN_AB_WEIGHTS
from strategy_rules import MomentumConfig


@pytest.fixture
def temp_account(tmp_path):
    acc_path = tmp_path / "sim_account.json"
    account = SimAccount(
        initial_cash=100000.0,
        strict_t1=True,
        save_path=str(acc_path),
        ledger_only=True
    )
    return account


def test_product_ab_registration():
    assert "Product_AB" in STRATEGY_REGISTRY
    assert STRATEGY_REGISTRY["Product_AB"] is ProductABHybridStrategy


def test_product_ab_instantiation_and_shadow_ledger_defaults(temp_account):
    strategy = create_strategy("Product_AB", account=temp_account, watchlist=["sh600519", "sz000001"])
    assert isinstance(strategy, ProductABHybridStrategy)
    assert strategy.name == "Product_AB"
    assert "Base Engine + Turbo Engine" in strategy.display_name

    # Check shadow ledger initialization
    ledger = strategy.get_shadow_ledger()
    assert ledger["nav_a"] == 1.0
    assert ledger["nav_b"] == 1.0
    assert ledger["nav_ab_theoretical"] == 1.0
    assert ledger["nav_actual"] == 1.0
    assert ledger["tracking_error_pct"] == 0.0
    assert ledger["total_equity"] == 100000.0


def test_frozen_ab_weights_mapping():
    assert FROZEN_AB_WEIGHTS["RISK_OFF"] == (1.00, 0.00)
    assert FROZEN_AB_WEIGHTS["NEUTRAL"] == (1.00, 0.00)
    assert FROZEN_AB_WEIGHTS["RISK_ON"] == (0.20, 0.80)
    assert FROZEN_AB_WEIGHTS["STRONG_RISK_ON"] == (0.50, 0.50)


def test_allocation_status_hybrid_fields(temp_account):
    strategy = create_strategy("Product_AB", account=temp_account, watchlist=["sh600519"])
    status = strategy.allocation_status()
    assert status.get("hybrid_mode") is True
    assert "market_regime" in status
    assert "weight_product_a" in status
    assert "weight_product_b" in status
    assert "tracking_error_pct" in status
    assert "regime_description" in status


def test_cash_overlay_interest_accrual(temp_account):
    strategy = create_strategy("Product_AB", account=temp_account, watchlist=["sh600519"])
    initial_cash = temp_account.cash  # 100,000.0

    # Simulate after-market tick on a trading day (e.g., 15:30 on Monday 2026-11-09)
    after_market_time = dt.datetime(2026, 11, 9, 15, 30, 0)
    strategy.on_tick(quotes={}, now=after_market_time)

    # 100,000 * 0.025 / 252 ~= 9.92 yuan
    expected_interest = round(100000.0 * (2.5 / 100.0 / 252.0), 2)
    assert temp_account.cash == initial_cash + expected_interest
    assert strategy.state.get("last_cash_yield_date") == "2026-11-09"

    # Second tick on the same day should not double count interest
    strategy.on_tick(quotes={}, now=after_market_time)
    assert temp_account.cash == initial_cash + expected_interest
