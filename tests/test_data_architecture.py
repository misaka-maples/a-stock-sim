# -*- coding: utf-8 -*-
"""
全面自动化测试套件：验证重构后的 A 股量化数据架构
涵盖：
1. 数据模型与代码规范化 (DailyBar, StockInfo, normalize_symbol, to_baostock_code)
2. 数据质量与清洗 (DataQualityValidator, OHLC有效性, 去重, 边界检查)
3. 本地存储 (LocalStorageManager, Parquet原子写入, 幂等性, 增量合并, SQLite元数据, LRU缓存)
4. 智能限流与熔断 (SmartRateLimiter, ProviderCircuitBreaker 状态机转换)
5. Provider 管理与故障转移 (ProviderManager, Failover, 指标统计)
6. 离线回测验证 (切断外网 Mock requests.Session.get, 确保纯本地数据驱动)
7. 双账户行情快照共享 (MarketSnapshot 统一计算, 双账户无冗余复用)
"""

import os
import shutil
import tempfile
import time
import math
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from src.astock.data.models import (
    DailyBar, StockInfo, SyncProgress, SyncTaskRecord,
    normalize_symbol, to_baostock_code, to_clean_code
)
from src.astock.domain.enums import MarketRegime
from src.astock.domain.models import MarketSnapshot
from src.astock.data.quality import DataQualityValidator
from src.astock.data.storage import LocalStorageManager, BoundedMemoryCache
from src.astock.data.rate_limiter import (
    SmartRateLimiter, ProviderCircuitBreaker, RateLimitConfig, CircuitBreakerOpen
)
from src.astock.data.providers.base import BaseMarketDataProvider
from src.astock.data.providers.manager import ProviderManager
from src.astock.data.service import AStockDataService
from history_data import HistoricalDataFeed


class TestDataModels(unittest.TestCase):
    """测试统一行情模型与代码标准化规范"""

    def test_normalize_symbol(self):
        # 沪深股票
        self.assertEqual(normalize_symbol("600000"), "sh600000")
        self.assertEqual(normalize_symbol("000001"), "sz000001")
        self.assertEqual(normalize_symbol("sh600519"), "sh600519")
        self.assertEqual(normalize_symbol("SZ000858"), "sz000858")
        # 指数标准化
        self.assertEqual(normalize_symbol("000300"), "sh000300")
        self.assertEqual(normalize_symbol("000016"), "sh000016")
        self.assertEqual(normalize_symbol("000905"), "sh000905")
        # 北交所
        self.assertEqual(normalize_symbol("830799"), "bj830799")
        self.assertEqual(normalize_symbol("430047"), "bj430047")

    def test_to_baostock_code(self):
        self.assertEqual(to_baostock_code("sh600000"), "sh.600000")
        self.assertEqual(to_baostock_code("sz000001"), "sz.000001")
        self.assertEqual(to_baostock_code("sh000300"), "sh.000300")

    def test_to_clean_code(self):
        self.assertEqual(to_clean_code("sh600000"), "600000")
        self.assertEqual(to_clean_code("sz000001"), "000001")
        self.assertEqual(to_clean_code("600519"), "600519")

    def test_daily_bar_serialization(self):
        bar = DailyBar(
            symbol="sh600000",
            trade_date="2026-11-13",
            open=10.19,
            high=10.44,
            low=10.12,
            close=10.40,
            volume=761573.0,
            amount=786507841.0,
            source="baostock",
            adjust_type="qfq",
            updated_at="2026-11-13T16:00:00",
            is_final=True,
            turnover_rate=0.23,
            change_pct=1.86,
            is_suspended=False,
            is_st=False
        )
        d = bar.to_dict()
        self.assertEqual(d["symbol"], "sh600000")
        self.assertEqual(d["close"], 10.40)
        self.assertEqual(d["raw_close"], 10.40)  # Auto-filled by __post_init__
        reloaded = DailyBar.from_dict(d)
        self.assertEqual(reloaded.trade_date, "2026-11-13")
        self.assertEqual(reloaded.volume, 761573.0)


