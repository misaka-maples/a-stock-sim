#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信推送模块单元测试
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

from notifier import NotificationManager, WxPusherClient, ServerChanClient


class TestNotifier(unittest.TestCase):

    def setUp(self):
        self.mgr = NotificationManager()
        # 使用临时测试配置
        self.test_cfg_path = BASE_DIR / "data" / "test_notification_config.json"
        self.mgr.config_path = self.test_cfg_path
        if self.test_cfg_path.exists():
            self.test_cfg_path.unlink()
        self.mgr.config = self.mgr._load_default_config()

    def tearDown(self):
        if self.test_cfg_path.exists():
            self.test_cfg_path.unlink()

    def test_default_config_and_masking(self):
        """测试默认配置与敏感凭证脱敏"""
        self.mgr.save_config({
            "enabled": True,
            "provider": "wxpusher",
            "wxpusher_app_token": "AT_1234567890abcdef",
            "wxpusher_uids": ["UID_test12345"],
            "serverchan_sendkey": "SCT999888777666"
        })
        safe_cfg = self.mgr.get_safe_config()
        self.assertTrue(safe_cfg["enabled"])
        self.assertIn("****", safe_cfg["wxpusher_app_token_masked"])
        self.assertIn("****", safe_cfg["serverchan_sendkey_masked"])
        self.assertEqual(safe_cfg["wxpusher_uids"], ["UID_test12345"])

    @patch("requests.post")
    def test_wxpusher_create_qrcode(self, mock_post):
        """测试 WxPusher 二维码生成客户端"""
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "code": 1000,
                "msg": "成功",
                "data": {
                    "url": "https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket=xxx",
                    "code": "test_ticket_123",
                    "shortUrl": "https://wxpusher.zjiecode.com/api/qrcode/xxx"
                }
            }
        )
        res = WxPusherClient.create_qrcode("AT_valid_token", "quant_sim")
        self.assertTrue(res["success"])
        self.assertIn("ticket=xxx", res["data"]["url"])
        self.assertEqual(res["data"]["code"], "test_ticket_123")

    @patch("requests.get")
    def test_wxpusher_query_scan_status(self, mock_get):
        """测试 WxPusher 扫码状态查询接口"""
        # 未扫码状态
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 1000, "data": None}
        )
        res1 = WxPusherClient.query_scan_status("test_code")
        self.assertTrue(res1["success"])
        self.assertFalse(res1["scanned"])
        self.assertIsNone(res1["uid"])

        # 已扫码授权成功
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 1000, "data": "UID_scanned_user_888"}
        )
        res2 = WxPusherClient.query_scan_status("test_code")
        self.assertTrue(res2["success"])
        self.assertTrue(res2["scanned"])
        self.assertEqual(res2["uid"], "UID_scanned_user_888")

    @patch("requests.post")
    def test_serverchan_send_message(self, mock_post):
        """测试 Server酱 消息发送客户端"""
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "message": "", "data": {"pushid": "12345"}}
        )
        res = ServerChanClient.send_message("SCT_key", "买入测试", "买入比亚迪200股")
        self.assertTrue(res["success"])
        self.assertIn("Server酱", res["message"])

    @patch.object(NotificationManager, "dispatch_message")
    def test_trade_notification_formatting(self, mock_dispatch):
        """测试买卖成交通知格式化"""
        self.mgr.config["enabled"] = True
        self.mgr.config["notify_on_buy"] = True
        self.mgr.config["notify_on_sell"] = True

        trade_buy = {
            "side": "BUY",
            "symbol": "sz002594",
            "name": "比亚迪",
            "price": 285.50,
            "shares": 200,
            "amount": 57100.0,
            "fees": 14.85,
            "reason": "突破均线主升浪",
            "time": "2026-09-21 09:35:00"
        }
        self.mgr.notify_trade(trade_buy, total_equity=100000.0, cash=42885.15)
        self.mgr.executor.shutdown(wait=True)  # 等待异步任务完成
        mock_dispatch.assert_called()
        args, kwargs = mock_dispatch.call_args
        title = args[0] if args else kwargs.get("title", "")
        content = args[1] if len(args) > 1 else kwargs.get("content", "")
        self.assertIn("买入成交", title)
        self.assertIn("比亚迪", content)
        self.assertIn("¥285.50", content)


if __name__ == "__main__":
    unittest.main()
