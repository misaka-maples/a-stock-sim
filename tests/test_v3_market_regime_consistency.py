# tests/test_v3_market_regime_consistency.py
# -*- coding: utf-8 -*-
"""
A-Stock V3.0 Market Regime Consistency & Data Invalidation Verification Suite
Strictly verifies Section III requirements across 8 fixed scenarios:
1. Breadth 20%  -> RISK_ON (Bal cap 65%, Agg cap 90%)
2. Breadth 45%  -> RISK_ON (Bal cap 65%, Agg cap 90%)
3. Breadth 55%  -> RISK_ON (Bal cap 65%, Agg cap 90%)
4. Breadth 65%  -> STRONG_RISK_ON (Bal cap 80%, Agg cap 100%)
5. Missing Breadth -> Data Quality Error, RISK_OFF, Block New Buys
6. Coverage < 60%  -> Data Quality Error, RISK_OFF, Block New Buys
7. Benchmark < 120 bars -> Data Quality Error, RISK_OFF, Block New Buys
8. Historical as_of earlier than dataset end date -> Zero lookahead truncation
"""

import os
import tempfile
import datetime
from pathlib import Path
import pytest

from src.astock.domain.enums import MarketRegime, DataQuality, REGIME_STRONG_BREADTH_MIN
from src.astock.domain.models import Bar
from src.astock.data.models import DailyBar, StockInfo
from src.astock.data.storage import LocalStorageManager
from src.astock.data.service import AStockDataService
from src.astock.trade.risk.market import MarketRiskRule
from src.astock.trade.risk.manager import RiskManager
from src.astock.trade.risk.portfolio import PortfolioRiskRule, BALANCED_REGIME_CAPS, AGGRESSIVE_REGIME_CAPS
from src.astock.trade.runtime import TradingRuntime


def make_bullish_benchmark(as_of: str, count: int = 150) -> list:
    """Produces HS300 bars with c > ma20 > ma60 > ma120 ending on as_of."""
    as_of_dt = datetime.date.fromisoformat(as_of)
    bars = []
    base_px = 3500.0
    for i in range(count):
        d_str = (as_of_dt - datetime.timedelta(days=count - 1 - i)).isoformat()
        bars.append({"date": d_str, "close": base_px + i * 5.0})
    return bars


def setup_mock_storage_and_service(tmp_dir: str, as_of: str, count: int = 150, num_stocks: int = 20, breadth_ratio: float = 0.65):
    """Sets up LocalStorageManager and AStockDataService with known breadth."""
    storage = LocalStorageManager(base_dir=tmp_dir)
    bench_bars = make_bullish_benchmark(as_of, count)
    domain_bench_bars = [
        DailyBar(
            symbol="sh000300", trade_date=b["date"],
            open=b["close"] - 10, high=b["close"] + 10, low=b["close"] - 15, close=b["close"],
            volume=50000.0, amount=500000000.0, source="test", adjust_type="none",
            updated_at="2026-11-16T09:00:00", is_final=True
        ) for b in bench_bars
    ]
    storage.save_daily_bars("sh000300", domain_bench_bars, adjust_type="none")

    stock_infos = [
        StockInfo(symbol=f"sh60{s:04d}", name=f"Stock{s}", status="L", list_date="2020-01-01")
        for s in range(num_stocks)
    ]
    storage.save_stocks(stock_infos)

    above_count = int(round(num_stocks * breadth_ratio))
    for s_idx in range(num_stocks):
        sym = f"sh60{s_idx:04d}"
        s_bars = []
        is_above = (s_idx < above_count)
        base = 10.0 if not is_above else 15.0
        for i in range(130):
            d_str = (datetime.date.fromisoformat(as_of) - datetime.timedelta(days=130 - 1 - i)).isoformat()
            px = base + (0.05 * i if is_above else -0.01 * i)
            s_bars.append(DailyBar(
                symbol=sym, trade_date=d_str,
                open=px, high=px + 0.5, low=px - 0.5, close=px,
                volume=1000.0, amount=100000.0, source="test", adjust_type="qfq",
                updated_at="2026-11-16T09:00:00", is_final=True
            ))
        storage.save_daily_bars(sym, s_bars, adjust_type="qfq")

    service = AStockDataService(storage=storage, offline_mode=True)
    return storage, service, bench_bars


