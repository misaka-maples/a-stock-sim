# tests/test_legacy_strategy_adapter.py
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from astock.domain.enums import MarketRegime, AccountMode
from astock.domain.models import AccountView
from astock.strategies.legacy_adapter import LegacyStrategyAdapter


def test_legacy_plan_to_signals_and_target_portfolio():
    mock_legacy_plan = {
        "as_of": "2026-11-12",
        "market_regime": "RISK_ON",
        "buy_orders": [
            {"symbol": "sh600519", "score": 2.5, "sector_score": 1.2, "sector": "白酒", "price": 1500.0, "atr": 25.0, "shares": 100},
            {"symbol": "sh600036", "score": 2.1, "sector_score": 1.0, "sector": "银行", "price": 35.0, "atr": 0.8, "shares": 500},
            {"symbol": "sz000001", "score": 1.8, "sector_score": 1.0, "sector": "银行", "price": 10.0, "atr": 0.3, "shares": 1500},
            {"symbol": "sh601318", "score": 1.5, "sector_score": 0.8, "sector": "保险", "price": 45.0, "atr": 1.1, "shares": 400},
        ]
    }

    adapter = LegacyStrategyAdapter(legacy_strategy=None, account_id="balanced")
    signals = adapter.extract_signals_from_plan(mock_legacy_plan, asof_date="2026-11-12")
    assert len(signals) == 4
    assert signals[0].symbol == "sh600519"
    assert signals[0].rank == 1

    account = AccountView(
        account_id="balanced",
        mode=AccountMode.PAPER,
        cash=85000.0,
        positions={},
        total_equity=100000.0,
        peak_equity=100000.0,
        max_drawdown_pct=0.0,
        current_drawdown_pct=0.0
    )

    policy = {
        "parameters": {
            "max_positions": 3,
            "max_stock_weight": 0.15,
            "regime_riskon_weight": 0.65
        }
    }

    target = adapter.build_target_portfolio(
        signals=signals,
        account=account,
        regime=MarketRegime.RISK_ON,
        policy=policy,
        target_date="2026-11-13"
    )

    assert target.account_id == "balanced"
    # max_positions is 3, so only top 3 are allocated
    assert len(target.target_positions) == 3
    assert "sh600519" in target.target_positions
    assert "sh600036" in target.target_positions
    assert "sz000001" in target.target_positions
    assert target.target_equity_weight == round(3 * 0.15, 4)

    # Read-only report
    report = adapter.generate_read_only_report(target, account)
    assert report["account_id"] == "balanced"
    assert report["desired_cap"] == 0.65
    assert report["feasible_cap"] == 0.45
    assert report["actual_exposure"] == 0.45
    assert report["is_mathematically_feasible"] is False
    assert len(report["limit_reasons"]) > 0


def test_dual_accounts_isolation_in_adapter():
    mock_plan = {
        "buy_orders": [
            {"symbol": "sh600000", "score": 2.0, "sector": "银行"},
            {"symbol": "sh600036", "score": 1.9, "sector": "银行"}
        ]
    }
    adapter_bal = LegacyStrategyAdapter(legacy_strategy=None, account_id="balanced")
    adapter_agg = LegacyStrategyAdapter(legacy_strategy=None, account_id="aggressive")

    signals_bal = adapter_bal.extract_signals_from_plan(mock_plan, asof_date="2026-11-12")
    signals_agg = adapter_agg.extract_signals_from_plan(mock_plan, asof_date="2026-11-12")

    acc_bal = AccountView("balanced", AccountMode.PAPER, 90000.0, {}, 100000.0, 100000.0, 0.0, 0.0)
    acc_agg = AccountView("aggressive", AccountMode.PAPER, 100000.0, {}, 100000.0, 100000.0, 0.0, 0.0)

    policy_bal = {"parameters": {"max_positions": 6, "max_stock_weight": 0.15, "regime_riskon_weight": 0.65}}
    policy_agg = {"parameters": {"max_positions": 5, "max_stock_weight": 0.25, "regime_riskon_weight": 0.90}}

    tp_bal = adapter_bal.build_target_portfolio(signals_bal, acc_bal, MarketRegime.RISK_ON, policy_bal, "2026-11-13")
    tp_agg = adapter_agg.build_target_portfolio(signals_agg, acc_agg, MarketRegime.RISK_ON, policy_agg, "2026-11-13")

    # Balanced weight per stock is 15%, Aggressive is 25%
    assert tp_bal.target_positions["sh600000"].target_weight == 0.15
    assert tp_agg.target_positions["sh600000"].target_weight == 0.25
    assert tp_bal.target_equity_weight == 0.30
    assert tp_agg.target_equity_weight == 0.50
    # Independent targets with zero shared state
    assert tp_bal.account_id != tp_agg.account_id