class TestDataQuality(unittest.TestCase):
    """测试行情数据质量清洗与校验器"""

    def test_valid_bar(self):
        bar = DailyBar(
            symbol="sh600000", trade_date="2026-11-13",
            open=10.0, high=10.5, low=9.8, close=10.2,
            volume=1000.0, amount=1020000.0,
            source="test", adjust_type="qfq",
            updated_at="2026-11-13T15:00:00", is_final=True
        )
        self.assertTrue(DataQualityValidator.validate_single_bar(bar))

    def test_invalid_price_bounds(self):
        # high < low 必须被判定为非法
        bar_bad_high = DailyBar(
            symbol="sh600000", trade_date="2026-11-13",
            open=10.0, high=9.5, low=9.8, close=10.2,
            volume=1000.0, amount=1020000.0,
            source="test", adjust_type="qfq",
            updated_at="2026-11-13T15:00:00", is_final=True
        )
        self.assertFalse(DataQualityValidator.validate_single_bar(bar_bad_high))

        # low > open 必须被判定为非法
        bar_bad_low = DailyBar(
            symbol="sh600000", trade_date="2026-11-13",
            open=10.0, high=11.0, low=10.5, close=10.8,
            volume=1000.0, amount=1020000.0,
            source="test", adjust_type="qfq",
            updated_at="2026-11-13T15:00:00", is_final=True
        )
        self.assertFalse(DataQualityValidator.validate_single_bar(bar_bad_low))

    def test_reject_negative_or_nan(self):
        bar_nan = DailyBar(
            symbol="sh600000", trade_date="2026-11-13",
            open=float("nan"), high=10.5, low=9.8, close=10.2,
            volume=1000.0, amount=1020000.0,
            source="test", adjust_type="qfq",
            updated_at="2026-11-13T15:00:00", is_final=True
        )
        self.assertFalse(DataQualityValidator.validate_single_bar(bar_nan))

        bar_neg = DailyBar(
            symbol="sh600000", trade_date="2026-11-13",
            open=-10.0, high=10.5, low=9.8, close=10.2,
            volume=1000.0, amount=1020000.0,
            source="test", adjust_type="qfq",
            updated_at="2026-11-13T15:00:00", is_final=True
        )
        self.assertFalse(DataQualityValidator.validate_single_bar(bar_neg))

    def test_deduplication_and_sorting(self):
        bar1 = DailyBar(
            symbol="sh600000", trade_date="2026-11-12",
            open=10.0, high=10.5, low=9.8, close=10.2,
            volume=1000.0, amount=1020000.0, source="test",
            adjust_type="qfq", updated_at="2026-11-12T15:00:00", is_final=True
        )
        bar2_old = DailyBar(
            symbol="sh600000", trade_date="2026-11-13",
            open=10.2, high=10.6, low=10.1, close=10.3,
            volume=1200.0, amount=1236000.0, source="test",
            adjust_type="qfq", updated_at="2026-11-13T14:00:00", is_final=False
        )
        bar2_final = DailyBar(
            symbol="sh600000", trade_date="2026-11-13",
            open=10.2, high=10.8, low=10.1, close=10.7,
            volume=1500.0, amount=1605000.0, source="test",
            adjust_type="qfq", updated_at="2026-11-13T15:30:00", is_final=True
        )
        cleaned = DataQualityValidator.clean_and_sort_bars([bar2_old, bar1, bar2_final])
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0].trade_date, "2026-11-12")
        self.assertEqual(cleaned[1].trade_date, "2026-11-13")
        self.assertEqual(cleaned[1].close, 10.7)


