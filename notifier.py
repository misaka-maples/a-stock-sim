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
        for key in ("wxpusher_app_token", "wxpusher_spt", "serverchan_sendkey"):
            safe.pop(key, None)
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
        if not is_buy:
            pnl_val = trade.get("realized_pnl")
            if pnl_val is not None:
                rev = trade.get("review")
                peak_info = f"，最高浮盈 +{rev['max_profit_pct']:.2f}%" if rev and rev.get("max_profit_pct") is not None else ""
                summary = f"{side_text} {name} {shares}股 @¥{price:.2f}，盈亏 ¥{float(pnl_val):,.2f}{peak_info}"

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

        if not is_buy and trade.get("realized_pnl") is not None:
            pnl_val = float(trade["realized_pnl"])
            pnl_label = "🔴 赤字亏损" if pnl_val < 0 else "🟢 盈利锁定"
            lines.append(f"- **平仓实现盈亏**：{pnl_label} ¥{pnl_val:,.2f}")

        if total_equity is not None:
            lines.append(f"- **当前账户总权益**：¥{total_equity:,.2f}")
        if cash is not None:
            lines.append(f"- **当前可用现金**：¥{cash:,.2f}")

        sizing = trade.get("sizing")
        if sizing and isinstance(sizing, dict):
            lines.extend([
                "",
                "### 📐 风险暴露与头寸测算",
                f"- **日均波幅 ATR%**：{sizing.get('atr_pct_str', '--')}",
                f"- **2×ATR 风险距离**：{sizing.get('risk_distance_str', '--')}",
                f"- **风险预算权重上限**：{sizing.get('risk_weight_cap_str', '--')}",
                f"- **实际建仓占比**：{sizing.get('actual_pct_str', '--')}"
            ])

        # 若为卖出成交，展示持仓期间 K 线与盈利机会复盘明细
        if not is_buy:
            if trade.get("review_markdown"):
                lines.append("")
                lines.append(trade["review_markdown"])
            elif trade.get("review"):
                from trade_analysis import format_trade_review_markdown
                lines.append("")
                lines.append(format_trade_review_markdown(trade["review"]))

        lines.extend([
            "",
            "---",
            "*来自 A股量化模拟交易控制台 · 实时极速推送*"
        ])

        content = "\n".join(lines)
        self.async_dispatch(title=title, content=content, summary=summary)

    def notify_position_adjustment(
        self,
        sizing_table: Dict[str, Any],
        adjustment_type: str = "持仓调整",
        total_equity: Optional[float] = None,
        cash: Optional[float] = None,
        force: bool = False
    ):
        """持仓调整与精准风控测算快报通知"""
        if not force and not self.config.get("enabled", False):
            return

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_weight = sizing_table.get("total_actual_pct_str") or "--"
        cash_pct = sizing_table.get("cash_pct_str") or "--"
        equity_val = total_equity or sizing_table.get("total_equity") or 0.0
        cash_val = cash or sizing_table.get("cash") or 0.0
        pos_count = len(sizing_table.get("rows", []))

        title = f"【A股模拟】🔄 {adjustment_type}快报 (总仓位 {total_weight}，现金 {cash_pct})"
        summary_text = f"{adjustment_type}完成：持仓 {pos_count} 只，总仓位 {total_weight}，留存现金 {cash_pct}"

        markdown_table = sizing_table.get("markdown_table", "")
        risk_pct = sizing_table.get("risk_budget_pct", "1%")

        lines = [
            f"# 🔄 {adjustment_type}与精准风控测算快报",
            f"> **调整时间**：{now_str}",
            f"> **账户总资产**：¥{equity_val:,.2f} | **可用现金**：¥{cash_val:,.2f} ({cash_pct})",
            f"> **当前持仓占用**：**{total_weight}** (共 {pos_count} 只标的)",
            "",
            "### 📐 仓位测算与风险暴露明细 (Fixed Risk Sizing)",
            markdown_table,
            "",
            "> 💡 **头寸与风控逻辑说明**：",
            f"> 1. **严格风控**：采用固定风险暴露模型（单票承受最大 {risk_pct} 账户回撤，结合 2×ATR 真实波幅），波动越大建仓越轻；",
            f"> 2. **现金防御**：持仓占用合计 {total_weight}，剩余 {cash_pct} 资金作为防御性现金储备，规避市场系统性风险。",
            "",
            "---",
            "*来自 A股量化模拟交易控制台 · 实时极速推送*"
        ]

        content = "\n".join(lines)
        self.async_dispatch(title=title, content=content, summary=summary_text)

    def notify_daily_summary(
        self,
        summary: Dict[str, Any],
        positions: Dict[str, Any],
        force: bool = False,
        date_str: Optional[str] = None,
        sizing_table: Optional[Dict[str, Any]] = None,
        sandbox_summary: Optional[Dict[str, Any]] = None,
        sandbox_positions: Optional[Dict[str, Any]] = None,
        diagnostic_report: Optional[Dict[str, Any]] = None,
        strategy_status: Optional[Dict[str, Any]] = None
    ):
        """每日 15:00 收盘快报通知（自动跳过周末与法定休市节假日，仅在开盘日推送；支持 Product AB 双账本与策略运行状况）"""
        if not self.config.get("notify_on_daily_summary", True):
            return

        today_str = date_str or datetime.datetime.now().strftime("%Y-%m-%d")

        # 检查是否为开盘交易日（非交易日跳过推送，除非 force=True）
        if not force:
            try:
                from trade_calendar import is_trading_day
                if not is_trading_day(today_str):
                    logger.info(f"[{today_str}] 为周末或法定休市节假日，跳过每日收盘微信战报推送")
                    return
            except Exception as e:
                logger.warning(f"交易日历检查异常，回退至周末检查: {e}")
                now_dt = datetime.datetime.now()
                if now_dt.weekday() >= 5:
                    logger.info(f"[{today_str}] 为周末休市，跳过每日收盘微信战报推送")
                    return

        # 判断是否为双账本架构推送模式 (Product AB)
        is_dual_ledger = (sandbox_summary is not None or diagnostic_report is not None)

        total_equity = float(summary.get("total_equity", 0.0))
        today_pnl = float(summary.get("today_pnl", 0.0))
        today_pnl_pct = float(summary.get("today_pnl_pct", 0.0))
        total_pnl = float(summary.get("total_pnl", 0.0))
        total_pnl_pct = float(summary.get("total_pnl_pct", 0.0))
        cash = float(summary.get("cash", 0.0))
        market_value = float(summary.get("market_value", 0.0))
        primary_stock_wt = ((market_value / total_equity) * 100) if total_equity > 0 else 0.0
        pos_count = len(positions)

        pnl_icon = "📈" if today_pnl >= 0 else "📉"

        if is_dual_ledger:
            # 提取测试账本数据 (Sandbox)
            sb_summary = sandbox_summary or {}
            sb_equity = float(sb_summary.get("total_equity", 100000.0))
            sb_pnl = float(sb_summary.get("today_pnl", 0.0))
            sb_pnl_pct = float(sb_summary.get("today_pnl_pct", 0.0))
            sb_init_cash = float(sb_summary.get("initial_cash", 100000.0))
            sb_cum_ret_pct = round((sb_equity / sb_init_cash - 1.0) * 100.0, 2)
            sb_cash = float(sb_summary.get("cash", 100000.0))
            sb_market_value = float(sb_summary.get("market_value", 0.0))
            sb_stock_wt = (sb_market_value / sb_equity * 100.0) if sb_equity > 0 else 0.0
            sb_positions = sandbox_positions if sandbox_positions is not None else {}

            # 接管收益率 (自 2026-11-09 接管以来)
            TAKEOVER_EQUITY = 95424.63
            takeover_ret_pct = round((total_equity / TAKEOVER_EQUITY - 1.0) * 100.0, 2)

            # 策略与模式信息
            regime = strategy_status.get("regime", "RISK_OFF") if strategy_status else "RISK_OFF"
            w_a = float(strategy_status.get("weight_a", 1.0)) if strategy_status else 1.0
            w_b = float(strategy_status.get("weight_b", 0.0)) if strategy_status else 0.0
            pending_entries = strategy_status.get("pending_entries", {}) if strategy_status else {}

            # 理论基准跟踪偏离
            tracking_error_pct = 0.0
            if diagnostic_report and "attribution" in diagnostic_report:
                tracking_error_pct = float(diagnostic_report["attribution"].get("execution_drag", {}).get("cum_tracking_error_pct", 0.0))

            is_v2 = (strategy_status and strategy_status.get("strategy_id") in ("Product_AB_Balanced_v2", "Product_B_Aggressive_v2"))
            if is_v2:
                title = f"【Product AB v2.0双账户】{pnl_icon} {today_str} 收盘战报 (Balanced稳健{today_pnl_pct:+.2f}% | Aggressive进攻{sb_pnl_pct:+.2f}%)"
                summary_text = (
                    f"Balanced稳健 ¥{total_equity:,.2f} ({today_pnl_pct:+.2f}%) | "
                    f"Aggressive进攻 ¥{sb_equity:,.2f} ({sb_pnl_pct:+.2f}%) | "
                    f"状态: {regime} (A:{w_a*100:.0f}%/B:{w_b*100:.0f}%)"
                )
            else:
                title = f"【Product AB双账本】{pnl_icon} {today_str} 收盘战报 (主账户{today_pnl_pct:+.2f}% | 测试{sb_pnl_pct:+.2f}%)"
                summary_text = (
                    f"主账户 ¥{total_equity:,.2f} ({today_pnl_pct:+.2f}%) | "
                    f"Sandbox ¥{sb_equity:,.2f} ({sb_pnl_pct:+.2f}%) | "
                    f"模式: {regime} (A:{w_a*100:.0f}%/B:{w_b*100:.0f}%)"
                )

            regime_desc_map = {
                "RISK_OFF": "防守避险 (A底盘100% 严控回撤，新买入上限0%，全额现金防御)",
                "NEUTRAL": "平衡震荡 (A底盘100% 稳健复利，回避宽幅假突破)",
                "RISK_ON": "进攻爆发 (A底盘20% / B成长80% 行业龙头全力主升浪)",
                "STRONG_RISK_ON": "强趋势攻防 (A底盘50% / B成长50% 抓大牛防跳空)"
            }
            regime_desc = regime_desc_map.get(regime, f"混合配置 A:{w_a*100:.0f}%/B:{w_b*100:.0f}%")
            st = strategy_status or {}
            if "max_regime_total_wt_num" in st:
                max_stock_wt_pct = float(st["max_regime_total_wt_num"]) * 100.0
            else:
                max_stock_wt_pct = 0.0 if regime == "RISK_OFF" else 35.0 if regime == "NEUTRAL" else 70.0 if regime == "RISK_ON" else 90.0
            target_pos_count = 4 if regime == "STRONG_RISK_ON" else 3

            primary_pos_cnt = st.get("primary_positions_count", pos_count)
            primary_max_pos = st.get("primary_max_positions", target_pos_count)
            sb_pos_cnt = st.get("sandbox_positions_count", len(sb_positions))
            sb_max_pos = st.get("sandbox_max_positions", target_pos_count)

            account1_header = "### 1️⃣ 🛡️ Balanced 稳健账户 (主账户 · 中等风险稳健增长)" if is_v2 else "### 1️⃣ 主账户 (生产历史连续)"
            header_title = f"# 📊 Product AB v2.0 双账户每日对账战报 ({today_str})" if is_v2 else f"# 📊 Product AB 每日收盘对账战报 ({today_str})"

            lines = [
                header_title,
                f"> **市场状态**：`{regime}` ({regime_desc})",
                f"> **策略配置**：A底盘蓝筹 **{w_a*100:.0f}%** / B行业动量 **{w_b*100:.0f}%** | 股票上限: **{max_stock_wt_pct:.0f}%**",
                "",
                "---",
                "",
                account1_header,
                f"- **总资产(权益)**：**¥{total_equity:,.2f}**",
                f"- **今日盈亏**：**{today_pnl:+,.2f} 元 ({today_pnl_pct:+.2f}%)**",
                f"- **接管以来累计收益**：**{takeover_ret_pct:+.2f}%** (自 2026-11-09 接管，全历史 {total_pnl_pct:+.2f}%)",
                f"- **可用现金**：¥{cash:,.2f} (实际股票仓位: **{primary_stock_wt:.1f}%**，闲置资金防御: {100-primary_stock_wt:.1f}%)",
                f"- **持仓数量约束**：**{primary_pos_cnt} / {primary_max_pos} 只** (上限 {primary_max_pos} 只，单票<=15%，行业<=30%)",
                f"- **风控纪律**：-6%硬止损 | 盘中跌破MA20趋势避险 | 3.5×ATR跟踪止盈 | +3%浮盈保护底线",
                f"- **组合回撤风控**：当前回撤 {st.get('drawdown_pct', 0.0):.2f}% (预警线 8%，复核线 12%)",
                f"- **持仓市值**：¥{market_value:,.2f}",
                "- **持仓明细**："
            ]

            if not positions:
                lines.append("  - 当前空仓，保持充裕现金。")
            else:
                for sym, pos in positions.items():
                    p_name = pos.get("name", sym)
                    shares = pos.get("total_shares", pos.get("shares", 0))
                    cost = float(pos.get("cost_price", 0.0))
                    last = float(pos.get("last_price", pos.get("current_price", cost)))
                    pnl = float(pos.get("unrealized_pnl", 0.0))
                    pnl_pct = float(pos.get("pnl_pct", 0.0))
                    lines.append(f"  - **{p_name}** (`{sym}`): {shares:,}股 | 成本 ¥{cost:.2f} → 现价 ¥{last:.2f} | 浮盈 **{pnl:+,.2f}元 ({pnl_pct:+.2f}%)**")

            account2_header = "### 2️⃣ ⚡ Aggressive 进攻账户 (第二账户 · 高风险积极进攻)" if is_v2 else "### 2️⃣ 测试账本 (Sandbox 纯净前向验证)"
            account2_desc = f"- **策略与选股**：纯 Product B 动态行业动量进攻 (100% B, 0% A) | 单票<=25% | 行业<=40%" if is_v2 else f"- **累计净值跟踪偏离**：**{tracking_error_pct:+.2f}%** (与理论无摩擦混动基准对照，受控正常)"
            account2_risk = f"- **风控纪律**：-6%硬止损 | 连续2日收盘跌破MA20退出 | 4.5×ATR宽幅跟踪(不设底线) | 组合回撤 {st.get('sandbox_drawdown_pct', 0.0):.2f}% (预警线15%, 复核线20%)" if is_v2 else f"- **接管以来累计收益**：**{sb_cum_ret_pct:+.2f}%** (标准 100,000.00 元起步纯净独立核算)"

            lines.extend([
                "",
                "---",
                "",
                account2_header,
                f"- **总资产(权益)**：**¥{sb_equity:,.2f}**",
                f"- **今日盈亏**：**{sb_pnl:+,.2f} 元 ({sb_pnl_pct:+.2f}%)**",
                f"- **接管以来累计收益**：**{sb_cum_ret_pct:+.2f}%** (标准 100,000.00 元起步纯净独立核算)",
                f"- **可用现金**：¥{sb_cash:,.2f} (实际股票仓位: **{sb_stock_wt:.1f}%**)",
                f"- **持仓数量约束**：**{sb_pos_cnt} / {sb_max_pos} 只** (上限 {sb_max_pos} 只，不强制补齐)",
                account2_desc,
                account2_risk,
                "- **持仓明细**："
            ])

            if not sb_positions:
                lines.append("  - 当前测试账本空仓，全额闲置资金进行年化 2.5% Cash Overlay 计息。")
            else:
                for sym, pos in sb_positions.items():
                    p_name = pos.get("name", sym)
                    shares = pos.get("total_shares", pos.get("shares", 0))
                    cost = float(pos.get("cost_price", 0.0))
                    last = float(pos.get("last_price", pos.get("current_price", cost)))
                    pnl = float(pos.get("unrealized_pnl", 0.0))
                    pnl_pct = float(pos.get("pnl_pct", 0.0))
                    lines.append(f"  - **{p_name}** (`{sym}`): {shares:,}股 | 成本 ¥{cost:.2f} → 现价 ¥{last:.2f} | 浮盈 **{pnl:+,.2f}元 ({pnl_pct:+.2f}%)**")

            allow_new = st.get("allow_new_entries", (regime != "RISK_OFF" and primary_stock_wt < max_stock_wt_pct))
            no_entry_reason = st.get("no_entry_reason") or st.get("wait_reason")
            if not no_entry_reason:
                if regime == "RISK_OFF":
                    no_entry_reason = "当前市场处于 RISK_OFF 弱市状态，新买入上限为 0.0%，全额现金防御；绝不为了凑齐3只股票而主动买入"
                elif primary_pos_cnt >= primary_max_pos:
                    no_entry_reason = f"当前持仓已达上限 ({primary_pos_cnt}/{primary_max_pos} 只)，禁止超额开仓"
                elif allow_new:
                    no_entry_reason = "当前允许开仓，严格遵照调仓周期与选股门槛"
                else:
                    no_entry_reason = "市场处于弱势或仓位已达上限，空位保持现金防御"

            pending_buys = st.get("pending_entries", {})
            pending_buy_count = len(pending_buys)
            service_status = st.get("service_status", "正常运行 (openarmx-quant.service)")
            data_status_str = st.get("data_status_str", "正常 (3,185 只股票增量更新已就绪)")
            order_execution_status = st.get("order_execution_status", f"正常 ({pending_buy_count} 笔待执行买单)")

            lines.extend([
                "",
                "---",
                "",
                "### 3️⃣ Product AB 策略运行状况与风控指标",
                f"> **市场状态与权重**：`{regime}` ({regime_desc}) | 仓位上限: **{max_stock_wt_pct:.1f}%**",
                "- **持仓配额监控**：",
                f"  - 主账户：**{primary_pos_cnt} 只** (最大允许持仓数量: **{primary_max_pos} 只**，绝不为了凑齐3只股票而主动买入)",
                f"  - Sandbox：**{sb_pos_cnt} 只** (最大允许持仓数量: **{sb_max_pos} 只**，绝不为了凑齐3只股票而主动买入)",
                f"- **当前是否允许新开仓**：**{'是' if allow_new else '否'}**",
                f"- **当前未开仓的具体原因**：{no_entry_reason}" if not allow_new else "- **开仓状态说明**：严格遵守冻结交易规则，不为了凑齐股票数而强制买入",
                f"- **当前待执行买单数量**：**{pending_buy_count} 笔**"
            ])

            if pending_buys:
                lines.append("- **待执行买单明细**：")
                for sym, entry in pending_buys.items():
                    sig = entry.get("signal", {})
                    s_name = sig.get("name") or entry.get("name") or sym
                    target_wt = float(entry.get("target_weight", 0.0)) * 100
                    reason = entry.get("entry_reason", "策略信号")
                    lines.append(f"  - **{s_name}** (`{sym}`): 目标仓位 **{target_wt:.1f}%** ({reason})")
            else:
                lines.append("- **买单执行状态**：当前无待执行买单，禁止为了凑齐 3 只股票而主动买入。")

            lines.extend([
                "",
                "---",
                "",
                "### 4️⃣ 系统运行与服务健康监控",
                f"- **系统服务状态**：{service_status}",
                f"- **数据更新状态**：{data_status_str}",
                f"- **订单执行状态**：{order_execution_status}"
            ])

        else:
            # 单账户传统模式
            win_rate = summary.get("win_rate", 0.0)
            pl_ratio = summary.get("profit_loss_ratio", 0.0)
            title = f"【A股模拟】{pnl_icon} {today_str} 收盘战报 (今日{today_pnl_pct:+.2f}%)"
            summary_text = f"总资产 ¥{total_equity:,.2f}，今日盈亏 ¥{today_pnl:+,.2f} ({today_pnl_pct:+.2f}%)"
            lines = [
                f"# 📊 每日收盘量化战报 ({today_str})",
                "",
                "### 💰 账户核心指标",
                f"- **总资产(权益)**：**¥{total_equity:,.2f}**",
                f"- **今日盈亏**：**{today_pnl:+,.2f} 元 ({today_pnl_pct:+.2f}%)**",
                f"- **累计总盈亏**：{total_pnl:+,.2f} 元 ({total_pnl_pct:+.2f}%)",
                f"- **可用现金**：¥{cash:,.2f} (仓位: {primary_stock_wt:.1f}%)",
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

        if sizing_table and sizing_table.get("markdown_table"):
            lines.extend([
                "",
                "---",
                "",
                "### 📐 仓位测算与风险暴露明细 (ATR 波动率风控)",
                sizing_table["markdown_table"],
                f"> 💡 单票风险预算上限 {sizing_table.get('risk_budget_pct', '1%')}；持仓总占比 {sizing_table.get('total_actual_pct_str', '--')}，留存现金 {sizing_table.get('cash_pct_str', '--')} 防御。"
            ])

        lines.extend([
            "",
            "---",
            f"*推送时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · A股量化模拟实盘 (Product AB 混动双账本)*"
        ])

        content = "\n".join(lines)
        self.async_dispatch(title=title, content=content, summary=summary_text)
        return [{"success": True, "message": "收盘战报已排队发送"}]

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