class TestV3MarketRegimeConsistency:
    """Verifies all 8 fixed-market scenarios for consistency across layers."""

    @pytest.mark.parametrize("breadth_val, expected_regime, expected_bal_cap, expected_agg_cap", [
        (0.20, MarketRegime.RISK_ON, 0.65, 0.90),
        (0.45, MarketRegime.RISK_ON, 0.65, 0.90),
        (0.55, MarketRegime.RISK_ON, 0.65, 0.90),
        (0.65, MarketRegime.STRONG_RISK_ON, 0.80, 1.00),
    ])
    def test_scenarios_1_to_4_valid_breadth_levels(self, breadth_val, expected_regime, expected_bal_cap, expected_agg_cap):
        """Scenarios 1-4: Valid market breadth at 20%, 45%, 55%, 65%."""
        as_of = "2026-05-11"
        bench_bars = make_bullish_benchmark(as_of, 150)

        # 1. MarketRiskRule direct check
        rule = MarketRiskRule(breadth_min=REGIME_STRONG_BREADTH_MIN)
        regime, breadth, audit = rule.determine_regime(bench_bars, as_of=as_of, market_breadth=breadth_val)
        assert regime == expected_regime, f"Expected {expected_regime} for breadth {breadth_val}, got {regime}"
        assert breadth == breadth_val
        assert audit.passed is True
        assert audit.severity.value != "BLOCK"

        # 2. RiskManager caps check
        pm_bal = PortfolioRiskRule(account_type="balanced")
        pm_agg = PortfolioRiskRule(account_type="aggressive")
        assert pm_bal.get_effective_regime_cap(regime, is_frozen=False) == expected_bal_cap
        assert pm_agg.get_effective_regime_cap(regime, is_frozen=False) == expected_agg_cap

        # 3. TradingRuntime step check
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "test.sqlite")
            rt = TradingRuntime(db_path=db_path, mode="ACTIVE", initial_capital=100000.0)
            quotes = {
                "sh600000": {"price": 10.0, "name": "浦发银行", "sector": "银行"},
                "sh600001": {"price": 12.0, "name": "中国石化", "sector": "石油"},
            }
            market_data = {
                "candidates": [
                    {"symbol": "sh600000", "name": "浦发银行", "score": 90.0, "sector": "银行", "price": 10.0, "ret20": 8.0, "atr": 0.5},
                    {"symbol": "sh600001", "name": "中国石化", "score": 88.0, "sector": "石油", "price": 12.0, "ret20": 7.0, "atr": 0.6},
                ],
                "csi300_bars": bench_bars,
                "market_breadth": breadth_val,
                # Native candidates still need completed, dated per-stock evidence.
                "stock_bars": {
                    symbol: [{"date": b["date"], "close": price,
                              "volume": 100000.0, "amount": price * 10_000_000,
                              "is_final": True, "is_st": False, "is_suspended": False}
                             for b in bench_bars[-130:]]
                    for symbol, price in [("sh600000", 10.0), ("sh600001", 12.0)]
                }
            }
            summary = rt.step(as_of, quotes, market_data)
            assert summary["regime"] == expected_regime.value
            assert summary["breadth"] == breadth_val
            assert summary["balanced"]["executed_fills"] >= 1
            assert summary["aggressive"]["executed_fills"] >= 1

    def test_scenario_5_missing_breadth_blocks_new_buys(self):
        """Scenario 5: Breadth missing on bullish golden cross -> Data quality error, RISK_OFF, blocks buys."""
        as_of = "2026-05-10"
        bench_bars = make_bullish_benchmark(as_of, 150)

        # 1. MarketRiskRule check
        rule = MarketRiskRule()
        regime, breadth, audit = rule.determine_regime(bench_bars, as_of=as_of, market_breadth=None)
        assert regime == MarketRegime.RISK_OFF
        assert breadth is None
        assert audit.passed is False
        assert audit.action_taken == "BLOCK_NEW_BUYS"
        assert audit.rule_name == "MARKET_BREADTH_INTEGRITY"

        # 2. TradingRuntime check: must freeze new buys (0 executed fills)
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "test.sqlite")
            rt = TradingRuntime(db_path=db_path, mode="ACTIVE", initial_capital=100000.0)
            quotes = {"sh600000": {"price": 10.0, "name": "浦发银行", "sector": "银行"}}
            market_data = {
                "candidates": [
                    {"symbol": "sh600000", "name": "浦发银行", "score": 90.0, "sector": "银行", "price": 10.0, "ret20": 8.0, "atr": 0.5},
                ],
                "csi300_bars": bench_bars,
                "market_breadth": None  # Missing!
            }
            summary = rt.step(as_of, quotes, market_data)
            assert summary["regime"] == MarketRegime.RISK_OFF.value
            assert summary["breadth"] is None
            assert summary["balanced"]["executed_fills"] == 0, "CRITICAL: New buys must be blocked when breadth missing!"
            assert summary["aggressive"]["executed_fills"] == 0, "CRITICAL: New buys must be blocked when breadth missing!"

    def test_scenario_6_insufficient_coverage_blocks_new_buys(self):
        """Scenario 6: Market coverage ratio < 60% (e.g. 40%) -> Data quality error, RISK_OFF, blocks buys."""
        as_of = "2026-05-10"
        bench_bars = make_bullish_benchmark(as_of, 150)

        # 1. MarketRiskRule check
        rule = MarketRiskRule()
        regime, breadth, audit = rule.determine_regime(
            bench_bars, as_of=as_of, market_breadth=0.65, coverage_ratio=0.40
        )
        assert regime == MarketRegime.RISK_OFF
        assert breadth is None
        assert audit.passed is False
        assert audit.action_taken == "BLOCK_NEW_BUYS"
        assert audit.rule_name == "MARKET_COVERAGE_INTEGRITY"

        # 2. TradingRuntime check
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "test.sqlite")
            rt = TradingRuntime(db_path=db_path, mode="ACTIVE", initial_capital=100000.0)
            quotes = {"sh600000": {"price": 10.0, "name": "浦发银行", "sector": "银行"}}
            market_data = {
                "candidates": [
                    {"symbol": "sh600000", "name": "浦发银行", "score": 90.0, "sector": "银行", "price": 10.0, "ret20": 8.0, "atr": 0.5},
                ],
                "csi300_bars": bench_bars,
                "coverage_ratio": 0.40
            }
            summary = rt.step(as_of, quotes, market_data)
            assert summary["regime"] == MarketRegime.RISK_OFF.value
            assert summary["balanced"]["executed_fills"] == 0, "New buys must be blocked under insufficient coverage!"

    def test_scenario_7_insufficient_benchmark_bars_blocks_new_buys(self):
        """Scenario 7: Benchmark has < 120 bars (e.g. 50 bars) -> Data quality error, RISK_OFF, blocks buys."""
        as_of = "2026-05-10"
        short_bench = make_bullish_benchmark(as_of, count=50)

        # 1. MarketRiskRule check
        rule = MarketRiskRule()
        regime, breadth, audit = rule.determine_regime(short_bench, as_of=as_of, market_breadth=0.65)
        assert regime == MarketRegime.RISK_OFF
        assert breadth is None
        assert audit.passed is False
        assert audit.action_taken == "BLOCK_NEW_BUYS"
        assert audit.rule_name == "MARKET_BENCHMARK_INTEGRITY"

        # 2. TradingRuntime check
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "test.sqlite")
            rt = TradingRuntime(db_path=db_path, mode="ACTIVE", initial_capital=100000.0)
            quotes = {"sh600000": {"price": 10.0, "name": "浦发银行", "sector": "银行"}}
            market_data = {
                "candidates": [
                    {"symbol": "sh600000", "name": "浦发银行", "score": 90.0, "sector": "银行", "price": 10.0, "ret20": 8.0, "atr": 0.5},
                ],
                "csi300_bars": short_bench,
                "market_breadth": 0.65
            }
            summary = rt.step(as_of, quotes, market_data)
            assert summary["regime"] == MarketRegime.RISK_OFF.value
            assert summary["balanced"]["executed_fills"] == 0

    def test_scenario_8_historical_asof_earlier_than_dataset_end(self):
        """Scenario 8: Historical as_of earlier than dataset end -> Zero lookahead truncation."""
        dataset_last_date = "2026-11-20"
        hist_asof = "2026-05-10"

        # Construct 300 bars from 2026-01-01 to 2026-11-20
        # Up to 2026-05-10, market is bullish golden cross
        # After 2026-05-10, market crashes dramatically
        start_dt = datetime.date(2026, 1, 1)
        as_of_dt = datetime.date.fromisoformat(hist_asof)
        end_dt = datetime.date.fromisoformat(dataset_last_date)
        total_days = (end_dt - start_dt).days + 1

        bars = []
        for i in range(total_days):
            d = start_dt + datetime.timedelta(days=i)
            d_str = d.isoformat()
            if d <= as_of_dt:
                px = 3500.0 + i * 5.0  # Steady bullish
            else:
                px = 2000.0 - (i - 130) * 10.0  # Severe crash in the future!
            bars.append({"date": d_str, "close": px})

        rule = MarketRiskRule()
        # Truncated strictly at hist_asof
        regime, breadth, audit = rule.determine_regime(bars, as_of=hist_asof, market_breadth=0.65)
        # Because it strictly truncated at hist_asof, it sees the bullish golden cross, NOT the future crash
        assert regime == MarketRegime.STRONG_RISK_ON
        assert audit.passed is True
        assert audit.details["as_of"] == hist_asof
        assert audit.details["close"] == 3500.0 + (as_of_dt - start_dt).days * 5.0