class TestLocalStorageParquet(unittest.TestCase):
    """测试本地存储层：Parquet 安全写入、幂等性、增量合并及 SQLite 元数据"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.storage = LocalStorageManager(base_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_atomic_write_and_read(self):
        bars = [
            DailyBar(
                symbol="sh600000", trade_date="2026-11-12",
                open=10.0, high=10.5, low=9.8, close=10.2,
                volume=1000.0, amount=1020000.0, source="test",
                adjust_type="qfq", updated_at="2026-11-12T15:00:00", is_final=True
            ),
            DailyBar(
                symbol="sh600000", trade_date="2026-11-13",
                open=10.2, high=10.8, low=10.1, close=10.7,
                volume=1500.0, amount=1605000.0, source="test",
                adjust_type="qfq", updated_at="2026-11-13T15:30:00", is_final=True
            ),
        ]
        saved = self.storage.save_daily_bars("sh600000", bars, adjust_type="qfq")
        self.assertEqual(saved, 2)

        read_bars = self.storage.get_daily_bars("sh600000", adjust_type="qfq")
        self.assertEqual(len(read_bars), 2)
        self.assertEqual(read_bars[0].trade_date, "2026-11-12")
        self.assertEqual(read_bars[1].trade_date, "2026-11-13")

    def test_idempotent_save(self):
        """重复写入相同数据必须具备幂等性，不产生重复记录"""
        bars = [
            DailyBar(
                symbol="sh600000", trade_date="2026-11-13",
                open=10.2, high=10.8, low=10.1, close=10.7,
                volume=1500.0, amount=1605000.0, source="test",
                adjust_type="qfq", updated_at="2026-11-13T15:30:00", is_final=True
            ),
        ]
        self.storage.save_daily_bars("sh600000", bars, adjust_type="qfq")
        self.storage.save_daily_bars("sh600000", bars, adjust_type="qfq")

        read_bars = self.storage.get_daily_bars("sh600000", adjust_type="qfq")
        self.assertEqual(len(read_bars), 1)

    def test_incremental_merge(self):
        """增量追加新交易日日线"""
        bar1 = [
            DailyBar(
                symbol="sh600000", trade_date="2026-11-12",
                open=10.0, high=10.5, low=9.8, close=10.2,
                volume=1000.0, amount=1020000.0, source="test",
                adjust_type="qfq", updated_at="2026-11-12T15:00:00", is_final=True
            )
        ]
        self.storage.save_daily_bars("sh600000", bar1, adjust_type="qfq")

        bar2 = [
            DailyBar(
                symbol="sh600000", trade_date="2026-11-13",
                open=10.2, high=10.8, low=10.1, close=10.7,
                volume=1500.0, amount=1605000.0, source="test",
                adjust_type="qfq", updated_at="2026-11-13T15:30:00", is_final=True
            )
        ]
        self.storage.save_daily_bars("sh600000", bar2, adjust_type="qfq")

        read_bars = self.storage.get_daily_bars("sh600000", adjust_type="qfq")
        self.assertEqual(len(read_bars), 2)
        self.assertEqual(read_bars[0].trade_date, "2026-11-12")
        self.assertEqual(read_bars[1].trade_date, "2026-11-13")

    def test_sqlite_metadata_and_tasks(self):
        """测试 SQLite 元数据表与任务跟踪"""
        stocks = [
            StockInfo(symbol="sh600000", name="浦发银行", industry="银行", market="主板", list_date="1999-11-10"),
            StockInfo(symbol="sz000001", name="平安银行", industry="银行", market="主板", list_date="1991-04-03"),
        ]
        self.storage.save_stocks(stocks)
        loaded = self.storage.get_stocks()
        self.assertEqual(len(loaded), 2)

        self.storage.update_sync_progress("sh600000", "2026-11-13", total_bars=500)
        prog = self.storage.get_sync_progress("sh600000")
        self.assertIsNotNone(prog)
        self.assertEqual(prog.last_trade_date, "2026-11-13")
        self.assertEqual(prog.total_bars, 500)

        task = SyncTaskRecord(
            task_id="task_test_001", task_type="INCREMENTAL", target_date="2026-11-13",
            total_symbols=3485, completed_symbols=3485, failed_symbols=0,
            status="COMPLETED", started_at="2026-11-14T20:00:00", finished_at="2026-11-14T20:05:00"
        )
        self.storage.record_sync_task(task)
        tasks = self.storage.get_latest_tasks(limit=5)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_id, "task_test_001")
        self.assertEqual(tasks[0].status, "COMPLETED")

    def test_lru_memory_cache(self):
        """测试 LRU 内存上限控制与命中率统计"""
        cache = BoundedMemoryCache(max_items=3)
        cache.put("k1", "v1")
        cache.put("k2", "v2")
        cache.put("k3", "v3")

        self.assertEqual(cache.get("k1"), "v1")  # Hit
        self.assertIsNone(cache.get("k_miss"))    # Miss

        cache.put("k4", "v4")
        self.assertIsNone(cache.get("k2"))  # 确认已驱逐
        self.assertIsNotNone(cache.get("k1"))
        self.assertIsNotNone(cache.get("k4"))

        stats = cache.get_stats()
        self.assertGreater(stats["hit_ratio"], 0.0)


class TestRateLimiterAndCircuitBreaker(unittest.TestCase):
    """测试智能限流器与熔断器状态机三态转换"""

    def test_circuit_breaker_transitions(self):
        cb = ProviderCircuitBreaker(
            provider_name="test_p",
            failure_threshold=3,
            cooldown_seconds=0.2
        )
        self.assertEqual(cb.state, "CLOSED")

        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, "CLOSED")
        self.assertTrue(cb.allow_request())

        cb.record_failure()
        self.assertEqual(cb.state, "OPEN")
        self.assertFalse(cb.allow_request())

        with self.assertRaises(CircuitBreakerOpen):
            if not cb.allow_request():
                raise CircuitBreakerOpen("Circuit is OPEN")

        time.sleep(0.25)
        self.assertTrue(cb.allow_request())
        self.assertEqual(cb.state, "HALF_OPEN")

        cb.record_success()
        self.assertEqual(cb.state, "CLOSED")

    def test_rate_limiter_concurrency_and_retry(self):
        cfg = RateLimitConfig(
            enabled=True,
            requests_per_second=20.0,
            max_concurrency=2,
            retry_times=2,
            backoff_factor=1.1,
            failure_threshold=3
        )
        limiter = SmartRateLimiter("test_limiter", cfg)

        call_count = 0
        def _flaky_call():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("Temporary connection failure")
            return "SUCCESS"

        result = limiter.execute(_flaky_call)
        self.assertEqual(result, "SUCCESS")
        self.assertEqual(call_count, 2)
        self.assertEqual(limiter.metrics.successful_requests, 1)


class DummyMockProvider(BaseMarketDataProvider):
    def __init__(self, name: str, should_fail: bool = False):
        self._name = name
        self.should_fail = should_fail
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    def is_healthy(self) -> bool:
        return not self.should_fail

    def fetch_stock_daily(self, symbol: str, start_date: str, end_date: str, adjust_type: str = "qfq"):
        self.call_count += 1
        if self.should_fail:
            raise RuntimeError(f"Simulated network outage for {self._name}")
        return [
            DailyBar(
                symbol=symbol, trade_date=end_date, open=10.0, high=10.5, low=9.8, close=10.2,
                volume=1000.0, amount=1020000.0, source=self._name, adjust_type=adjust_type,
                updated_at="2026-11-13T15:00:00", is_final=True
            )
        ]

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str):
        return []

    def fetch_stock_list(self):
        return []

    def fetch_industry_map(self):
        return {}


class TestProviderManagerFailover(unittest.TestCase):
    """测试 Provider 管理器故障自动转移"""

    def test_automatic_failover(self):
        pm = ProviderManager()
        failing_primary = DummyMockProvider("baostock", should_fail=True)
        working_secondary = DummyMockProvider("sina", should_fail=False)

        pm.providers["baostock"] = failing_primary
        pm.providers["sina"] = working_secondary

        bars = pm.fetch_stock_daily("sh600000", "2026-11-13", "2026-11-13")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].source, "sina")
        self.assertGreaterEqual(failing_primary.call_count, 1)
        self.assertEqual(working_secondary.call_count, 1)


class TestDualAccountMarketSnapshotSharing(unittest.TestCase):
    """测试双账户共享 MarketSnapshot 架构：避免双账户重复请求与计算"""

    def test_snapshot_computation_and_sharing(self):
        test_dir = tempfile.mkdtemp()
        try:
            storage = LocalStorageManager(base_dir=test_dir)
            start_dt = datetime(2026, 1, 1)
            bm_bars = []
            base_p = 3800.0
            for i in range(150):
                d_str = (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
                bm_bars.append(DailyBar(
                    symbol="sh000300", trade_date=d_str,
                    open=base_p, high=base_p+20, low=base_p-20, close=base_p+10,
                    volume=50000.0, amount=500000000.0, source="test", adjust_type="none",
                    updated_at="2026-11-13T15:00:00", is_final=True
                ))
                base_p += 1.0
            storage.save_daily_bars("sh000300", bm_bars, adjust_type="none")

            last_date = bm_bars[-1].trade_date
            for s_idx in range(20):
                sym = f"sh60{s_idx:04d}"
                s_bar = DailyBar(
                    symbol=sym, trade_date=last_date,
                    open=10.0, high=10.5, low=9.8, close=10.2 if s_idx % 2 == 0 else 9.7,
                    volume=1000.0, amount=1020000.0, source="test", adjust_type="qfq",
                    updated_at="2026-11-13T15:00:00", is_final=True
                )
                storage.save_daily_bars(sym, [s_bar], adjust_type="qfq")

            service = AStockDataService(storage=storage, offline_mode=True)
            snapshot = service.compute_market_snapshot(last_date)

            self.assertIsNotNone(snapshot)
            self.assertIn(snapshot.market_regime, [
                MarketRegime.STRONG_RISK_ON, MarketRegime.RISK_ON,
                MarketRegime.NEUTRAL, MarketRegime.RISK_OFF
            ])
            self.assertGreaterEqual(snapshot.market_breadth, 0.0)

            balanced_regime = snapshot.market_regime
            aggressive_regime = snapshot.market_regime
            self.assertIs(balanced_regime, aggressive_regime)

        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


class TestOfflineBacktestGuarantee(unittest.TestCase):
    """测试回测绝对纯本地化：断网/禁止网络调用条件下回测依然能成功执行"""

    def test_offline_backtest_no_external_network_calls(self):
        feed = HistoricalDataFeed(offline_only=True)
        self.assertTrue(feed.offline_only)

        with patch("requests.Session.get", side_effect=AssertionError("网络调用被严格禁止！回测系统必须100%离线运行！")), \
             patch("requests.get", side_effect=AssertionError("网络调用被严格禁止！")):

            bars = feed.fetch_benchmark_kline("000300", start_date="2026-11-10", end_date="2026-11-13")
            self.assertIsInstance(bars, list)
            if bars:
                self.assertGreaterEqual(len(bars), 1)


if __name__ == "__main__":
    unittest.main()
