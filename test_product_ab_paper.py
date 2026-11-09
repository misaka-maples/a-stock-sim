import pytest
import datetime as dt
from pathlib import Path
import tempfile
import copy
import json

from sim_trader import SimAccount, create_strategy, STRATEGY_REGISTRY
from product_ab_strategy import ProductABHybridStrategy, FROZEN_AB_WEIGHTS
from strategy_rules import MomentumConfig
import server
from fastapi.testclient import TestClient


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
    assert "shadow_ledger_summary" in status


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


def test_order_merging_and_dual_constraints(temp_account):
    """验证当 A 与 B 选中相同/不同股票时的双向订单合并与组合单票(35%)及行业(35%)约束"""
    industry_map = {
        "sh600519": "白酒",
        "sz000858": "白酒",
        "sh601318": "非银金融",
        "sz002475": "消费电子"
    }
    strategy = create_strategy(
        "Product_AB",
        account=temp_account,
        watchlist=["sh600519", "sz000858", "sh601318", "sz002475"],
        industry_map=industry_map
    )

    # 模拟最新候选信号 (包含 A 票与 B 票)
    strategy.latest_candidates = [
        {"symbol": "sh600519", "score": 90.0, "atr_20": 20.0, "current": 1600.0, "raw_signal_price": 1600.0, "adjustment_factor": 1.0},
        {"symbol": "sz000858", "score": 88.0, "atr_20": 3.0, "current": 150.0, "raw_signal_price": 150.0, "adjustment_factor": 1.0},
        {"symbol": "sh601318", "score": 85.0, "atr_20": 1.0, "current": 50.0, "raw_signal_price": 50.0, "adjustment_factor": 1.0},
        {"symbol": "sz002475", "score": 95.0, "atr_20": 1.2, "current": 35.0, "raw_signal_price": 35.0, "adjustment_factor": 1.0},
    ]

    # Mock get_current_regime_and_weights 为 STRONG_RISK_ON (A 50% / B 50%)
    strategy.get_current_regime_and_weights = lambda as_of: ("STRONG_RISK_ON", 0.50, 0.50)

    # 执行 plan
    strategy._plan("2026-11-06", "2026-11-09")
    pending = strategy.state.get("pending_entries", {})

    # 1. 验证没有重复订单 (每个 symbol 最多一个 entry)
    assert len(pending) == len(set(pending.keys()))

    # 2. 验证单票上限不超过 35%
    for sym, order in pending.items():
        assert order["target_weight"] <= 0.35 + 1e-4, f"{sym} weight {order['target_weight']} exceeds 35%"

    # 3. 验证白酒行业上限不超过 35%
    liquor_weight = sum(order["target_weight"] for sym, order in pending.items() if industry_map.get(sym) == "白酒")
    assert liquor_weight <= 0.35 + 1e-4, f"Liquor total weight {liquor_weight} exceeds 35%"


def test_shadow_ledger_multi_day_step_forward(temp_account):
    """验证影子账本从 T_0 到 T_1 的多日净值递推、合成与跟踪误差计算"""
    strategy = create_strategy(
        "Product_AB",
        account=temp_account,
        watchlist=["sh600519", "sz002475"]
    )

    # 注入模拟历史 K 线
    strategy.history_cache = {
        "sh600519": [
            {"date": "2026-11-05", "close": 1500.0},
            {"date": "2026-11-06", "close": 1530.0}  # +2.0%
        ],
        "sz002475": [
            {"date": "2026-11-05", "close": 30.0},
            {"date": "2026-11-06", "close": 31.5}   # +5.0%
        ]
    }

    # Day 1: 2026-11-05 (NEUTRAL: A 100% / B 0%)
    holdings_a_day1 = {"sh600519": 0.30}  # 30% 茅台, 70% 现金
    holdings_b_day1 = {"sz002475": 0.30}  # 30% 立讯, 70% 现金
    strategy._update_shadow_ledger("2026-11-05", "NEUTRAL", 1.00, 0.00, holdings_a_day1, holdings_b_day1)

    ledger1 = strategy.get_shadow_ledger()
    assert ledger1["nav_a"] == 1.0
    assert ledger1["nav_b"] == 1.0
    assert ledger1["nav_ab_theoretical"] == 1.0
    assert ledger1["nav_actual"] == 1.0
    assert ledger1["tracking_error_pct"] == 0.0

    # Day 2: 2026-11-06 (RISK_ON: A 20% / B 80%)
    # 理论收益测算:
    # A 理论持仓: 0.30 * 2.0% + 0.70 * (2.5% / 252) ~= 0.0060 + 0.0000694 = 0.0060694 (+0.607%)
    # B 理论持仓: 0.30 * 5.0% + 0.70 * (2.5% / 252) ~= 0.0150 + 0.0000694 = 0.0150694 (+1.507%)
    # Day 1 到 Day 2 的 Blend 权重是 Day 1 设置的 (A 100% / B 0%):
    # ret_ab = 1.00 * ret_a + 0.00 * ret_b = ret_a ~= +0.607%
    holdings_a_day2 = {"sh600519": 0.30}
    holdings_b_day2 = {"sz002475": 0.35}

    # 模拟实际账户略有盈利 (例如撮合成交使得实际资产为 100,500 元, nav_actual = 1.0050)
    temp_account.cash = 100500.0
    strategy._update_shadow_ledger("2026-11-06", "RISK_ON", 0.20, 0.80, holdings_a_day2, holdings_b_day2)

    ledger2 = strategy.get_shadow_ledger()
    assert ledger2["nav_a"] > 1.0050  # 约 1.0061
    assert ledger2["nav_b"] > 1.0140  # 约 1.0151
    assert ledger2["nav_ab_theoretical"] > 1.0050
    assert ledger2["nav_actual"] == 1.0050
    assert len(ledger2["history"]) == 2
    assert ledger2["history"][-1]["date"] == "2026-11-06"
    assert ledger2["history"][-1]["regime"] == "RISK_ON"


def test_server_shadow_ledger_api_endpoint(temp_account):
    """测试 Server 端暴露的 /api/strategy/shadow_ledger 接口"""
    manager = server.auth_manager
    manager.initialize()
    with TestClient(server.app) as client:
        # 激活 Product_AB 策略并挂载到 server 上下文
        strategy = create_strategy("Product_AB", account=temp_account, watchlist=["sh600519"])
        server.ctx.strategy = strategy
        server.ctx.account = temp_account

        # 认证登录
        if manager.credentials_path and manager.credentials_path.exists():
            password = json.loads(manager.credentials_path.read_text())["password"]
            login_res = client.post("/api/auth/login", json={"password": password})
            assert login_res.status_code == 200

        response = client.get("/api/strategy/shadow_ledger")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        ledger = data["data"]
        assert "nav_a" in ledger
        assert "nav_b" in ledger
        assert "nav_ab_theoretical" in ledger
        assert "nav_actual" in ledger
        assert "tracking_error_pct" in ledger
        assert "history" in ledger
