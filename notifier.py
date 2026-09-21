#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股量化模拟交易系统 - 微信推送与消息通知模块
支持双通道通知：
1. WxPusher: 支持网页生成微信二维码扫码关注、自动回调/轮询绑定 UID、极简 SPT 推送
2. Server酱 (方糖 Turbo 版): 支持 SendKey 一键推送微信
"""

import os
import json
import logging
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger("notifier")

BASE_DIR = Path(__file__).resolve().parent
NOTIFICATION_CONFIG_PATH = BASE_DIR / "data" / "notification_config.json"


class WxPusherClient:
    """WxPusher 微信推送客户端"""

    API_SEND_MESSAGE = "https://wxpusher.zjiecode.com/api/send/message"
    API_SIMPLE_PUSH = "https://wxpusher.zjiecode.com/api/send/message/simple-push"
    API_CREATE_QRCODE = "https://wxpusher.zjiecode.com/api/fun/create/qrcode"
    API_SCAN_QRCODE_UID = "https://wxpusher.zjiecode.com/api/fun/scan-qrcode-uid"

    @classmethod
    def create_qrcode(cls, app_token: str, extra: str = "binding", valid_time: int = 1800) -> Dict[str, Any]:
        """
        生成带参数的关注二维码
        :param app_token: WxPusher 应用 AppToken (以 AT_ 开头)
        :param extra: 自定义业务透传参数
        :param valid_time: 二维码有效期 (秒)，默认 1800 秒 (30分钟)
        :return: {"success": bool, "data": {"url": ..., "code": ..., "shortUrl": ...}, "message": str}
        """
        if not app_token:
            return {"success": False, "message": "AppToken 不能为空"}

        try:
            payload = {
                "appToken": app_token.strip(),
                "extra": str(extra),
                "validTime": valid_time
            }
            res = requests.post(cls.API_CREATE_QRCODE, json=payload, timeout=8)
            data = res.json()
            if data.get("code") == 1000 and data.get("data"):
                return {
                    "success": True,
                    "data": data["data"],
                    "message": "二维码创建成功"
                }
            return {
                "success": False,
                "message": data.get("msg", "创建二维码失败"),
                "raw": data
            }
        except Exception as e:
            logger.error(f"[WxPusher] 创建二维码异常: {e}")
            return {"success": False, "message": f"网络请求异常: {e}"}

    @classmethod
    def query_scan_status(cls, code: str) -> Dict[str, Any]:
        """
        根据创建二维码时返回的 code 查询是否有用户扫码并获取其 UID
        WxPusher 要求轮询间隔不小于 10 秒
        :param code: 创建二维码返回的 code
        :return: {"success": bool, "scanned": bool, "uid": str or None, "message": str}
        """
        if not code:
            return {"success": False, "scanned": False, "message": "code 不能为空"}

        try:
            url = f"{cls.API_SCAN_QRCODE_UID}?code={code.strip()}"
            res = requests.get(url, timeout=8)
            data = res.json()
            if data.get("code") == 1000:
                uid = data.get("data")
                if uid:
                    return {
                        "success": True,
                        "scanned": True,
                        "uid": uid,
                        "message": "用户已扫码并授权成功"
                    }
                return {
                    "success": True,
                    "scanned": False,
                    "uid": None,
                    "message": "等待用户扫码中"
                }
            return {
                "success": False,
                "scanned": False,
                "message": data.get("msg", "查询扫码状态失败"),
                "raw": data
            }
        except Exception as e:
            logger.error(f"[WxPusher] 查询扫码状态异常: {e}")
            return {"success": False, "scanned": False, "message": f"网络异常: {e}"}

    @classmethod
    def send_message(
        cls,
        app_token: str,
        content: str,
        summary: str = "",
        uids: Optional[List[str]] = None,
        topic_ids: Optional[List[int]] = None,
        url: str = ""
    ) -> Dict[str, Any]:
        """
        标准应用消息推送 (向关注该应用的用户发送 Markdown 消息)
        """
        if not app_token:
            return {"success": False, "message": "未配置 AppToken"}
        if not uids and not topic_ids:
            return {"success": False, "message": "未指定接收者 UID 或 TopicID"}

        try:
            payload = {
                "appToken": app_token.strip(),
                "content": content,
                "summary": summary[:100] if summary else content[:50].replace("\n", " "),
                "contentType": 3,  # 3 代表 Markdown 格式
                "uids": [u.strip() for u in (uids or []) if u.strip()],
                "topicIds": topic_ids or [],
                "url": url
            }
            res = requests.post(cls.API_SEND_MESSAGE, json=payload, timeout=8)
            data = res.json()
            if data.get("code") == 1000:
                return {"success": True, "message": "WxPusher 消息发送成功", "data": data.get("data")}
            return {"success": False, "message": f"WxPusher 错误: {data.get('msg')}", "raw": data}
        except Exception as e:
            logger.error(f"[WxPusher] 消息发送异常: {e}")
            return {"success": False, "message": f"WxPusher 发送异常: {e}"}

    @classmethod
    def send_simple_push(cls, spt: str, content: str, summary: str = "") -> Dict[str, Any]:
        """
        极简推送 (SPT, Simple Push Token)
        """
        if not spt:
            return {"success": False, "message": "未配置 SPT"}

        try:
            payload = {
                "spt": spt.strip(),
                "content": content,
                "summary": summary[:100] if summary else content[:50].replace("\n", " "),
                "contentType": 3
            }
            res = requests.post(cls.API_SIMPLE_PUSH, json=payload, timeout=8)
            data = res.json()
            if data.get("code") == 1000:
                return {"success": True, "message": "WxPusher SPT 消息发送成功", "data": data.get("data")}
            return {"success": False, "message": f"WxPusher SPT 错误: {data.get('msg')}", "raw": data}
        except Exception as e:
            logger.error(f"[WxPusher] 极简推送异常: {e}")
            return {"success": False, "message": f"WxPusher SPT 异常: {e}"}


class ServerChanClient:
    """Server酱 (方糖 Turbo 版) 客户端"""

    @classmethod
    def send_message(cls, sendkey: str, title: str, content: str) -> Dict[str, Any]:
        """
        通过 Server酱 Turbo 发送微信通知
        :param sendkey: 用户的 SendKey (以 SCT 开头)
        :param title: 消息标题 (最多 32 字)
        :param content: Markdown 消息正文
        """
        if not sendkey:
            return {"success": False, "message": "未配置 Server酱 SendKey"}

        try:
            url = f"https://sctapi.ftqq.com/{sendkey.strip()}.send"
            payload = {
                "title": title[:32],
                "desp": content
            }
            res = requests.post(url, data=payload, timeout=8)
            data = res.json()
            if data.get("code") == 0:
                return {"success": True, "message": "Server酱 消息发送成功", "data": data.get("data")}
            return {"success": False, "message": f"Server酱 错误: {data.get('message')}", "raw": data}
        except Exception as e:
            logger.error(f"[Server酱] 发送异常: {e}")
            return {"success": False, "message": f"Server酱 发送异常: {e}"}


class NotificationManager:
    """统一微信推送管理器 (负责格式化模板、分发路由、线程池异步非阻塞执行)"""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(NotificationManager, cls).__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.config_path = NOTIFICATION_CONFIG_PATH
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="wx_notifier")
        self.config: Dict[str, Any] = self._load_default_config()
        self.load_config()

    def _load_default_config(self) -> Dict[str, Any]:
        return {
            "enabled": False,
            "provider": "wxpusher",  # "wxpusher" | "serverchan" | "both"
            # WxPusher 自建应用参数
            "wxpusher_app_token": "",
            "wxpusher_uids": [],
            "wxpusher_topic_ids": [],
            # WxPusher 极简推送参数
            "wxpusher_spt": "",
            # Server酱 参数
            "serverchan_sendkey": "",
            # 通知事件开关
            "notify_on_buy": True,
            "notify_on_sell": True,
            "notify_on_daily_summary": True,
            "notify_on_risk_alert": True,
            "updated_at": ""
        }

    def load_config(self) -> Dict[str, Any]:
        """从文件加载推送配置"""
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                self.config.update(data)
            except Exception as e:
                logger.warning(f"读取微信推送配置失败: {e}")
        return self.config

    def save_config(self, new_config: Dict[str, Any]) -> bool:
        """保存推送配置到本地文件"""
        try:
            self.config.update(new_config)
            self.config["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("微信推送配置已成功持久化更新")
            return True
        except Exception as e:
            logger.error(f"保存微信推送配置失败: {e}")
            return False

    def get_safe_config(self) -> Dict[str, Any]:
        """返回脱敏后的配置供前端展示"""
        safe = dict(self.config)

        def mask_token(s: str) -> str:
            if not s:
                return ""
            if len(s) <= 8:
                return s[:2] + "****"
            return s[:4] + "****" + s[-4:]

        safe["wxpusher_app_token_masked"] = mask_token(safe.get("wxpusher_app_token", ""))
        safe["wxpusher_spt_masked"] = mask_token(safe.get("wxpusher_spt", ""))
        safe["serverchan_sendkey_masked"] = mask_token(safe.get("serverchan_sendkey", ""))
        return safe

    def dispatch_message(self, title: str, content: str, summary: str = "") -> List[Dict[str, Any]]:
        """
        同步或内部向配置的所有可用渠道派发消息
        """
        if not self.config.get("enabled", False):
            return [{"success": False, "channel": "none", "message": "微信推送未开启"}]

        results = []
        provider = self.config.get("provider", "wxpusher")

        # 1. WxPusher 渠道
        if provider in ("wxpusher", "both"):
            app_token = self.config.get("wxpusher_app_token", "").strip()
            uids = self.config.get("wxpusher_uids", [])
            spt = self.config.get("wxpusher_spt", "").strip()

            if app_token and uids:
                res = WxPusherClient.send_message(
                    app_token=app_token,
                    content=content,
                    summary=summary or title,
                    uids=uids,
                    topic_ids=self.config.get("wxpusher_topic_ids", [])
                )
                res["channel"] = "wxpusher_app"
                results.append(res)
            elif spt:
                res = WxPusherClient.send_simple_push(
                    spt=spt,
                    content=content,
                    summary=summary or title
                )
                res["channel"] = "wxpusher_spt"
                results.append(res)
            else:
                results.append({
                    "success": False,
                    "channel": "wxpusher",
                    "message": "未配置有效 WxPusher AppToken+UID 或 SPT"
                })

        # 2. Server酱 渠道
        if provider in ("serverchan", "both"):
            sendkey = self.config.get("serverchan_sendkey", "").strip()
            if sendkey:
                res = ServerChanClient.send_message(
                    sendkey=sendkey,
                    title=title,
                    content=content
                )
                res["channel"] = "serverchan"
                results.append(res)
            else:
                results.append({
                    "success": False,
                    "channel": "serverchan",
                    "message": "未配置 Server酱 SendKey"
                })

        return results

    def async_dispatch(self, title: str, content: str, summary: str = ""):
        """异步非阻塞提交到线程池发送，绝不阻塞量化撮合主循环"""
        if not self.config.get("enabled", False):
            return
        self.executor.submit(self.dispatch_message, title, content, summary)

    # ==========================================================================
    # 业务通知模板
    # ==========================================================================

    def notify_trade(self, trade: Dict[str, Any], total_equity: Optional[float] = None, cash: Optional[float] = None):
        """买入/卖出成交微信通知"""
        side = trade.get("side", "BUY")
        is_buy = side == "BUY"

        if is_buy and not self.config.get("notify_on_buy", True):
            return
        if not is_buy and not self.config.get("notify_on_sell", True):
            return

        side_text = "🟢 买入成交" if is_buy else "🔴 卖出成交"
        symbol = trade.get("symbol", "")
        name = trade.get("name", "")
        price = trade.get("price", 0.0)
        shares = trade.get("shares", 0)
        amount = trade.get("amount", 0.0)
        fees = trade.get("fees", 0.0)
        reason = trade.get("reason", "量化策略信号驱动")
        time_str = trade.get("time", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        title = f"【A股模拟】{side_text}：{name}({symbol}) {shares}股"
        summary = f"{side_text} {name} {shares}股 @¥{price:.2f}，金额 ¥{amount:,.2f}"

        lines = [
            f"# {side_text}提醒",
            f"> **标的信息**：`{name}` (`{symbol}`)",
            f"> **成交时间**：{time_str}",
            "",
            "### 📋 订单明细",
            f"- **成交均价**：¥{price:.2f}",
            f"- **成交数量**：{shares:,} 股 ({shares // 100} 手)",
            f"- **成交金额**：¥{amount:,.2f}",
            f"- **交易税费**：¥{fees:.2f}",
            f"- **入场/离场逻辑**：{reason}",
        ]

        if total_equity is not None:
            lines.append(f"- **当前账户总权益**：¥{total_equity:,.2f}")
        if cash is not None:
            lines.append(f"- **当前可用现金**：¥{cash:,.2f}")

        lines.extend([
            "",
            "---",
            "*来自 A股量化模拟交易控制台 · 实时极速推送*"
        ])

        content = "\n".join(lines)
        self.async_dispatch(title=title, content=content, summary=summary)

    def notify_daily_summary(self, summary: Dict[str, Any], positions: Dict[str, Any]):
        """每日 15:00 收盘快报通知"""
        if not self.config.get("notify_on_daily_summary", True):
            return

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        total_equity = summary.get("total_equity", 0.0)
        today_pnl = summary.get("today_pnl", 0.0)
        today_pnl_pct = summary.get("today_pnl_pct", 0.0)
        total_pnl = summary.get("total_pnl", 0.0)
        total_pnl_pct = summary.get("total_pnl_pct", 0.0)
        cash = summary.get("cash", 0.0)
        market_value = summary.get("market_value", 0.0)
        win_rate = summary.get("win_rate", 0.0)
        pl_ratio = summary.get("profit_loss_ratio", 0.0)
        pos_count = len(positions)

        pnl_icon = "📈" if today_pnl >= 0 else "📉"
        title = f"【A股模拟】{pnl_icon} {today_str} 收盘战报 (今日{today_pnl_pct:+.2f}%)"
        summary_text = f"总资产 ¥{total_equity:,.2f}，今日盈亏 ¥{today_pnl:+,.2f} ({today_pnl_pct:+.2f}%)"

        lines = [
            f"# 📊 每日收盘量化战报 ({today_str})",
            "",
            "### 💰 账户核心指标",
            f"- **总资产(权益)**：**¥{total_equity:,.2f}**",
            f"- **今日盈亏**：**{today_pnl:+,.2f} 元 ({today_pnl_pct:+.2f}%)**",
            f"- **累计总盈亏**：{total_pnl:+,.2f} 元 ({total_pnl_pct:+.2f}%)",
            f"- **可用现金**：¥{cash:,.2f} (仓位: {((market_value / total_equity) * 100) if total_equity > 0 else 0:.1f}%)",
            f"- **持仓市值**：¥{market_value:,.2f}",
            f"- **交易胜率**：{win_rate:.1f}% | **盈亏比**：{pl_ratio:.2f}",
            "",
            f"### 📦 当前持仓明细 ({pos_count} 只)",
        ]

        if not positions:
            lines.append("- 当前空仓，保持充裕现金捕捉下轮战机。")
        else:
            for sym, pos in positions.items():
                p_name = pos.get("name", sym)
                shares = pos.get("total_shares", 0)
                cost = pos.get("cost_price", 0.0)
                last = pos.get("last_price", 0.0)
                pnl = pos.get("unrealized_pnl", 0.0)
                pnl_pct = pos.get("pnl_pct", 0.0)
                lines.append(f"- **{p_name}** ({sym}): {shares}股 | 成本 ¥{cost:.2f} → 现价 ¥{last:.2f} | 浮盈 **{pnl:+,.2f}元 ({pnl_pct:+.2f}%)**")

        lines.extend([
            "",
            "---",
            f"*推送时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · A股模拟实盘*"
        ])

        content = "\n".join(lines)
        self.async_dispatch(title=title, content=content, summary=summary_text)

    def send_test_message(self) -> Dict[str, Any]:
        """向当前配置的渠道发送测试微信"""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = "【A股量化系统】微信推送通道测试成功 ✅"
        content = (
            f"# 🎉 微信推送通道连接成功！\n\n"
            f"> 本条消息由 **A股量化模拟交易控制台** 自动触发发出。\n\n"
            f"- **测试时间**：`{now_str}`\n"
            f"- **当前策略**：截面领头羊动量轮动策略 (Momentum_Rotation)\n"
            f"- **推送机制**：已就绪，当系统产生买卖成交或收盘报告时将自动同步发送到您的微信。\n\n"
            f"---\n"
            f"祝您交易顺利，策略收益常青！🚀"
        )
        results = self.dispatch_message(title=title, content=content, summary="微信推送测试消息已送达！")
        success = any(r.get("success", False) for r in results)
        return {
            "success": success,
            "results": results,
            "message": "测试消息发送完成" if success else "测试消息发送失败，请检查配置或凭证是否有效"
        }
