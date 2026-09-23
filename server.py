#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股模拟交易 HTTP 服务端与 Web API 控制台
基于 FastAPI + Uvicorn 构建，支持长期常驻运行在 Linux 服务器后台。
"""

import sys
import os
import json
import time
import argparse
import datetime
import threading
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Body, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# 引入核心交易系统模块
BASE_DIR = Path(__file__).resolve().parent
sys.path.extend([str(BASE_DIR), "/home/maple"])

from sim_trader import (
    SimAccount, MarketFeed, StockUniverse,
    MomentumBreakoutStrategy, ShortTermResonanceStrategy,
    PaperTradingEngine, BEIJING,
    create_strategy, STRATEGY_REGISTRY
)
from backtest import run_backtest
from history_data import PRESET_POOLS
from notifier import NotificationManager, WxPusherClient
from snapshot_manager import SnapshotManager, SNAPSHOTS_DIR

STRATEGY_CONFIG_PATH = BASE_DIR / "data" / "strategy_config.json"

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("sim_server")


# ==============================================================================
# 全局共享状态与后台工作线程
# ==============================================================================

class ServerContext:
    def __init__(self):
        self.account: Optional[SimAccount] = None
        self.strategy: Optional[Any] = None
        self.engine: Optional[PaperTradingEngine] = None
        self.symbols: List[str] = []
        self.radar_candidates: List[Dict[str, Any]] = []
        self.interval: float = 3.0
        self.ignore_market_hours: bool = False
        self.strategy_active: bool = True
        self.running: bool = False
        self.worker_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.last_quotes: Dict[str, Dict[str, Any]] = {}
        self.last_step_time: Optional[datetime.datetime] = None

ctx = ServerContext()


def on_snapshot_restored(snapshot_data: Dict[str, Any]):
    """当系统从快照恢复时，持锁热重载内存交易引擎与账户数据"""
    with ctx.lock:
        if ctx.account:
            ctx.account.load()
            if ctx.engine:
                ctx.engine.account = ctx.account
        logger.info("系统快照已热重载至内存交易引擎")

SnapshotManager.register_on_restore_callback(on_snapshot_restored)


def setup_account_trade_notification(account: SimAccount):
    """为账户挂载微信交易成交通知与实时快照更新回调"""
    def on_trade_callback(trade: Dict[str, Any], acc: SimAccount):
        try:
            summary = acc.get_summary() if acc else {}
            NotificationManager().notify_trade(
                trade=trade,
                total_equity=summary.get("total_equity"),
                cash=summary.get("cash")
            )
        except Exception as e:
            logger.error(f"微信交易推送执行异常: {e}")

        try:
            # 交易发生后自动同步当日最新流水快照
            SnapshotManager().update_daily_snapshot()
        except Exception as e:
            logger.error(f"成交快照同步异常: {e}")

    if hasattr(account, "register_trade_callback"):
        account.register_trade_callback(on_trade_callback)


def background_worker():
    """后台定时执行交易策略的守护线程"""
    logger.info("后台交易轮询线程已启动...")
    last_daily_summary_date: Optional[str] = None

    while ctx.running:
        try:
            now = datetime.datetime.now(BEIJING)
            is_trading = MarketFeed.is_trading_time(now)

            # 1. 快速读取当前账户持仓与监控标的代码 (持锁微秒级)
            with ctx.lock:
                ctx.last_step_time = now
                held_symbols = list(ctx.account.positions.keys()) if ctx.account else []
                symbols_to_monitor = list(ctx.symbols)
                strategy_active = ctx.strategy_active

            # 每日 15:00~15:10 收盘快报推送与每日快照归档 (交易日执行一次)
            today_str = now.strftime("%Y-%m-%d")
            is_closing_time = (datetime.time(15, 0) <= now.time() <= datetime.time(15, 10))
            if is_closing_time and last_daily_summary_date != today_str and now.weekday() < 5:
                last_daily_summary_date = today_str
                with ctx.lock:
                    if ctx.account:
                        try:
                            summary = ctx.account.get_summary()
                            positions = dict(ctx.account.positions)
                            NotificationManager().notify_daily_summary(summary, positions)
                        except Exception as e:
                            logger.error(f"每日收盘微信战报推送失败: {e}")

                        try:
                            SnapshotManager().update_daily_snapshot(date_str=today_str)
                            logger.info(f"每日收盘系统快照 [daily_{today_str}] 已自动归档")
                        except Exception as e:
                            logger.error(f"每日收盘系统快照归档异常: {e}")

            # 2. 休市降频保护：
            # 若处于收盘/休市时段（且非忽略时段测试模式），且已有今日最新收盘行情，
            # 无需每3秒全量抓取5000+标的，降频为30秒轮询，极大降低CPU与外部接口压力
            if not is_trading and not ctx.ignore_market_hours and ctx.last_quotes:
                for _ in range(30):
                    if not ctx.running:
                        break
                    time.sleep(1.0)
                continue

            # 3. 在锁外执行全市场/自选池实时行情并发拉取 (避免网络IO阻塞Web接口)
            all_symbols = sorted(list(set(symbols_to_monitor) | set(held_symbols)))
            quotes = MarketFeed.fetch_quotes(all_symbols)

            # 4. 拿到行情后，加锁更新内存数据、结算并触发策略
            with ctx.lock:
                if quotes:
                    ctx.last_quotes.update(quotes)

                # 价格更新与市值结算
                if ctx.account:
                    ctx.account.update_market_prices(ctx.last_quotes)

                # 若开启自动策略且处于交易时间（或测试模式），触发策略信号
                if strategy_active and (is_trading or ctx.ignore_market_hours) and ctx.strategy:
                    ctx.strategy.on_tick(ctx.last_quotes)
                    ctx.radar_candidates = getattr(ctx.strategy, "latest_candidates", [])
                    ctx.account.update_market_prices(ctx.last_quotes)

                if ctx.account:
                    ctx.account.save()

        except Exception as e:
            logger.error(f"后台轮询发生异常: {e}", exc_info=True)

        time.sleep(ctx.interval)
    logger.info("后台交易轮询线程已停止。")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动与关闭生命周期管理"""
    ctx.running = True
    if ctx.account:
        setup_account_trade_notification(ctx.account)
    ctx.worker_thread = threading.Thread(target=background_worker, daemon=True)
    ctx.worker_thread.start()
    logger.info("HTTP API 服务启动完成。")
    yield
    logger.info("正在停止后台线程与服务...")
    ctx.running = False
    if ctx.worker_thread:
        ctx.worker_thread.join(timeout=3.0)
    with ctx.lock:
        if ctx.account:
            ctx.account.save()
    logger.info("模拟交易服务已安全停止并持久化数据。")


