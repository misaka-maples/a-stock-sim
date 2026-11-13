# tests/test_domain_contracts.py
import pytest
from dataclasses import FrozenInstanceError

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from astock.domain.enums import (
    MarketRegime,
    OrderState,
    OrderSide,
    OrderType,
    DataQuality,
    AccountMode,
    ReasonCode
)
from astock.domain.models import (
    Bar,
    Quote,
    MarketSnapshot,
    Signal,
    TargetPosition,
    TargetPortfolio,
    OrderIntent,
    ExecutionReport,
    AccountView
)


def test_enums_completeness_and_values():
    assert MarketRegime.RISK_OFF == "RISK_OFF"
    assert MarketRegime.UNKNOWN == "UNKNOWN"
    assert OrderState.PROPOSED == "PROPOSED"
    assert OrderState.FILLED == "FILLED"
    assert OrderSide.BUY == "BUY"
    assert OrderSide.SELL == "SELL"
    assert DataQuality.VALID == "VALID"
    assert AccountMode.PAPER == "PAPER"


def test_bar_immutability():
    bar = Bar(
        date="2026-11-12",
        open=10.0,
        high=10.5,
        low=9.8,
        close=10.2,
        volume=1000.0,
        amount=1020000.0,
        raw_open=20.0,
        raw_high=21.0,
        raw_low=19.6,
        raw_close=20.4,
        adjustment_factor=0.5,
        source="akshare"
    )
    assert bar.close == 10.2
    with pytest.raises(FrozenInstanceError):
        bar.close = 11.0


def test_signal_and_target_portfolio_immutability():
    sig = Signal(
        run_id="run-1",
        signal_asof="2026-11-12",
        symbol="sh600000",
        rank=1,
        momentum_score=0.85,
        sector_score=0.90,
        sector="银行"
    )
    assert sig.symbol == "sh600000"
    with pytest.raises(FrozenInstanceError):
        sig.rank = 2

    pos = TargetPosition(symbol="sh600000", target_weight=0.15)
    tp = TargetPortfolio(
        account_id="balanced",
        target_date="2026-11-13",
        target_positions={"sh600000": pos},
        target_equity_weight=0.15,
        cash_weight=0.85,
        regime=MarketRegime.RISK_ON,
        regime_cap=0.65
    )
    assert tp.account_id == "balanced"
    with pytest.raises(FrozenInstanceError):
        tp.cash_weight = 0.50


def test_order_intent_and_execution_report():
    order = OrderIntent(
        intent_id="intent-1",
        client_order_id="ord-20261113-001",
        account_id="balanced",
        strategy_run_id="run-1",
        symbol="sh600000",
        side=OrderSide.BUY,
        shares=1000,
        limit_price=10.2
    )
    assert order.side == OrderSide.BUY
    assert order.status == OrderState.PROPOSED

    report = ExecutionReport(
        broker_order_id="brk-001",
        client_order_id="ord-20261113-001",
        account_id="balanced",
        symbol="sh600000",
        side=OrderSide.BUY,
        shares=1000,
        price=10.2,
        fee=5.0,
        status=OrderState.FILLED,
        event_time="2026-11-13T09:35:00+08:00",
        execution_id="exec-001"
    )
    assert report.price == 10.2
    with pytest.raises(FrozenInstanceError):
        report.fee = 0.0
