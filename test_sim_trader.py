#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟交易系统单元与集成测试脚本
"""

import sys
import unittest
import datetime
from pathlib import Path
import tempfile
import json

from sim_trader import SimAccount, MarketFeed, StockUniverse, MomentumBreakoutStrategy, ShortTermResonanceStrategy, PaperTradingEngine

class TestSimTrader(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.account_file = Path(self.tmp_dir.name) / "test_account.json"
        self.account = SimAccount(
            initial_cash=100_000.0,
            strict_t1=True,
            save_path=str(self.account_file)
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_fee_calculation(self):
        """测试 A 股佣金、印花税与过户费计算"""
        # 1. 小额买入触发 5 元最低佣金 (1000 元 * 0.00025 = 0.25元 < 5元)
        fees = self.account.calculate_fees("BUY", 1000.0)
        self.assertEqual(fees["commission"], 5.0)
        self.assertEqual(fees["stamp_tax"], 0.0)
        self.assertAlmostEqual(fees["transfer_fee"], 0.01, places=2)

        # 2. 大额买入佣金按万2.5计算 (100,000 * 0.00025 = 25元)
        fees = self.account.calculate_fees("BUY", 100_000.0)
        self.assertEqual(fees["commission"], 25.0)
        self.assertEqual(fees["stamp_tax"], 0.0)
        self.assertEqual(fees["transfer_fee"], 1.0)
        self.assertEqual(fees["total_fees"], 26.0)

        # 3. 卖出收取万5印花税 (100,000 * 0.0005 = 50元)
        fees_sell = self.account.calculate_fees("SELL", 100_000.0)
        self.assertEqual(fees_sell["stamp_tax"], 50.0)
        self.assertEqual(fees_sell["commission"], 25.0)
        self.assertEqual(fees_sell["transfer_fee"], 1.0)
        self.assertEqual(fees_sell["total_fees"], 76.0)

    def test_buy_execution_and_constraints(self):
        """测试买入限制与持仓变化"""
        # 1. 尝试买非整手 (150股应被拒绝)
        res = self.account.execute_buy("sh600519", "贵州茅台", 1500.0, 150)
        self.assertFalse(res["success"])
        self.assertIn("100股的整数倍", res["reason"])

        # 2. 尝试超额买入 (资金不足)
        res = self.account.execute_buy("sh600519", "贵州茅台", 2000.0, 1000)
        self.assertFalse(res["success"])
        self.assertIn("可用现金不足", res["reason"])

        # 3. 正常买入 200 股
        price = 100.0
        shares = 200
        res = self.account.execute_buy("sz002594", "比亚迪", price, shares, reason="测试买入")
        self.assertTrue(res["success"])

        # 检查账户与持仓
        self.assertIn("sz002594", self.account.positions)
        pos = self.account.positions["sz002594"]
        self.assertEqual(pos["total_shares"], 200)
        # 在 T+1 严格模式下，当日买入的可用股份应为 0
        self.assertEqual(pos["available_shares"], 0)
        self.assertGreater(pos["cost_price"], price) # 成本价计入了手续费
        self.assertLess(self.account.cash, 100_000.0 - 20000.0)

    def test_t1_lock_and_unlock(self):
        """测试 A 股严格 T+1 锁仓与解冻"""
        # 当日买入
        res = self.account.execute_buy("sz002594", "比亚迪", 100.0, 200)
        self.assertTrue(res["success"])

        # 当日尝试卖出，应被 T+1 拦截
        res_sell = self.account.execute_sell("sz002594", 105.0, 200)
        self.assertFalse(res_sell["success"])
        self.assertIn("T+1今日锁仓", res_sell["reason"])

        # 模拟进入下一个交易日
        tomorrow = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.account.update_t1_available_shares(today_str=tomorrow)

        # 检查是否解冻为可用
        pos = self.account.positions["sz002594"]
        self.assertEqual(pos["available_shares"], 200)

        # 次日卖出成功
        res_sell2 = self.account.execute_sell("sz002594", 110.0, 200, reason="次日止盈卖出")
        self.assertTrue(res_sell2["success"])
        self.assertNotIn("sz002594", self.account.positions) # 仓位已清空
        self.assertGreater(res_sell2["trade"]["realized_pnl"], 1800.0) # 盈利 2000 扣除双边税费

    def test_persistence_save_load(self):
        """测试 JSON 持久化与断点加载"""
        self.account.execute_buy("sz002594", "比亚迪", 100.0, 100)
        self.assertTrue(self.account_file.exists())

        # 读取并重建账户对象
        new_acc = SimAccount(save_path=str(self.account_file))
        self.assertEqual(new_acc.cash, self.account.cash)
        self.assertIn("sz002594", new_acc.positions)
        self.assertEqual(new_acc.positions["sz002594"]["total_shares"], 100)
        self.assertEqual(len(new_acc.trades), 1)

    def test_market_feed_live_quotes(self):
        """测试腾讯实时行情接口连通性与解析字段"""
        test_symbols = ["sh600519", "sz002594"]
        quotes = MarketFeed.fetch_quotes(test_symbols)
        self.assertIn("sh600519", quotes)
        q = quotes["sh600519"]
        self.assertIn("current", q)
        self.assertGreater(q["current"], 0)
        self.assertIn("bids", q)
        self.assertIn("asks", q)
        self.assertIn("name", q)
        print(f"\n[测试实盘报价成功] {q['name']}({q['symbol']}): 最新价 {q['current']}, 涨跌幅 {q['change_pct']:+}%")

    def test_engine_single_step(self):
        """测试引擎单步执行流程与策略触发"""
        strat = MomentumBreakoutStrategy(
            account=self.account,
            watchlist=["600519", "002594"],
            min_change_pct=-10.0, # 扩大买入触发区间以测试策略执行
            max_change_pct=15.0
        )
        engine = PaperTradingEngine(
            strategy=strat,
            symbols=["600519", "002594"],
            interval_seconds=1.0,
            ignore_market_hours=True # 强制测试模式
        )
        # 单步执行
        engine.step()
        summary = self.account.get_summary()
        self.assertGreater(summary["total_equity"], 0)

    def test_short_term_resonance_strategy(self):
        """测试超短线共振策略的五档买卖比过滤、硬止损与动态追踪止盈"""
        self.account.strict_t1 = False  # 测试模式下允许日内平仓测试逻辑
        strat = ShortTermResonanceStrategy(
            account=self.account,
            watchlist=["sz002475"],
            min_change_pct=2.0,
            max_change_pct=5.5,
            min_bid_ask_ratio=1.5,
            take_profit_trigger_pct=3.5,
            trailing_callback_pct=1.5,
            stop_loss_pct=-2.0
        )

        # 1. 模拟买盘不足情境 (买盘 500手 < 卖盘 1000手，比率 0.5 < 1.5) -> 不开仓
        weak_order_book = {
            "sz002475": {
                "symbol": "sz002475", "name": "立讯精密", "current": 50.0,
                "open": 48.5, "pre_close": 48.5, "change": 1.5, "change_pct": 3.09,
                "fresh": True,
                "bids": [{"level": 1, "price": 49.95, "volume": 500}],
                "asks": [{"level": 1, "price": 50.00, "volume": 1000}]
            }
        }
        strat.on_tick(weak_order_book)
        self.assertNotIn("sz002475", self.account.positions)

        # 2. 模拟盘口共振强势情境 (买盘 3000手 >= 卖盘 1000手的 1.5倍) -> 触发买入
        strong_order_book = {
            "sz002475": {
                "symbol": "sz002475", "name": "立讯精密", "current": 50.0,
                "open": 48.5, "pre_close": 48.5, "change": 1.5, "change_pct": 3.09,
                "fresh": True,
                "bids": [{"level": 1, "price": 49.98, "volume": 3000}],
                "asks": [{"level": 1, "price": 50.00, "volume": 1000}]
            }
        }
        strat.on_tick(strong_order_book)
        self.assertIn("sz002475", self.account.positions)
        self.assertEqual(strat.peak_prices.get("sz002475"), 50.0)

        # 3. 模拟冲高至 +4.0% (价格 52.0) 激活追踪止盈
        tick_high = {
            "sz002475": {
                "symbol": "sz002475", "name": "立讯精密", "current": 52.0,
                "open": 48.5, "pre_close": 48.5, "change": 3.5, "change_pct": 7.2,
                "fresh": True,
                "bids": [{"level": 1, "price": 51.98, "volume": 2000}],
                "asks": [{"level": 1, "price": 52.00, "volume": 1000}]
            }
        }
        self.account.update_market_prices(tick_high)
        strat.on_tick(tick_high)
        self.assertEqual(strat.peak_prices.get("sz002475"), 52.0)
        self.assertIn("sz002475", self.account.positions) # 尚未回撤，继续持仓

        # 4. 冲高后从 52.0 回落至 51.1 (回撤 -1.73% > 1.5%) -> 触发追踪止盈卖出
        tick_pullback = {
            "sz002475": {
                "symbol": "sz002475", "name": "立讯精密", "current": 51.1,
                "open": 48.5, "pre_close": 48.5, "change": 2.6, "change_pct": 5.36,
                "fresh": True,
                "bids": [{"level": 1, "price": 51.08, "volume": 2000}],
                "asks": [{"level": 1, "price": 51.10, "volume": 1000}]
            }
        }
        self.account.update_market_prices(tick_pullback)
        strat.on_tick(tick_pullback)
        # 验证已触发止盈平仓
        self.assertNotIn("sz002475", self.account.positions)
        last_trade = self.account.trades[-1]
        self.assertEqual(last_trade["side"], "SELL")
        self.assertIn("动态追踪止盈", last_trade["reason"])

    def test_stock_universe_exclusion(self):
        """测试科创板 (688*) 严格排除逻辑"""
        self.assertTrue(StockUniverse.is_sci_tech_board("sh688001"))
        self.assertTrue(StockUniverse.is_sci_tech_board("sz688981"))
        self.assertTrue(StockUniverse.is_sci_tech_board("688111"))
        # 主板含 688 的非科创板股票不得误判
        self.assertFalse(StockUniverse.is_sci_tech_board("sh600688"))
        self.assertFalse(StockUniverse.is_sci_tech_board("sh601688"))
        self.assertFalse(StockUniverse.is_sci_tech_board("sz000001"))
        self.assertFalse(StockUniverse.is_sci_tech_board("sz300750"))

    def test_is_main_board(self):
        """测试沪深纯主板判断逻辑 (仅允许 600/601/603/605/000/001/002/003)"""
        # 沪市主板: 600, 601, 603, 605
        self.assertTrue(StockUniverse.is_main_board("sh600519"))
        self.assertTrue(StockUniverse.is_main_board("sh601398"))
        self.assertTrue(StockUniverse.is_main_board("sh603288"))
        self.assertTrue(StockUniverse.is_main_board("sh605117"))
        self.assertTrue(StockUniverse.is_main_board("600519"))

        # 深市主板: 000, 001, 002, 003
        self.assertTrue(StockUniverse.is_main_board("sz000001"))
        self.assertTrue(StockUniverse.is_main_board("sz001234"))
        self.assertTrue(StockUniverse.is_main_board("sz002594"))
        self.assertTrue(StockUniverse.is_main_board("sz003001"))
        self.assertTrue(StockUniverse.is_main_board("002594"))

        # 严格排除: 科创板 (688*)
        self.assertFalse(StockUniverse.is_main_board("sh688001"))
        self.assertFalse(StockUniverse.is_main_board("688981"))

        # 严格排除: 创业板 (300*, 301*)
        self.assertFalse(StockUniverse.is_main_board("sz300750"))
        self.assertFalse(StockUniverse.is_main_board("sz301520"))
        self.assertFalse(StockUniverse.is_main_board("sz301611"))
        self.assertFalse(StockUniverse.is_main_board("300059"))

        # 严格排除: 北交所 (43*, 83*, 87*, 920*)
        self.assertFalse(StockUniverse.is_main_board("bj430017"))
        self.assertFalse(StockUniverse.is_main_board("bj830001"))
        self.assertFalse(StockUniverse.is_main_board("bj870001"))
        self.assertFalse(StockUniverse.is_main_board("bj920002"))

        # 严格排除: B股与退市/无效代码
        self.assertFalse(StockUniverse.is_main_board("sh900901"))
        self.assertFalse(StockUniverse.is_main_board("sz200002"))
        self.assertFalse(StockUniverse.is_main_board("abc123"))

    def test_can_buy_rejects_non_main_board(self):
        """测试 SimAccount.can_buy 严格拦截非主板标的"""
        # 主板标的允许买入 (以浦发银行 sh600000 为例)
        ok_sh, msg_sh = self.account.can_buy("sh600000", 10.0, 100)
        self.assertTrue(ok_sh, msg_sh)

        ok_sz, msg_sz = self.account.can_buy("sz002594", 250.0, 100)
        self.assertTrue(ok_sz, msg_sz)

        # 创业板标的拦截
        ok_cy, msg_cy = self.account.can_buy("sz301520", 35.0, 100)
        self.assertFalse(ok_cy)
        self.assertIn("不属于沪深主板", msg_cy)

        # 科创板标的拦截
        ok_kc, msg_kc = self.account.can_buy("sh688001", 50.0, 100)
        self.assertFalse(ok_kc)
        self.assertIn("不属于沪深主板", msg_kc)

        # 北交所标的拦截
        ok_bj, msg_bj = self.account.can_buy("bj430017", 10.0, 100)
        self.assertFalse(ok_bj)
        self.assertIn("不属于沪深主板", msg_bj)

    def test_can_sell_allows_existing_non_main_board_positions(self):
        """测试存量非主板持仓（如创业板万邦医药、珂玛科技）依然可以正常受保护并平仓卖出"""
        # 模拟历史持有的创业板仓位
        self.account.positions["sz301520"] = {
            "symbol": "sz301520",
            "name": "万邦医药",
            "total_shares": 500,
            "available_shares": 500,
            "cost_price": 28.0,
            "current_price": 35.0,
            "market_value": 17500.0,
            "pnl": 3500.0,
            "pnl_pct": 25.0
        }
        # can_sell 必须允许通过，不得阻碍平仓
        can_sell, reason = self.account.can_sell("sz301520", 35.0, 500)
        self.assertTrue(can_sell, reason)

        # 执行卖出必须成功
        res = self.account.execute_sell("sz301520", 35.0, 500, reason="止盈平仓测试")
        self.assertTrue(res["success"])
        self.assertNotIn("sz301520", self.account.positions)


if __name__ == "__main__":
    unittest.main()

