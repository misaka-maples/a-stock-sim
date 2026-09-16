#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP 服务与 REST API 测试脚本
"""

import sys
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

from server import app, ctx
from sim_trader import SimAccount, MomentumBreakoutStrategy

class TestServerAPI(unittest.TestCase):

    def setUp(self):
        # 每个测试独立初始化账户
        test_file = BASE_DIR / "test_api_account.json"
        if test_file.exists():
            test_file.unlink()
        ctx.account = SimAccount(
            initial_cash=500_000.0,
            strict_t1=False,  # 测试环境允许立即卖出
            save_path=str(test_file)
        )
        ctx.symbols = ["600519", "002594"]
        ctx.strategy = MomentumBreakoutStrategy(account=ctx.account, watchlist=ctx.symbols)
        self.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        test_file = BASE_DIR / "test_api_account.json"
        if test_file.exists():
            test_file.unlink()

    def test_index_page(self):
        """测试 Web 页面访问"""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("A股量化模拟交易控制台", res.text)

    def test_get_summary(self):
        """测试账户总览接口"""
        res = self.client.get("/api/summary")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("total_equity", data)
        self.assertIn("cash", data)
        self.assertEqual(data["initial_cash"], 500_000.0)

    def test_get_status(self):
        """测试状态接口"""
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("market_status", data)
        self.assertIn("strategy_active", data)

    def test_strategy_toggle(self):
        """测试自动策略启停接口"""
        res = self.client.post("/api/strategy/toggle", json={"active": False})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["strategy_active"])

        res2 = self.client.post("/api/strategy/toggle", json={"active": True})
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.json()["strategy_active"])

    def test_manual_trade_order(self):
        """测试手动下单接口 (买入与卖出)"""
        # 1. 市价买入 200 股比亚迪 (sz002594)
        buy_res = self.client.post("/api/trade/order", json={
            "symbol": "sz002594",
            "side": "BUY",
            "shares": 200,
            "price": 0.0, # 市价
            "reason": "API测试买入"
        })
        self.assertEqual(buy_res.status_code, 200)
        buy_data = buy_res.json()
        self.assertTrue(buy_data["success"], f"买入失败: {buy_data.get('reason')}")

        # 检查持仓
        pos_res = self.client.get("/api/positions")
        self.assertEqual(pos_res.status_code, 200)
        pos_list = pos_res.json()
        self.assertTrue(any(p["symbol"] == "sz002594" for p in pos_list))

        # 2. 市价卖出 200 股
        sell_res = self.client.post("/api/trade/order", json={
            "symbol": "sz002594",
            "side": "SELL",
            "shares": 200,
            "price": 0.0,
            "reason": "API测试卖出"
        })
        self.assertEqual(sell_res.status_code, 200)
        sell_data = sell_res.json()
        self.assertTrue(sell_data["success"], f"卖出失败: {sell_data.get('reason')}")

    def test_universe_and_search(self):
        """测试全市场股票池概况与个股快速检索接口"""
        # 测试全市场股票池概况
        u_res = self.client.get("/api/universe")
        self.assertEqual(u_res.status_code, 200)
        u_data = u_res.json()
        self.assertIn("total_count", u_data)
        self.assertIn("exclude_rules", u_data)

        # 测试个股检索 (平安银行 000001)
        s_res = self.client.get("/api/stock/search?q=000001")
        self.assertEqual(s_res.status_code, 200)
        s_data = s_res.json()
        self.assertTrue(len(s_data) > 0)
        self.assertEqual(s_data[0]["code"], "000001")

    def test_get_performance(self):
        """测试收益统计与绩效分析接口"""
        res = self.client.get("/api/performance")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("summary", data)
        self.assertIn("equity_curve", data)
        self.assertIn("symbol_stats", data)
        self.assertIn("daily_stats", data)
        s = data["summary"]
        self.assertIn("today_pnl", s)
        self.assertIn("realized_pnl", s)
        self.assertIn("win_rate", s)
        self.assertIn("profit_loss_ratio", s)

    def test_backtest_endpoints(self):
        """测试历史回测配置与执行接口"""
        cfg_res = self.client.get("/api/backtest/config")
        self.assertEqual(cfg_res.status_code, 200)
        cfg_data = cfg_res.json()
        self.assertIn("strategies", cfg_data)
        self.assertIn("pools", cfg_data)

        # 测试简易回测运行
        run_res = self.client.post("/api/backtest/run", json={
            "start_date": "2026-08-01",
            "end_date": "2026-09-15",
            "initial_cash": 100000.0,
            "strategy_name": "ShortTermResonance",
            "symbols": ["sz300319", "sh600237"]
        })
        self.assertEqual(run_res.status_code, 200)
        run_data = run_res.json()
        self.assertTrue(run_data.get("success"))
        self.assertIn("data", run_data)
        self.assertIn("summary", run_data["data"])
        self.assertIn("equity_curve", run_data["data"])

    def test_strategy_list(self):
        """测试获取可用量化策略列表接口"""
        res = self.client.get("/api/strategy/list")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("strategies", data)
        self.assertIn("active_strategy_id", data)
        strat_ids = [s["id"] for s in data["strategies"]]
        self.assertIn("Momentum_Rotation", strat_ids)
        self.assertIn("Minervini_SEPA", strat_ids)
        self.assertIn("TurtleBreakout", strat_ids)

        # 检查冠军策略信息
        champion = next(s for s in data["strategies"] if s["id"] == "Momentum_Rotation")
        self.assertIn("77.5", champion["one_year_return"])
        self.assertTrue(champion["recommended"])

    def test_strategy_switch(self):
        """测试在线热切换自动交易策略接口"""
        # 切换到近一年最高收益冠军策略 Momentum_Rotation
        switch_res = self.client.post("/api/strategy/switch", json={
            "strategy_id": "Momentum_Rotation"
        })
        self.assertEqual(switch_res.status_code, 200)
        switch_data = switch_res.json()
        self.assertTrue(switch_data["success"])
        self.assertEqual(switch_data["strategy_id"], "Momentum_Rotation")
        self.assertIn("领头羊动量轮动", switch_data["strategy_name"])

        # 检查 /api/status 反映的新策略
        status_res = self.client.get("/api/status")
        self.assertEqual(status_res.status_code, 200)
        status_data = status_res.json()
        self.assertEqual(status_data["strategy_id"], "Momentum_Rotation")

        # 检查内部 ctx.strategy 是否更新
        from sim_trader import MomentumRotationStrategy
        self.assertIsInstance(ctx.strategy, MomentumRotationStrategy)

        # 测试切换非法策略报错
        bad_res = self.client.post("/api/strategy/switch", json={
            "strategy_id": "NonExistentStrategy"
        })
        self.assertEqual(bad_res.status_code, 400)


if __name__ == "__main__":
    unittest.main()