app = FastAPI(
    title="A-Share Paper Trading System",
    description="A股实时行情与拟真模拟交易系统 HTTP 接口",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==============================================================================
# Pydantic 接口请求模型
# ==============================================================================

class OrderRequest(BaseModel):
    symbol: str = Field(..., description="股票代码或名称 (如 600519 或 sz002594)")
    side: str = Field(..., description="买卖方向: 'BUY' 或 'SELL'")
    shares: int = Field(..., description="委托股数 (买入需整手)")
    price: Optional[float] = Field(0.0, description="委托价格 (0 代表盘口市价撮合)")
    reason: Optional[str] = Field("Web手动下单", description="下单说明")


class StrategyToggleRequest(BaseModel):
    active: bool = Field(..., description="是否启用自动策略执行")


class StrategyConfigRequest(BaseModel):
    symbols: Optional[List[str]] = Field(None, description="更新监控标的池")
    interval: Optional[float] = Field(None, description="轮询间隔秒数")
    min_change: Optional[float] = Field(None, description="突破买入最低涨幅%")
    max_change: Optional[float] = Field(None, description="突破买入最高涨幅%")
    take_profit: Optional[float] = Field(None, description="止盈百分比%")
    stop_loss: Optional[float] = Field(None, description="止损百分比%")


class StrategySwitchRequest(BaseModel):
    strategy_id: str = Field(..., description="要切换的目标策略ID")
    max_stock_weight: Optional[float] = Field(None, description="单票仓位上限 (例如 0.33)")
    max_positions: Optional[int] = Field(None, description="最大持仓数量 (例如 3)")
    stop_loss: Optional[float] = Field(None, description="硬止损比例 (例如 -6.0)")


class AccountResetRequest(BaseModel):
    cash: Optional[float] = Field(1_000_000.0, description="重置初始资金")


class BacktestRequest(BaseModel):
    start_date: str = Field("2024-01-01", description="回测起始日期 (YYYY-MM-DD)")
    end_date: str = Field("2026-09-15", description="回测截止日期 (YYYY-MM-DD)")
    initial_cash: float = Field(100000.0, description="初始本金")
    strategy_name: str = Field("ShortTermResonance", description="回测策略名称")
    pool_type: str = Field("core_active", description="预设股票池 (core_active / csi300_sample)")
    symbols: Optional[List[str]] = Field(None, description="自定义股票代码列表")
    take_profit: float = Field(3.5, description="动态追踪止盈点%")
    stop_loss: float = Field(-2.5, description="硬止损线%")
    max_holding_days: int = Field(5, description="最大持仓轮动天数")
    max_positions: int = Field(3, description="最大持仓标的数")


class NotificationConfigRequest(BaseModel):
    enabled: bool = Field(..., description="是否启用微信推送")
    provider: str = Field("wxpusher", description="推送渠道: wxpusher | serverchan | both")
    wxpusher_app_token: Optional[str] = Field(None, description="WxPusher AppToken")
    wxpusher_uids: Optional[List[str]] = Field(None, description="WxPusher 接收者 UID 列表")
    wxpusher_spt: Optional[str] = Field(None, description="WxPusher 极简推送 SPT")
    serverchan_sendkey: Optional[str] = Field(None, description="Server酱 SendKey")
    notify_on_buy: Optional[bool] = Field(True, description="是否推送买入成交")
    notify_on_sell: Optional[bool] = Field(True, description="是否推送卖出成交")
    notify_on_daily_summary: Optional[bool] = Field(True, description="是否推送每日收盘战报")
    notify_on_risk_alert: Optional[bool] = Field(True, description="是否推送风控预警")


class WxPusherQrcodeRequest(BaseModel):
    app_token: Optional[str] = Field(None, description="WxPusher AppToken (若不传则尝试读取已配置的 Token)")


# ==============================================================================
# HTTP API 路由实现
# ==============================================================================

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index_page():
    """返回 Web 仪表盘控制台页面"""
    html_path = BASE_DIR / "web" / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Web 页面模板不存在")
    return FileResponse(html_path)


@app.get("/api/summary")
def get_summary():
    """获取账户整体资金、市值与盈亏概况"""
    with ctx.lock:
        return ctx.account.get_summary()


@app.get("/api/performance")
def get_performance():
    """获取详尽的量化收益统计、收益率曲线数据、个股盈亏归因与每日盈亏明细"""
    with ctx.lock:
        if ctx.account:
            return ctx.account.get_performance_metrics()
        return {
            "summary": {},
            "equity_curve": [],
            "symbol_stats": [],
            "daily_stats": []
        }


@app.get("/api/positions")
def get_positions():
    """获取当前所有持仓标的明细"""
    with ctx.lock:
        return list(ctx.account.positions.values())


@app.get("/api/quotes")
def get_quotes():
    """获取自选股票池/全市场共振雷达标的与持仓标的的实时盘口"""
    with ctx.lock:
        if not ctx.last_quotes:
            # 若后台尚未拉取完全部行情，快速拉取持仓及前20只标的，避免锁内拉取数千只卡死
            quick_symbols = list(ctx.account.positions.keys()) if ctx.account else []
            if not quick_symbols and ctx.symbols:
                quick_symbols = ctx.symbols[:20]
            if quick_symbols:
                quick_quotes = MarketFeed.fetch_quotes(quick_symbols)
                ctx.last_quotes.update(quick_quotes)

        # 优先展示：当前持仓标的 + 策略扫描出的全市场起爆共振雷达标的 (Top 35)
        res = {}
        # 1. 持仓股票
        for sym in ctx.account.positions.keys():
            if sym in ctx.last_quotes:
                res[sym] = ctx.last_quotes[sym]

        # 2. 共振雷达标的
        for c in ctx.radar_candidates:
            sym = c["symbol"]
            if sym not in res and sym in ctx.last_quotes:
                item = dict(ctx.last_quotes[sym])
                item["score"] = c.get("score", 0.0)
                item["bid_ask_ratio"] = c.get("bid_ask_ratio", 0.0)
                res[sym] = item
            if len(res) >= 35:
                break

        # 3. 若雷达标的较少（如盘前或刚开盘），补充涨跌幅居前的活跃标的
        if len(res) < 15 and ctx.last_quotes:
            sorted_by_change = sorted(
                ctx.last_quotes.values(),
                key=lambda x: abs(x.get("change_pct", 0.0)),
                reverse=True
            )
            for q in sorted_by_change:
                sym = q["symbol"]
                if sym not in res:
                    res[sym] = q
                if len(res) >= 20:
                    break

        return res


@app.get("/api/trades")
def get_trades(limit: int = 50):
    """获取历史成交明细"""
    with ctx.lock:
        return ctx.account.trades[-limit:]


@app.get("/api/orders")
def get_orders(limit: int = 50):
    """获取委托订单记录"""
    with ctx.lock:
        return ctx.account.orders[-limit:]


@app.get("/api/status")
def get_status():
    """获取系统运行状态、时段与参数信息"""
    now = datetime.datetime.now(BEIJING)
    with ctx.lock:
        strat_id = getattr(ctx.strategy, "name", "Momentum_Rotation")
        strat_display = getattr(ctx.strategy, "display_name", strat_id)
        return {
            "running": ctx.running,
            "strategy_active": ctx.strategy_active,
            "strategy_id": strat_id,
            "strategy_name": strat_id,
            "strategy_display_name": strat_display,
            "market_status": MarketFeed.get_market_status(now),
            "is_trading_time": MarketFeed.is_trading_time(now),
            "ignore_market_hours": ctx.ignore_market_hours,
            "interval": ctx.interval,
            "universe_count": len(ctx.symbols),
            "radar_count": len(ctx.radar_candidates),
            "board_type": "仅限沪深主板 (10% 涨跌幅)",
            "exclude_sci_tech": True,
            "only_main_board": True,
            "symbols_sample": ctx.symbols[:10],
            "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "last_step_time": ctx.last_step_time.strftime("%Y-%m-%d %H:%M:%S") if ctx.last_step_time else None
        }


@app.get("/api/universe")
def get_universe_info():
    """获取股票监控池概况"""
    with ctx.lock:
        return {
            "total_count": len(ctx.symbols),
            "board_type": "仅限沪深主板 (10% 涨跌幅限制)",
            "exclude_rules": ["创业板 (300*/301*)", "科创板 (688*)", "北交所 (43*/83*/87*/92*)", "已退市/零价格标的"],
            "radar_candidates_count": len(ctx.radar_candidates),
            "is_full_market": len(ctx.symbols) > 1000
        }


@app.get("/api/stock/search")
def search_stock(q: str):
    """根据代码或名称快速检索全市场股票"""
    q_str = q.strip().lower()
    if not q_str:
        raise HTTPException(status_code=400, detail="请输入股票代码或名称")

    with ctx.lock:
        matched = []
        for sym, quote in ctx.last_quotes.items():
            if q_str == sym or q_str == quote.get("code") or q_str in quote.get("name", "").lower():
                matched.append(quote)
                if len(matched) >= 10:
                    break

        if not matched:
            sym = MarketFeed.normalize_symbol(q_str)
            fetched = MarketFeed.fetch_quotes([sym])
            if sym in fetched:
                matched.append(fetched[sym])

        if not matched:
            raise HTTPException(status_code=404, detail=f"未检索到股票: {q}")

        return matched


@app.get("/api/strategy/list")
def list_strategies():
    """获取所有可用实盘自动策略列表及当前生效状态"""
    with ctx.lock:
        current_id = getattr(ctx.strategy, "name", "Momentum_Rotation")
        current_display = getattr(ctx.strategy, "display_name", current_id)
        strategies = [
            {
                "id": "Momentum_Rotation",
                "name": "👑 截面领头羊动量轮动策略 (西蒙斯/AQR)",
                "badge": "近一年 +77.5% 冠军",
                "recommended": True,
                "description": "多因子截面动量排序，重仓全市场最强势领头羊，以 MA20 生命线动态追踪，不设提前止盈顶，让大牛股吃满数周主升浪。",
                "author": "吉姆·西蒙斯 (Jim Simons) & AQR 阿斯内斯",
                "one_year_return": "+77.51%",
                "win_loss_ratio": "4.94",
                "max_drawdown": "19.95%"
            },
            {
                "id": "Minervini_SEPA",
                "name": "🏆 米奈尔维尼 SEPA/VCP 波动率收缩起爆策略",
                "badge": "全美投资冠军",
                "recommended": False,
                "description": "严格趋势模板过滤，VCP 振幅收敛洗盘充分后放量起爆突破介入，跌破 MA20 趋势离场，大波段+30%止盈。",
                "author": "马克·米奈尔维尼 (Mark Minervini)",
                "one_year_return": "+1.16%",
                "win_loss_ratio": "4.05",
                "max_drawdown": "15.56%"
            },
            {
                "id": "TurtleBreakout",
                "name": "🐢 经典海龟交易法则 (唐奇安突破)",
                "badge": "大牛股波段王",
                "recommended": False,
                "description": "突破 20 日唐奇安通道高点介入，跌破 10 日唐奇安低点平仓，截断亏损让利润奔跑。",
                "author": "理查德·丹尼斯 (Richard Dennis)",
                "one_year_return": "-8.64%",
                "win_loss_ratio": "2.74",
                "max_drawdown": "25.98%"
            },
            {
                "id": "ONeil_CANSLIM",
                "name": "🦅 欧奈尔 CAN SLIM 相对强度领头羊突破",
                "badge": "回撤防守最佳",
                "recommended": False,
                "description": "60日相对强度大幅跑赢大盘超额，放量突破 20 日平台阻力位买入，严格控制回撤。",
                "author": "威廉·欧奈尔 (William O'Neil)",
                "one_year_return": "+0.52%",
                "win_loss_ratio": "3.31",
                "max_drawdown": "13.83%"
            },
            {
                "id": "ShortTermResonance",
                "name": "⚡ 超短线量价共振起爆策略 (日内高频)",
                "badge": "超短日内",
                "recommended": False,
                "description": "分时突破结合买卖五档委比厚度共振，短线快速止盈止损。",
                "author": "系统原创",
                "one_year_return": "-23.46%",
                "win_loss_ratio": "1.56",
                "max_drawdown": "25.72%"
            },
            {
                "id": "MomentumBreakout",
                "name": "🚀 20日高点突破动量策略",
                "badge": "基础动量",
                "recommended": False,
                "description": "突破近 20 日高点入场，硬止损止盈。",
                "author": "传统动量",
                "one_year_return": "-10.03%",
                "win_loss_ratio": "1.81",
                "max_drawdown": "14.30%"
            }
        ]
        return {
            "current_strategy": current_id,
            "active_strategy_id": current_id,
            "current_display_name": current_display,
            "strategy_active": ctx.strategy_active,
            "strategies": strategies
        }


@app.post("/api/strategy/switch")
def switch_strategy(req: StrategySwitchRequest):
    """在线热切换当前实盘自动模拟策略（保留现有持仓与资金安全，无需重启）"""
    with ctx.lock:
        if req.strategy_id not in STRATEGY_REGISTRY:
            raise HTTPException(status_code=400, detail=f"不支持的策略ID: {req.strategy_id}")

        kwargs = {}
        if req.max_stock_weight is not None:
            kwargs["max_stock_weight"] = req.max_stock_weight
        if req.max_positions is not None:
            kwargs["max_positions"] = req.max_positions
        if req.stop_loss is not None:
            kwargs["stop_loss_pct"] = req.stop_loss

        new_strat = create_strategy(
            req.strategy_id,
            account=ctx.account,
            watchlist=ctx.symbols,
            **kwargs
        )
        ctx.strategy = new_strat
        display_name = getattr(new_strat, "display_name", new_strat.name)
        logger.info(f"[自动策略热切换] 成功切换至: {display_name}")

        # 持久化策略配置
        try:
            cfg_data = {
                "active_strategy": req.strategy_id,
                "parameters": kwargs,
                "updated_at": datetime.datetime.now(BEIJING).isoformat()
            }
            STRATEGY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            STRATEGY_CONFIG_PATH.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning(f"持久化保存策略配置失败: {e}")

        return {
            "success": True,
            "strategy_id": req.strategy_id,
            "strategy_name": display_name,
            "message": f"已成功切换自动交易策略为【{display_name}】"
        }


@app.post("/api/strategy/toggle")
def toggle_strategy(req: StrategyToggleRequest):
    """开启或暂停自动策略"""
    with ctx.lock:
        ctx.strategy_active = req.active
        status_str = "已启动" if ctx.strategy_active else "已暂停"
        logger.info(f"策略执行状态更改: {status_str}")
        return {"success": True, "strategy_active": ctx.strategy_active, "message": f"自动策略{status_str}"}


@app.post("/api/strategy/config")
def update_strategy_config(req: StrategyConfigRequest):
    """动态更新策略参数与自选股列表"""
    with ctx.lock:
        if req.symbols is not None:
            ctx.symbols = [MarketFeed.normalize_symbol(s) for s in req.symbols]
            ctx.strategy.watchlist = ctx.symbols
        if req.interval is not None:
            ctx.interval = max(req.interval, 1.0)
        if req.min_change is not None and hasattr(ctx.strategy, "min_change_pct"):
            ctx.strategy.min_change_pct = req.min_change
        if req.max_change is not None and hasattr(ctx.strategy, "max_change_pct"):
            ctx.strategy.max_change_pct = req.max_change
        if req.take_profit is not None and hasattr(ctx.strategy, "take_profit_pct"):
            ctx.strategy.take_profit_pct = req.take_profit
        if req.stop_loss is not None and hasattr(ctx.strategy, "stop_loss_pct"):
            ctx.strategy.stop_loss_pct = req.stop_loss
        return {"success": True, "message": "策略参数已更新"}


# ==============================================================================
# 微信推送与消息通知 API
# ==============================================================================

@app.get("/api/notification/config")
def get_notification_config():
    """获取当前微信通知配置（包含脱敏凭证供前端安全展示）"""
    mgr = NotificationManager()
    return mgr.get_safe_config()


@app.post("/api/notification/config")
def update_notification_config(req: NotificationConfigRequest):
    """更新微信推送配置并持久化保存"""
    mgr = NotificationManager()
    update_dict = {
        "enabled": req.enabled,
        "provider": req.provider
    }
    if req.wxpusher_app_token is not None:
        if "****" not in req.wxpusher_app_token:
            update_dict["wxpusher_app_token"] = req.wxpusher_app_token.strip()
    if req.wxpusher_uids is not None:
        update_dict["wxpusher_uids"] = [u.strip() for u in req.wxpusher_uids if u.strip()]
    if req.wxpusher_spt is not None:
        if "****" not in req.wxpusher_spt:
            update_dict["wxpusher_spt"] = req.wxpusher_spt.strip()
    if req.serverchan_sendkey is not None:
        if "****" not in req.serverchan_sendkey:
            update_dict["serverchan_sendkey"] = req.serverchan_sendkey.strip()
    if req.notify_on_buy is not None:
        update_dict["notify_on_buy"] = req.notify_on_buy
    if req.notify_on_sell is not None:
        update_dict["notify_on_sell"] = req.notify_on_sell
    if req.notify_on_daily_summary is not None:
        update_dict["notify_on_daily_summary"] = req.notify_on_daily_summary
    if req.notify_on_risk_alert is not None:
        update_dict["notify_on_risk_alert"] = req.notify_on_risk_alert

    ok = mgr.save_config(update_dict)
    if not ok:
        raise HTTPException(status_code=500, detail="保存推送配置失败")
    return {"success": True, "message": "微信推送配置已保存", "config": mgr.get_safe_config()}


@app.post("/api/notification/test")
def test_notification():
    """向配置的微信渠道发送一条即时测试消息"""
    mgr = NotificationManager()
    res = mgr.send_test_message()
    return res


@app.post("/api/notification/wxpusher/qrcode")
def create_wxpusher_qrcode(req: WxPusherQrcodeRequest):
    """在网页上生成 WxPusher 微信扫码关注二维码"""
    mgr = NotificationManager()
    token = req.app_token.strip() if req.app_token else mgr.config.get("wxpusher_app_token", "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="请先输入 WxPusher AppToken")

    res = WxPusherClient.create_qrcode(app_token=token, extra="quant_sim")
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message", "生成二维码失败"))
    return res


@app.get("/api/notification/wxpusher/scan_status")
def get_wxpusher_scan_status(code: str):
    """查询二维码扫码状态；若用户已扫码授权，则自动绑定 UID 并保存"""
    if not code:
        raise HTTPException(status_code=400, detail="缺少 code 参数")

    res = WxPusherClient.query_scan_status(code)
    if res.get("success") and res.get("scanned") and res.get("uid"):
        uid = res["uid"]
        mgr = NotificationManager()
        current_uids = list(mgr.config.get("wxpusher_uids", []))
        if uid not in current_uids:
            current_uids.append(uid)
            mgr.save_config({"wxpusher_uids": current_uids})
            logger.info(f"[WxPusher] 扫码成功，已自动绑定 UID: {uid}")
        res["bound_uids"] = current_uids
    return res


@app.post("/api/notification/wxpusher/callback")
async def wxpusher_callback(payload: Dict[str, Any] = Body(...)):
    """接收 WxPusher 官方 Webhook 回调（用户扫码关注事件）"""
    logger.info(f"[WxPusher Webhook] 收到事件: {payload}")
    action = payload.get("action")
    data = payload.get("data", {})
    if action == "app_subscribe":
        uid = data.get("uid")
        if uid:
            mgr = NotificationManager()
            current_uids = list(mgr.config.get("wxpusher_uids", []))
            if uid not in current_uids:
                current_uids.append(uid)
                mgr.save_config({"wxpusher_uids": current_uids})
                logger.info(f"[WxPusher Webhook] 自动绑定新关注用户 UID: {uid}")
    return {"code": 1000, "msg": "ok"}


@app.post("/api/trade/order")
def place_order(req: OrderRequest):
    """手动买入/卖出委托撮合"""
    sym = MarketFeed.normalize_symbol(req.symbol)
    side = req.side.upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="交易方向必须为 BUY 或 SELL")

    with ctx.lock:
        # 获取实时行情用于市价撮合与验证
        quotes = MarketFeed.fetch_quotes([sym])
        q = quotes.get(sym)
        name = q.get("name", sym) if q else sym
        price = req.price or 0.0

        # 若未指定价格，按盘口撮合（买入按卖一价，卖出按买一价）
        if price <= 0:
            if not q or q.get("current", 0) <= 0:
                return {"success": False, "reason": f"未获取到标的 {sym} 的实时行情，无法市价撮合"}
            if side == "BUY":
                asks = q.get("asks", [])
                price = asks[0]["price"] if (asks and asks[0]["price"] > 0) else q["current"]
            else:
                bids = q.get("bids", [])
                price = bids[0]["price"] if (bids and bids[0]["price"] > 0) else q["current"]

        if side == "BUY":
            if not StockUniverse.is_main_board(sym):
                return {
                    "success": False,
                    "reason": f"标的 {sym} 不属于沪深主板。当前交易系统仅允许交易沪深纯主板股票（严格排除创业板 300*/301*、科创板 688*、北交所）。"
                }
            res = ctx.account.execute_buy(sym, name, price, req.shares, reason=req.reason)
        else:
            res = ctx.account.execute_sell(sym, price, req.shares, reason=req.reason)

        return res


@app.post("/api/account/reset")
def reset_account(req: AccountResetRequest):
    """重置模拟账户资金与记录"""
    with ctx.lock:
        save_path = ctx.account.save_path
        if save_path.exists():
            save_path.unlink()
        ctx.account = SimAccount(
            initial_cash=req.cash,
            strict_t1=ctx.account.strict_t1,
            save_path=str(save_path)
        )
        ctx.strategy.account = ctx.account
        logger.info(f"模拟账户已重置，初始资金: ¥{req.cash:,.2f}")
        return {"success": True, "summary": ctx.account.get_summary()}


@app.post("/api/account/purge_non_main")
def purge_non_main_board_assets():
    """剔除非沪深主板标的的所有收益、交易记录与持仓，并依据主板真实流水重算净值与可用现金"""
    with ctx.lock:
        res = ctx.account.purge_non_main_board()
        logger.info(f"已剔除非主板资产与收益: {res}")
        return res


@app.get("/api/backtest/config")
def get_backtest_config():
    """获取回测可用策略列表与预设股票池"""
    return {
        "strategies": [
            {"id": "Momentum_Rotation", "name": "👑 吉姆·西蒙斯 / AQR - 截面领头羊动量轮动策略 (+77.5% 近一年最高)"},
            {"id": "Minervini_SEPA", "name": "🏆 马克·米奈尔维尼 - SEPA/VCP 波动率收缩起爆策略 (+83.6% 全美投资冠军)"},
            {"id": "ONeil_CANSLIM", "name": "🦅 威廉·欧奈尔 - CAN SLIM 相对强度领头羊突破策略 (+62.5% 趋势宗师)"},
            {"id": "TurtleBreakout", "name": "🐢 理查德·丹尼斯 - 经典海龟交易法则 (+98.4% 趋势突破)"},
            {"id": "MATrendFollowing", "name": "📈 双均线多头趋势跟踪策略 (MA20/MA60多头生命线)"},
            {"id": "ShortTermResonance", "name": "⚡ 超短线量价共振起爆策略 (实时同款高频)"},
            {"id": "MomentumBreakout", "name": "🚀 20日高点放量突破策略 (动量突破)"}
        ],
        "pools": [
            {"id": "core_active", "name": "精选核心活跃标的池 (20只主流龙头与持仓股)"},
            {"id": "csi300_sample", "name": "沪深300指数样本池 (20只代表性蓝筹)"}
        ],
        "default_start": "2024-01-01",
        "default_end": "2026-09-15",
        "default_cash": 100000.0,
        "default_take_profit": 3.5,
        "default_stop_loss": -2.5,
        "default_max_holding_days": 5
    }


@app.post("/api/backtest/run")
def run_backtest_endpoint(req: BacktestRequest):
    """触发量化策略历史回测并返回绩效报告与资产曲线"""
    try:
        res = run_backtest(
            start_date=req.start_date,
            end_date=req.end_date,
            initial_cash=req.initial_cash,
            strategy_name=req.strategy_name,
            pool_type=req.pool_type,
            symbols=req.symbols,
            take_profit_trigger_pct=req.take_profit,
            stop_loss_pct=req.stop_loss,
            max_holding_days=req.max_holding_days,
            max_positions=req.max_positions
        )
        return {"success": True, "data": res}
    except Exception as e:
        logger.error(f"回测执行失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.get("/api/backtest/run")
def run_backtest_get(
    start: str = "2024-01-01",
    end: str = "2026-09-15",
    cash: float = 100000.0,
    strategy: str = "ShortTermResonance",
    pool: str = "core_active",
    take_profit: float = 3.5,
    stop_loss: float = -2.5
):
    """GET 方式调用回测接口，方便快速调试"""
    try:
        res = run_backtest(
            start_date=start,
            end_date=end,
            initial_cash=cash,
            strategy_name=strategy,
            pool_type=pool,
            take_profit_trigger_pct=take_profit,
            stop_loss_pct=stop_loss
        )
        return {"success": True, "data": res}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/health")
def health_check():
    """服务健康检查接口"""
    return {"status": "ok", "time": datetime.datetime.now(BEIJING).isoformat()}


# ==============================================================================
# 5. 系统快照与离线整机迁移 API (Snapshot & Migration Endpoints)
# ==============================================================================

class SnapshotCreateRequest(BaseModel):
    tag: str = Field(default="manual", description="快照标签，如 manual / daily")
    description: str = Field(default="", description="快照说明描述")


class SnapshotRestoreRequest(BaseModel):
    snapshot_id: str = Field(..., description="要恢复的快照ID")
    backup_current: bool = Field(default=True, description="恢复前是否备份当前状态")


@app.get("/api/snapshot/list")
def list_snapshots():
    """获取系统所有历史快照列表"""
    try:
        snapshots = SnapshotManager().list_snapshots()
        return {"success": True, "snapshots": snapshots}
    except Exception as e:
        logger.error(f"获取快照列表失败: {e}")
        return {"success": False, "error": str(e), "snapshots": []}


@app.post("/api/snapshot/create")
def create_snapshot(req: SnapshotCreateRequest):
    """手动创建即时系统快照"""
    try:
        with ctx.lock:
            snap = SnapshotManager().create_snapshot(
                tag=req.tag or "manual",
                description=req.description
            )
        return {"success": True, "snapshot": snap}
    except Exception as e:
        logger.error(f"创建快照失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.get("/api/snapshot/download")
def download_snapshot(id: str = Query(..., description="快照ID")):
    """下载指定快照的 .tar.gz 独立离线迁移压缩包"""
    try:
        mgr = SnapshotManager()
        archive_path = mgr.export_archive(id)
        if not archive_path.exists():
            raise HTTPException(status_code=404, detail="快照归档包生成失败")
        return FileResponse(
            path=archive_path,
            filename=archive_path.name,
            media_type="application/gzip"
        )
    except Exception as e:
        logger.error(f"下载快照失败: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/snapshot/restore")
def restore_snapshot(req: SnapshotRestoreRequest):
    """从快照库中恢复系统运行状态（含热重载）"""
    try:
        res = SnapshotManager().restore_snapshot(req.snapshot_id, backup_current=req.backup_current)
        return res
    except Exception as e:
        logger.error(f"恢复快照失败: {e}", exc_info=True)
        return {"success": False, "message": f"恢复失败: {e}"}


@app.post("/api/snapshot/upload")
async def upload_snapshot(request: Request):
    """上传离线快照文件 (.tar.gz 或 .json) 并恢复系统"""
    try:
        content_type = request.headers.get("content-type", "")
        filename = request.headers.get("x-filename", "")
        body = await request.body()
        if not body:
            return {"success": False, "message": "上传的文件内容为空"}

        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        # 根据文件名或内容自动识别是 JSON 还是 tar.gz
        is_json = False
        if filename.endswith(".json") or "json" in content_type:
            is_json = True
        else:
            try:
                json.loads(body.decode("utf-8"))
                is_json = True
            except Exception:
                is_json = False

        ext = ".json" if is_json else ".tar.gz"
        temp_upload = SNAPSHOTS_DIR / f"upload_{int(time.time())}{ext}"
        with open(temp_upload, "wb") as f:
            f.write(body)

        try:
            res = SnapshotManager().restore_snapshot(temp_upload, backup_current=True)
            return res
        finally:
            if temp_upload.exists():
                temp_upload.unlink()
    except Exception as e:
        logger.error(f"上传并恢复快照失败: {e}", exc_info=True)
        return {"success": False, "message": f"上传恢复失败: {e}"}


@app.delete("/api/snapshot/delete")
def delete_snapshot(id: str = Query(..., description="快照ID")):
    """删除指定的系统快照"""
    try:
        deleted = SnapshotManager().delete_snapshot(id)
        return {"success": deleted, "message": "快照已成功删除" if deleted else "未找到该快照"}
    except Exception as e:
        logger.error(f"删除快照失败: {e}")
        return {"success": False, "message": f"删除失败: {e}"}


# ==============================================================================
# 服务主函数
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="A股模拟交易系统 HTTP 服务与 Web 控制台")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="服务端口 (默认 8000)")
    parser.add_argument("--symbols", nargs="+", default=None, help="自定义监控股票池（默认不传则启用全A股监控）")
    parser.add_argument("--full-market", action="store_true", default=True, help="启用全A股市场监控（仅限沪深主板，默认开启）")
    parser.add_argument("--refresh-universe", action="store_true", help="强制重新探测并刷新 stock_universe.json")
    parser.add_argument("--interval", type=float, default=3.0, help="策略轮询秒数 (默认 3.0)")
    parser.add_argument("--cash", type=float, default=100_000.0, help="初始资金 (默认 10 万元)")
    parser.add_argument("--reset", action="store_true", help="清空历史持仓与交易记录，重新初始化账户")
    parser.add_argument("--test", action="store_true", help="测试模式: 忽略交易时段限制")
    parser.add_argument("--no-t1", action="store_true", help="关闭 T+1 交易制度 (允许当日卖出)")
    parser.add_argument("--purge-non-main", action="store_true", help="启动时剔除非沪深纯主板的持仓与历史收益，重新平衡现金")
    parser.add_argument("--account-file", type=str, default="sim_account.json", help="账户存储路径")

    args = parser.parse_args()

    account_file_path = BASE_DIR / args.account_file
    if args.reset and account_file_path.exists():
        account_file_path.unlink()
        logger.info(f"已清除历史账户存储: {account_file_path}")

    # 标的池确定 (仅限沪深纯主板 10% 标的)
    if args.refresh_universe:
        logger.info("正在全网并发探测全A股代码（仅限沪深纯主板）...")
        stocks = StockUniverse.scan_and_save(only_main_board=True)
        symbols = [s["symbol"] for s in stocks]
        logger.info(f"全市场探测完成，共加载 {len(symbols)} 只沪深主板标的。")
    elif args.symbols:
        symbols = [MarketFeed.normalize_symbol(s) for s in args.symbols if StockUniverse.is_main_board(s)]
        logger.info(f"使用指定监控池 ({len(symbols)} 只沪深主板标的): {symbols}")
    else:
        symbols = StockUniverse.load_universe(only_main_board=True)
        logger.info(f"已全量加载全A股监控池（严格仅限沪深主板），共监控 {len(symbols)} 只标的。")

    # 初始化上下文
    ctx.symbols = symbols
    ctx.interval = args.interval
    ctx.ignore_market_hours = args.test

    ctx.account = SimAccount(
        initial_cash=args.cash,
        strict_t1=(not args.no_t1),
        save_path=str(account_file_path)
    )
    if args.purge_non_main:
        purge_res = ctx.account.purge_non_main_board()
        logger.info(f"已剥离历史非沪深主板资产与收益: {purge_res}")

    setup_account_trade_notification(ctx.account)

    active_strat_id = "Momentum_Rotation"
    strat_kwargs = {}
    if STRATEGY_CONFIG_PATH.exists():
        try:
            saved_cfg = json.loads(STRATEGY_CONFIG_PATH.read_text())
            active_strat_id = saved_cfg.get("active_strategy", "Momentum_Rotation")
            strat_kwargs = saved_cfg.get("parameters", {})
            logger.info(f"读取到已保存的自动策略配置: {active_strat_id}")
        except Exception as e:
            logger.warning(f"读取策略配置文件失败: {e}")

    ctx.strategy = create_strategy(
        active_strat_id,
        account=ctx.account,
        watchlist=ctx.symbols,
        **strat_kwargs
    )
    logger.info(f"当前生效自动实盘策略: {getattr(ctx.strategy, 'display_name', ctx.strategy.name)}")

    logger.info(f"启动 Web 模拟交易服务: http://{args.host}:{args.port}")
    logger.info(f"监控标的总数: {len(ctx.symbols)} 只 | 严格限制沪深主板: 是")
    logger.info(f"刷新间隔: {ctx.interval}s | 测试模式: {ctx.ignore_market_hours} | T+1: {not args.no_t1}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

