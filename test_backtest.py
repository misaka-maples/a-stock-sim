#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史数据获取与策略回测引擎自动化单元测试 (Unit Tests for Backtesting Engine)
"""

import os
import sys
import shutil
import pytest
from pathlib import Path

from history_data import HistoricalDataFeed, PRESET_POOLS, normalize_code_for_eastmoney, normalize_symbol_for_tencent
from backtest import BacktestEngine, run_backtest

TEST_CACHE_DIR = Path(__file__).resolve().parent / "data" / "test_history_cache"


@pytest.fixture(autouse=True)
def setup_and_teardown():
    TEST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    if TEST_CACHE_DIR.exists():
        shutil.rmtree(TEST_CACHE_DIR, ignore_errors=True)


def test_code_normalization():
    """测试代码格式化"""
    secid, code = normalize_code_for_eastmoney("sh600519")
    assert secid == "1.600519" and code == "600519"

    secid, code = normalize_code_for_eastmoney("sz000001")
    assert secid == "0.000001" and code == "000001"

    std = normalize_symbol_for_tencent("600519")
    assert std == "sh600519"

    std = normalize_symbol_for_tencent("000001")
    assert std == "sz000001"


def test_preset_pools():
    """测试预设股票池完整性"""
    assert "core_active" in PRESET_POOLS
    assert "csi300_sample" in PRESET_POOLS
    symbols = HistoricalDataFeed.get_preset_symbols("core_active")
    assert len(symbols) >= 10
    assert "sz300319" in symbols  # 麦捷科技
    assert "sh600237" in symbols  # 铜峰电子


def test_benchmark_fetching():
    """测试基准指数拉取与缓存"""
    feed = HistoricalDataFeed(cache_dir=TEST_CACHE_DIR)
    bars = feed.fetch_benchmark_kline(
        index_code="000300",
        start_date="2026-09-01",
        end_date="2026-09-15",
        use_cache=True
    )
    assert len(bars) > 0
    first = bars[0]
    assert "date" in first
    assert "close" in first
    assert "open" in first
    assert "high" in first
    assert "low" in first
    assert first["close"] > 0

    # 验证缓存文件是否写入
    cache_files = list(TEST_CACHE_DIR.glob("idx_000300_*.json"))
    assert len(cache_files) == 1

    # 第二次读取应命中缓存
    bars2 = feed.fetch_benchmark_kline(
        index_code="000300",
        start_date="2026-09-01",
        end_date="2026-09-15",
        use_cache=True
    )
    assert len(bars2) == len(bars)


def test_stock_kline_fetching():
    """测试单只股票日K线拉取与缓存"""
    feed = HistoricalDataFeed(cache_dir=TEST_CACHE_DIR)
    sym, name, bars = feed.fetch_stock_kline(
        "sz300319",
        start_date="2026-09-01",
        end_date="2026-09-15",
        use_cache=True
    )
    assert sym == "sz300319"
    assert len(bars) > 0
    assert bars[-1]["close"] > 0
    assert bars[-1]["volume"] > 0


def test_backtest_execution_and_metrics():
    """测试回测引擎完整运行与指标计算"""
    res = run_backtest(
        start_date="2026-08-01",
        end_date="2026-09-15",
        initial_cash=100000.0,
        strategy_name="ShortTermResonance",
        symbols=["sz300319", "sh600237", "sh600105", "sz003035"],
        take_profit_trigger_pct=3.5,
        stop_loss_pct=-2.5,
        max_positions=2,
        max_stock_weight=0.4
    )

    assert "summary" in res
    assert "equity_curve" in res
    assert "trades" in res

    s = res["summary"]
    assert s["initial_cash"] == 100000.0
    assert s["trading_days"] > 0
    assert "final_equity" in s
    assert "total_return_pct" in s
    assert "benchmark_return_pct" in s
    assert "alpha_pct" in s
    assert "max_drawdown_pct" in s
    assert "win_rate" in s
    assert "profit_loss_ratio" in s
    assert "sharpe_ratio" in s

    # 验证资产净值曲线连续性
    curve = res["equity_curve"]
    assert len(curve) == s["trading_days"]
    assert curve[0]["equity"] > 0

    # 验证交易流水与 T+1 纪律
    for t in res["trades"]:
        assert t["shares"] % 100 == 0  # 必须是 100 整手数
        assert t["buy_date"] < t["sell_date"]  # 严格次日及以后卖出，T+1
        assert t["fees"] > 0  # 必须扣除手续费
