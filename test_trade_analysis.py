#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

from trade_analysis import (
    find_matching_buy_trades,
    extract_holding_kline_data,
    generate_trade_kline_chart,
    format_trade_review_markdown,
    find_trade_by_id,
    CHARTS_DIR
)
import server


class TestTradeAnalysis(unittest.TestCase):
    def setUp(self):
        self.trades = [
            {
                "trade_id": "T_BUY_1",
                "order_id": 101,
                "time": "2026-11-02 09:51:48",
                "symbol": "sh601857",
                "name": "中国石油",
                "side": "BUY",
                "price": 13.85,
                "shares": 1100,
                "amount": 15235.0,
                "fees": 5.0
            },
            {
                "trade_id": "T_SELL_1",
                "order_id": 102,
                "time": "2026-11-05 09:30:05",
                "symbol": "sh601857",
                "name": "中国石油",
                "side": "SELL",
                "price": 12.88,
                "shares": 1100,
                "amount": 14168.0,
                "fees": 15.0,
                "realized_pnl": -1084.38,
                "reason": "Momentum_Rotation_Pro: 2026-11-05盘中硬止损信号，实时触发执行"
            }
        ]

        self.mock_history = {
            "sh601857": [
                {"date": "2026-10-30", "open": 13.60, "high": 13.88, "low": 13.51, "close": 13.84, "volume": 1647900},
                {"date": "2026-11-02", "open": 13.71, "high": 14.07, "low": 13.56, "close": 14.02, "volume": 1840048},
                {"date": "2026-11-03", "open": 13.88, "high": 14.00, "low": 13.24, "close": 14.00, "volume": 2247680},
                {"date": "2026-11-04", "open": 13.96, "high": 14.02, "low": 13.50, "close": 13.62, "volume": 2197220},
                {"date": "2026-11-05", "open": 12.88, "high": 12.88, "low": 12.88, "close": 12.88, "volume": 500000}
            ]
        }

    def test_find_matching_buy_trades(self):
        sell = self.trades[1]
        matching, summary = find_matching_buy_trades(sell, self.trades)
        self.assertEqual(len(matching), 1)
        self.assertEqual(summary["buy_price"], 13.85)
        self.assertEqual(summary["buy_date"], "2026-11-02")
        self.assertEqual(summary["buy_shares"], 1100)

    def test_extract_holding_kline_data(self):
        sell = self.trades[1]
        review = extract_holding_kline_data(
            sell_trade=sell,
            trades_history=self.trades,
            history_cache=self.mock_history
        )

        self.assertEqual(review["symbol"], "sh601857")
        self.assertEqual(review["holding_days"], 4)
        self.assertEqual(review["buy_price"], 13.85)
        self.assertEqual(review["sell_price"], 12.88)
        self.assertEqual(review["max_high"], 14.07)
        self.assertEqual(review["max_high_date"], "2026-11-02")
        self.assertAlmostEqual(review["max_profit_pct"], 1.59, places=1)
        self.assertEqual(review["realized_pnl"], -1084.38)
        self.assertAlmostEqual(review["realized_pnl_pct"], -7.00, places=1)
        self.assertIn("未能形成持续主升浪", review["verdict"])
        self.assertEqual(len(review["daily_trajectory"]), 4)

    def test_generate_chart_and_markdown(self):
        sell = self.trades[1]
        review = extract_holding_kline_data(
            sell_trade=sell,
            trades_history=self.trades,
            history_cache=self.mock_history
        )
        chart_path = generate_trade_kline_chart(review)
        self.assertTrue(os.path.exists(chart_path))
        self.assertGreater(os.path.getsize(chart_path), 10000)

        md = format_trade_review_markdown(review, chart_base_url="http://127.0.0.1:8000")
        self.assertIn("持仓周期 K 线与盈利机会复盘", md)
        self.assertIn("中国石油", md)
        self.assertIn("+1.59%", md)
        self.assertIn("逐日价格与浮盈轨迹", md)
        self.assertIn("/api/trade/chart/T_SELL_1", md)

    def test_api_trade_review_and_chart(self):
        class MockAccount:
            def __init__(self, trades):
                self.trades = list(trades)
                self.positions = {}

        old_acc = server.ctx.account
        server.ctx.account = MockAccount(self.trades)

        try:
            client = TestClient(server.app)
            # 测试 /api/trade/review/T_SELL_1
            resp = client.get("/api/trade/review/T_SELL_1")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data["review"]["symbol"], "sh601857")

            # 测试 /api/trade/chart/T_SELL_1
            resp_chart = client.get("/api/trade/chart/T_SELL_1")
            self.assertEqual(resp_chart.status_code, 200)
            self.assertEqual(resp_chart.headers["content-type"], "image/png")
            self.assertGreater(len(resp_chart.content), 5000)
        finally:
            server.ctx.account = old_acc


if __name__ == "__main__":
    unittest.main()
