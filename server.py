#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股模拟交易 HTTP 服务端与 Web API 控制台
基于 FastAPI + Uvicorn 构建，支持长期常驻运行在 Linux 服务器后台。
"""

import sys
import os
import time
import argparse
import datetime
import threading
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# 引入核心交易系统模块
BASE_DIR = Path(__file__).resolve().parent
sys.path.extend([str(BASE_DIR), "/home/maple"])

from sim_trader import SimAccount, MarketFeed, StockUniverse, MomentumBreakoutStrategy, ShortTermResonanceStrategy, PaperTradingEngine, BEIJING
from backtest import run_backtest
from history_data import PRESET_POOLS

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


def background_worker():
    """后台定时执行交易策略的守护线程"""
    logger.info("后台交易轮询线程已启动...")
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
        return {
            "running": ctx.running,
            "strategy_active": ctx.strategy_active,
            "strategy_name": getattr(ctx.strategy, "name", "ShortTermResonance"),
            "market_status": MarketFeed.get_market_status(now),
            "is_trading_time": MarketFeed.is_trading_time(now),
            "ignore_market_hours": ctx.ignore_market_hours,
            "interval": ctx.interval,
            "universe_count": len(ctx.symbols),
            "radar_count": len(ctx.radar_candidates),
            "exclude_sci_tech": True,
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
            "exclude_rules": ["科创板 (688*)", "已退市/零价格标的"],
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
        if req.min_change is not None:
            ctx.strategy.min_change_pct = req.min_change
        if req.max_change is not None:
            ctx.strategy.max_change_pct = req.max_change
        if req.take_profit is not None:
            ctx.strategy.take_profit_pct = req.take_profit
        if req.stop_loss is not None:
            ctx.strategy.stop_loss_pct = req.stop_loss
        return {"success": True, "message": "策略参数已更新"}


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


@app.get("/api/backtest/config")
def get_backtest_config():
    """获取回测可用策略列表与预设股票池"""
    return {
        "strategies": [
            {"id": "TurtleBreakout", "name": "经典海龟交易法则 / 唐奇安通道突破策略 (大牛股趋势跟踪)"},
            {"id": "MATrendFollowing", "name": "双均线多头趋势跟踪策略 (MA20/MA60多头生命线)"},
            {"id": "ShortTermResonance", "name": "超短线量价共振起爆策略 (实时同款)"},
            {"id": "MomentumBreakout", "name": "20日高点放量突破策略 (动量突破)"}
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
# 服务主函数
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="A股模拟交易系统 HTTP 服务与 Web 控制台")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="服务端口 (默认 8000)")
    parser.add_argument("--symbols", nargs="+", default=None, help="自定义监控股票池（默认不传则启用全A股监控）")
    parser.add_argument("--full-market", action="store_true", default=True, help="启用全A股市场监控（排除科创板，默认开启）")
    parser.add_argument("--refresh-universe", action="store_true", help="强制重新探测并刷新 stock_universe.json")
    parser.add_argument("--interval", type=float, default=3.0, help="策略轮询秒数 (默认 3.0)")
    parser.add_argument("--cash", type=float, default=100_000.0, help="初始资金 (默认 10 万元)")
    parser.add_argument("--reset", action="store_true", help="清空历史持仓与交易记录，重新初始化账户")
    parser.add_argument("--test", action="store_true", help="测试模式: 忽略交易时段限制")
    parser.add_argument("--no-t1", action="store_true", help="关闭 T+1 交易制度 (允许当日卖出)")
    parser.add_argument("--account-file", type=str, default="sim_account.json", help="账户存储路径")

    args = parser.parse_args()

    account_file_path = BASE_DIR / args.account_file
    if args.reset and account_file_path.exists():
        account_file_path.unlink()
        logger.info(f"已清除历史账户存储: {account_file_path}")

    # 标的池确定 (全市场非科创板或指定自选)
    if args.refresh_universe:
        logger.info("正在全网并发探测全A股代码（排除科创板）...")
        stocks = StockUniverse.scan_and_save()
        symbols = [s["symbol"] for s in stocks]
        logger.info(f"全市场探测完成，共加载 {len(symbols)} 只非科创板标的。")
    elif args.symbols:
        symbols = [MarketFeed.normalize_symbol(s) for s in args.symbols]
        logger.info(f"使用指定监控池 ({len(symbols)} 只标的): {symbols}")
    else:
        symbols = StockUniverse.load_universe()
        logger.info(f"已全量加载全A股监控池（严格排除科创板），共监控 {len(symbols)} 只标的。")

    # 初始化上下文
    ctx.symbols = symbols
    ctx.interval = args.interval
    ctx.ignore_market_hours = args.test

    ctx.account = SimAccount(
        initial_cash=args.cash,
        strict_t1=(not args.no_t1),
        save_path=str(account_file_path)
    )

    ctx.strategy = ShortTermResonanceStrategy(
        account=ctx.account,
        watchlist=ctx.symbols
    )

    logger.info(f"启动 Web 模拟交易服务: http://{args.host}:{args.port}")
    logger.info(f"监控标的总数: {len(ctx.symbols)} 只 | 严格排除科创板: 是")
    logger.info(f"刷新间隔: {ctx.interval}s | 测试模式: {ctx.ignore_market_hours} | T+1: {not args.no_t1}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

