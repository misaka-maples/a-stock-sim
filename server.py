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
import math
import inspect
import uuid
import copy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Dict, Any, Optional, Literal
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Body
from fastapi import FastAPI, HTTPException, Body, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
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
from history_data import PRESET_POOLS, normalize_symbol_for_tencent
from notifier import NotificationManager, WxPusherClient
from snapshot_manager import SnapshotManager, SNAPSHOTS_DIR
from dataclasses import asdict
from strategy_rules import MomentumConfig
from history_data import last_completed_trading_day
from trade_calendar import is_trading_day
from api_auth import install_auth
from execution import _risk_and_limits

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
        self.lock = threading.RLock()
        self.last_quotes: Dict[str, Dict[str, Any]] = {}
        self.last_step_time: Optional[datetime.datetime] = None
        self.worker_stop = threading.Event()
        self.worker_started = False
        self.worker_last_success: Optional[datetime.datetime] = None
        self.worker_last_error: Optional[str] = None
        self.worker_failures = 0
        self.invalid_quote_count = 0
        self.closing_report_pending = False
        self.execution_participation_rate = .05

ctx = ServerContext()


def validated_interval(value):
    if isinstance(value, bool):
        raise ValueError("轮询间隔必须是1至60秒之间的有限数值")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("轮询间隔必须是1至60秒之间的有限数值") from None
    if not math.isfinite(value) or not 1 <= value <= 60:
        raise ValueError("轮询间隔必须是1至60秒之间的有限数值")
    return value


def normalized_watchlist(symbols):
    if not isinstance(symbols, list) or any(not isinstance(s, str) or not StockUniverse.is_main_board(s) for s in symbols):
        raise ValueError("监控池必须是沪深主板股票代码列表")
    return list(dict.fromkeys(MarketFeed.normalize_symbol(s) for s in symbols))


def sync_execution_settings(participation_rate=None):
    if not ctx.account:
        return
    config = getattr(ctx.strategy, "config", None)
    value = config.participation_rate if config else participation_rate if participation_rate is not None else ctx.execution_participation_rate
    if not math.isfinite(float(value)) or not 0 < float(value) <= 1:
        raise ValueError("成交参与率必须在(0,1]内")
    ctx.account.participation_rate = float(value)
    ctx.execution_participation_rate = float(value)
    if ctx.engine:
        ctx.engine.interval = validated_interval(ctx.interval)
        ctx.engine.symbols = list(ctx.symbols)


def configured_strategy(strategy_id, account, watchlist, parameters):
    parameters = dict(parameters)
    if not strategy_id.startswith("Momentum_Rotation"):
        # Execution participation belongs to the broker for legacy strategies;
        # constructors without **kwargs cannot accept unrelated risk settings.
        parameters.pop("participation_rate", None)
        signature = inspect.signature(STRATEGY_REGISTRY[strategy_id].__init__)
        if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
            parameters = {k: v for k, v in parameters.items() if k in signature.parameters}
    return create_strategy(strategy_id, account=account, watchlist=watchlist, **parameters)


def persisted_settings():
    parameters = asdict(ctx.strategy.config) if hasattr(ctx.strategy, "config") else {
        key: getattr(ctx.strategy, key) for key in ("max_stock_weight", "max_positions", "stop_loss_pct")
        if hasattr(ctx.strategy, key)}
    if not hasattr(ctx.strategy, "config") and ctx.account:
        parameters["participation_rate"] = ctx.account.participation_rate
    return dict(active_strategy=ctx.strategy.name, parameters=parameters,
                strategy_active=ctx.strategy_active, symbols=list(ctx.symbols),
                interval=validated_interval(ctx.interval), updated_at=datetime.datetime.now(BEIJING).isoformat())


def persist_settings(data=None):
    data = data if data is not None else persisted_settings()
    STRATEGY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STRATEGY_CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    temporary.replace(STRATEGY_CONFIG_PATH)
    return data


def validate_snapshot_settings(snapshot):
    """Pure validation, registered before snapshot files are replaced."""
    saved = snapshot.get("strategy_state", {})
    if not isinstance(saved, dict):
        raise ValueError("快照策略配置必须为对象")
    strategy_id = saved.get("active_strategy", saved.get("current_strategy"))
    if strategy_id is not None and strategy_id not in STRATEGY_REGISTRY:
        raise ValueError("快照包含不支持的策略")
    parameters = saved.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("快照策略参数必须为对象")
    if strategy_id and strategy_id.startswith("Momentum_Rotation"):
        MomentumConfig.from_dict(parameters)
    else:
        risk_fields = {k: v for k, v in parameters.items() if k in MomentumConfig.__dataclass_fields__}
        if risk_fields:
            MomentumConfig.from_dict(risk_fields)
        if any(isinstance(v, (int, float)) and not math.isfinite(v) for v in parameters.values()):
            raise ValueError("快照策略参数必须为有限数值")
    if "interval" in saved:
        validated_interval(saved["interval"])
    if "symbols" in saved:
        normalized_watchlist(saved["symbols"])
    if "strategy_active" in saved and not isinstance(saved["strategy_active"], bool):
        raise ValueError("快照策略启停标志必须为布尔值")


def on_snapshot_restored(snapshot_data: Dict[str, Any]):
    """当系统从快照恢复时，持锁热重载内存交易引擎与账户数据"""
    with ctx.lock:
        if ctx.account:
            ctx.account.load()
            if ctx.strategy:
                saved = snapshot_data.get("strategy_state", {})
                if not saved and STRATEGY_CONFIG_PATH.exists():
                    saved = json.loads(STRATEGY_CONFIG_PATH.read_text())
                strategy_id = saved.get("active_strategy", saved.get("current_strategy", ctx.strategy.name))
                params = saved.get("parameters", asdict(ctx.strategy.config) if hasattr(ctx.strategy, "config") else {})
                ctx.symbols = normalized_watchlist(saved.get("symbols", ctx.symbols))
                ctx.interval = validated_interval(saved.get("interval", ctx.interval))
                ctx.strategy_active = saved.get("strategy_active", ctx.strategy_active)
                ctx.strategy = configured_strategy(strategy_id, ctx.account, ctx.symbols, params)
                sync_execution_settings(params.get("participation_rate"))
                ctx.last_quotes = {}
                ctx.radar_candidates = []
            if ctx.engine:
                ctx.engine.account = ctx.account
                ctx.engine.strategy = ctx.strategy
        logger.info("系统快照已热重载至内存交易引擎")

SnapshotManager.register_on_restore_callback(on_snapshot_restored)
SnapshotManager.register_restore_validator(validate_snapshot_settings)


def setup_account_trade_notification(account: SimAccount):
    """为账户挂载微信交易成交通知回调"""
    """为账户挂载微信交易成交通知与实时快照更新回调"""
    if getattr(account, "_server_trade_callback", None) is not None:
        return
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
        account._server_trade_callback = on_trade_callback


def _quote_timestamp(quote):
    value = quote.get("time")
    try:
        if isinstance(value, datetime.datetime):
            result = value
        elif isinstance(value, str) and len(value) == 14 and value.isdigit():
            result = datetime.datetime.strptime(value, "%Y%m%d%H%M%S")
        elif isinstance(value, str) and ("T" in value or " " in value):
            result = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        return result.replace(tzinfo=BEIJING) if result.tzinfo is None else result.astimezone(BEIJING)
    except (ValueError, TypeError):
        return None


def _finite_quote(quote):
    if not isinstance(quote, dict):
        return None
    result = dict(quote)
    for key in ("current", "pre_close", "open", "high", "low", "change", "change_pct", "amount_wanyuan", "volume_lots"):
        if key not in result:
            continue
        try:
            value = float(result[key])
        except (ValueError, TypeError):
            return None
        if not math.isfinite(value) or (key == "current" and value <= 0):
            return None
        result[key] = value
    if "current" not in result:
        return None
    timestamp = _quote_timestamp(result)
    if timestamp is None or result.get("pre_close", 0) <= 0:
        return None
    try:
        lower, upper = _risk_and_limits("SELL", result, Decimal(str(result["pre_close"])), timestamp.date())
        if not lower <= Decimal(str(result["current"])) <= upper:
            return None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None
    # Invalid book values must not poison either JSON responses or valuation.
    for side in ("asks", "bids"):
        if side in result:
            if not isinstance(result[side], list):
                return None
            for level in result[side]:
                if not isinstance(level, dict):
                    return None
                for key in ("price", "volume"):
                    try:
                        if not math.isfinite(float(level[key])):
                            return None
                    except (KeyError, TypeError, ValueError):
                        return None
    return result


def refresh_quote_freshness(now):
    for quote in ctx.last_quotes.values():
        timestamp = _quote_timestamp(quote)
        age = (now - timestamp).total_seconds() if timestamp else -1
        quote["fresh"] = timestamp is not None and timestamp.date() == now.date() and 0 <= age <= 120


def _close_prices_ready(now, held_symbols, fetched):
    close = now.replace(hour=15, minute=0, second=0, microsecond=0)
    for symbol in held_symbols:
        quote = _finite_quote(fetched.get(symbol))
        timestamp = _quote_timestamp(quote) if quote else None
        if timestamp is None or not close <= timestamp <= now:
            return False
    return True


def worker_step(now=None):
    """One poll, exposed for offline timing/failure tests; no sleeping here."""
    supplied_clock = now is not None
    now = now or datetime.datetime.now(BEIJING)
    with ctx.lock:
        ctx.last_step_time = now
        held = list(ctx.account.positions) if ctx.account else []
        symbols = list(ctx.symbols)
        strategy = ctx.strategy
    if strategy and hasattr(strategy, "prepare"):
        strategy.prepare(now)
        preview = strategy.preview_candidates(now) if hasattr(strategy, "preview_candidates") else strategy.latest_candidates
        with ctx.lock:
            if ctx.strategy is strategy:
                ctx.radar_candidates = preview
        symbols = [candidate["symbol"] for candidate in preview]
        with ctx.lock:
            if ctx.strategy is strategy and ctx.account and len(ctx.account.positions) < getattr(strategy, "max_positions", 0):
                # Display is capped at 50, but a valid reserve candidate beyond
                # that cap must still receive quotes. Bound extra polling and
                # skip candidates already cancelled by today's entry guards.
                intents = getattr(strategy, "state", {}).get("pending_entries", {})
                reserves = [symbol for symbol, intent in intents.items()
                            if symbol not in symbols and symbol not in ctx.account.positions
                            and intent.get("blocked_date") != now.date().isoformat()
                            and not (intent.get("target_shares") is not None
                                     and intent.get("filled_shares", 0) > 0
                                     and intent["filled_shares"] >= intent["target_shares"])]
                symbols.extend(reserves[:20])
    is_trading = MarketFeed.is_trading_time(now)
    closing = is_trading_day(now) and now.time() >= datetime.time(15)
    reported = ctx.account.strategy_state.get("__server__", {}).get("closing_report_date") if ctx.account else None
    closing_due = bool(closing and reported != now.date().isoformat() and ctx.account)
    # The final closing fetch is never skipped just because old quotes exist.
    fetch_needed = is_trading or ctx.ignore_market_hours or closing_due or not ctx.last_quotes
    fetched = MarketFeed.fetch_quotes(sorted(set(symbols) | set(held))) if fetch_needed else {}
    checked_at = now if supplied_clock else datetime.datetime.now(BEIJING)
    with ctx.lock:
        valid, invalid = {}, []
        for symbol, quote in fetched.items():
            cleaned = _finite_quote(quote)
            if cleaned is None:
                invalid.append(symbol)
            else:
                valid[symbol] = cleaned
        # Also clean previously cached quotes: no persisted NaN/Inf reaches marks.
        ctx.last_quotes = {symbol: cleaned for symbol, quote in ctx.last_quotes.items()
                           if (cleaned := _finite_quote(quote)) is not None}
        ctx.last_quotes.update(valid)
        ctx.invalid_quote_count = len(invalid)
        refresh_quote_freshness(checked_at)
        for symbol in invalid:
            if symbol in ctx.last_quotes:
                ctx.last_quotes[symbol]["fresh"] = False
        if ctx.account:
            ctx.account.update_market_prices(ctx.last_quotes, now=checked_at)
            sync_execution_settings()
        storage_healthy = ctx.account is None or getattr(ctx.account, "_persistence_error", None) is None
        if storage_healthy and ctx.strategy_active and ctx.strategy and (MarketFeed.is_trading_time(checked_at) or ctx.ignore_market_hours):
            if hasattr(ctx.strategy, "config"):
                ctx.strategy.on_tick(ctx.last_quotes, now=checked_at)
            else:
                ctx.strategy.on_tick(ctx.last_quotes)
            ctx.radar_candidates = getattr(ctx.strategy, "latest_candidates", [])
            ctx.account.update_market_prices(ctx.last_quotes, now=checked_at)
        if ctx.account:
            ctx.account.save()
        current_held = list(ctx.account.positions) if ctx.account else []
        ctx.closing_report_pending = closing_due and not _close_prices_ready(checked_at, current_held, valid)
        if closing_due and not ctx.closing_report_pending:
            day = checked_at.date().isoformat()
            # Save final marks before archiving them. Mark reporting complete
            # only after the final-price snapshot has been written successfully.
            SnapshotManager().update_daily_snapshot(date_str=day)
            ctx.account.strategy_state.setdefault("__server__", {})["closing_report_date"] = day
            ctx.account.save()
            try:
                NotificationManager().notify_daily_summary(ctx.account.get_summary(), dict(ctx.account.positions))
            except Exception:
                logger.exception("最终收盘战报通知失败；收盘快照已保存")
        ctx.worker_last_success = checked_at
        ctx.worker_last_error = getattr(ctx.account, "_persistence_error", None) if ctx.account else None


def worker_wait_seconds(now):
    try:
        interval = validated_interval(ctx.interval)
    except ValueError:
        # A damaged in-memory setting cannot kill the worker or cause a busy loop.
        interval = 3.
    delay = interval if MarketFeed.is_trading_time(now) or ctx.ignore_market_hours else 30.
    if ctx.closing_report_pending:
        delay = min(interval, 5.)
    if is_trading_day(now):
        for hour, minute, second in ((9, 30, 0), (11, 30, 1), (13, 0, 0), (14, 57, 0), (15, 0, 0), (15, 5, 0)):
            boundary = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
            remaining = (boundary - now).total_seconds()
            if remaining > 0:
                delay = min(delay, remaining)
    return max(.1, delay)


def worker_is_healthy(now):
    if not ctx.worker_thread or not ctx.worker_thread.is_alive() or not ctx.running or ctx.worker_last_error:
        return False
    if ctx.account and getattr(ctx.account, "_persistence_error", None):
        return False
    heartbeat = ctx.worker_last_success or ctx.last_step_time
    if heartbeat is None:
        return False
    try:
        interval = validated_interval(ctx.interval)
    except ValueError:
        interval = 3.
    return 0 <= (now - heartbeat).total_seconds() <= max(90., interval * 3)


def background_worker():
    logger.info("后台交易轮询线程已启动")
    try:
        while ctx.running and not ctx.worker_stop.is_set():
            try:
                worker_step()
            except Exception as exc:
                with ctx.lock:
                    ctx.worker_last_error = str(exc)
                    ctx.worker_failures += 1
                logger.exception("后台轮询发生异常")
            ctx.worker_stop.wait(worker_wait_seconds(datetime.datetime.now(BEIJING)))
    finally:
        ctx.running = False
        logger.info("后台交易轮询线程已停止")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动与关闭生命周期管理"""
    if auth_manager.enabled:
        auth_manager.initialize()
    ctx.worker_stop.clear()
    ctx.running = True
    ctx.worker_started = True
    if ctx.account:
        setup_account_trade_notification(ctx.account)
    ctx.worker_thread = threading.Thread(target=background_worker, daemon=True)
    ctx.worker_thread.start()
    logger.info("HTTP API 服务启动完成。")
    try:
        yield
    finally:
        logger.info("正在停止后台线程与服务")
        ctx.running = False
        ctx.worker_stop.set()
        if ctx.worker_thread:
            ctx.worker_thread.join(timeout=10.)
        with ctx.lock:
            if ctx.account:
                ctx.account.save()
        logger.info("模拟交易服务已停止并持久化数据")


app = FastAPI(
    title="A-Share Paper Trading System",
    description="A股实时行情与拟真模拟交易系统 HTTP 接口",
    version="2.0.0",
    lifespan=lifespan
)

auth_manager = install_auth(app, data_dir=BASE_DIR / "data", enabled=True)


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


class MomentumParameters(BaseModel):
    refill_vacancies: Optional[Any] = None
    stop_loss_mode: Optional[Literal["intraday", "daily_close"]] = None
    max_stock_weight: Optional[float] = Field(None, gt=0, le=1)
    max_positions: Optional[int] = Field(None, ge=1, le=50)
    risk_per_trade: Optional[float] = Field(None, ge=0, le=.1)
    max_sector_weight: Optional[float] = Field(None, gt=0, le=1)
    volatility_target: Optional[float] = Field(None, ge=0)
    rebalance_every: Optional[int] = Field(None, ge=1)
    rank_buffer: Optional[int] = Field(None, ge=0)
    min_holding_days: Optional[int] = Field(None, ge=0)
    cooldown_days: Optional[int] = Field(None, ge=0)
    participation_rate: Optional[float] = Field(None, gt=0, le=1)
    use_ma120: Optional[bool] = None
    use_high_filter: Optional[bool] = None
    risk_adjusted: Optional[bool] = None
    market_filter: Optional[bool] = None
    # Preserve the original JSON types for MomentumConfig's strict validation.
    max_entry_gap_down_pct: Optional[Any] = None
    max_entry_gap_up_pct: Optional[Any] = None
    max_entry_intraday_drop_pct: Optional[Any] = None
    entry_require_ma20: Optional[Any] = None


class StrategyConfigRequest(MomentumParameters):
    symbols: Optional[List[str]] = Field(None, description="更新监控标的池")
    interval: Optional[float] = Field(None, ge=1, le=60, allow_inf_nan=False, description="轮询间隔秒数 (1至60秒)")
    min_change: Optional[float] = Field(None, description="突破买入最低涨幅%")
    max_change: Optional[float] = Field(None, description="突破买入最高涨幅%")
    take_profit: Optional[float] = Field(None, description="止盈百分比%")
    stop_loss: Optional[float] = Field(None, description="止损百分比%")


class StrategySwitchRequest(MomentumParameters):
    strategy_id: str = Field(..., description="要切换的目标策略ID")
    stop_loss: Optional[float] = Field(None, description="硬止损比例 (例如 -6.0)")


class AccountResetRequest(BaseModel):
    cash: float = Field(1_000_000.0, gt=0, allow_inf_nan=False, description="重置初始资金")


class BacktestRequest(BaseModel):
    refill_vacancies: Optional[Any] = None
    stop_loss_mode: Literal["intraday", "daily_close"] = "intraday"
    start_date: str = Field("2024-01-01", description="回测起始日期 (YYYY-MM-DD)")
    end_date: Optional[str] = Field(None, description="默认上一完整交易日")
    initial_cash: float = Field(100000.0, description="初始本金")
    strategy_name: str = Field("Momentum_Rotation_Pro", description="回测策略名称")
    pool_type: str = Field("core_active", description="预设股票池 (core_active / csi300_sample)")
    symbols: Optional[List[str]] = Field(None, description="自定义股票代码列表")
    take_profit: float = Field(3.5, description="动态追踪止盈点%")
    stop_loss: float = Field(-6.0, description="硬止损线%")
    max_holding_days: int = Field(5, description="最大持仓轮动天数")
    max_positions: int = Field(3, description="最大持仓标的数")
    max_stock_weight: float = Field(.20, gt=0, le=1)
    risk_per_trade: float = Field(.01, ge=0, le=.1)
    max_sector_weight: float = Field(.35, gt=0, le=1)
    risk_adjusted: bool = False
    market_filter: bool = False
    use_current_config: bool = Field(False, description="冻结当前同名日线策略的完整参数，忽略表单覆盖")
    parameters: Optional[Dict[str, Any]] = Field(None, description="独立研究的完整 MomentumConfig 参数；不可与当前配置同时使用")
    max_entry_gap_down_pct: Optional[Any] = None
    max_entry_gap_up_pct: Optional[Any] = None
    max_entry_intraday_drop_pct: Optional[Any] = None
    entry_require_ma20: Optional[Any] = None


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
    now = datetime.datetime.now(BEIJING)
    with ctx.lock:
        risks = (ctx.strategy.risk_status(ctx.last_quotes, now=now)
                 if hasattr(ctx.strategy, "risk_status") else [])
        by_symbol = {row["symbol"]: row for row in risks}
        result = []
        for symbol, position in ctx.account.positions.items():
            row = copy.deepcopy(position)
            quote = ctx.last_quotes.get(symbol, {})
            stamp = _quote_timestamp(quote)
            age = (now-stamp).total_seconds() if stamp else -1
            fresh = bool(stamp and stamp.date() == now.date() and 0 <= age <= 120
                         and quote.get("fresh") is not False)
            risk = dict(by_symbol.get(symbol, {"status": "unavailable", "reason": "当前策略未提供风险状态"}))
            if not ctx.strategy_active:
                risk.update(status="paused", blocked_reason="自动策略已暂停，不会自动下单")
            elif getattr(ctx.account, "_persistence_error", None):
                risk.update(status="unavailable", blocked_reason="账户持久化异常，已停止自动成交")
            row.update(risk=risk,
                       quote_time=stamp.isoformat() if stamp else None,
                       quote_status="fresh" if fresh else "stale" if stamp else "unavailable")
            result.append(row)
        return result


@app.get("/api/quotes")
def get_quotes():
    """获取自选股票池/全市场共振雷达标的与持仓标的的实时盘口"""
    with ctx.lock:
        refresh_quote_freshness(datetime.datetime.now(BEIJING))
        if not ctx.last_quotes:
            # 若后台尚未拉取完全部行情，快速拉取持仓及前20只标的，避免锁内拉取数千只卡死
            quick_symbols = list(ctx.account.positions.keys()) if ctx.account else []
            if not quick_symbols and ctx.symbols:
                quick_symbols = ctx.symbols[:20]
            if quick_symbols:
                quick_quotes = MarketFeed.fetch_quotes(quick_symbols)
                ctx.last_quotes.update({s: cleaned for s, q in quick_quotes.items()
                                        if (cleaned := _finite_quote(q)) is not None})
                refresh_quote_freshness(datetime.datetime.now(BEIJING))

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
        thread_alive = bool(ctx.worker_thread and ctx.worker_thread.is_alive())
        persistence_healthy = ctx.account is None or getattr(ctx.account, "_persistence_error", None) is None
        strat_id = getattr(ctx.strategy, "name", "Momentum_Rotation")
        strat_display = getattr(ctx.strategy, "display_name", strat_id)
        if hasattr(ctx.strategy, "preview_candidates"):
            ctx.radar_candidates = ctx.strategy.preview_candidates(now)
        quality = dict(getattr(ctx.strategy, "data_status", {}))
        if hasattr(ctx.strategy, "_next_failed_retry"):
            quality["next_failed_retry_seconds"] = max(0, round(ctx.strategy._next_failed_retry-time.monotonic()))
        if hasattr(ctx.strategy, "_source_health"):
            quality["source_health"] = ctx.strategy._source_health()
        held_pos = list(ctx.account.positions) if ctx.account else []
        eligible_set = getattr(ctx.strategy, "_entry_eligible", set()) if ctx.strategy else set()
        missing_held = [s for s in held_pos if s not in eligible_set]
        quality["holding_history_ready"] = (len(missing_held) == 0)
        quality["missing_holding_symbols"] = missing_held
        allocation = (ctx.strategy.allocation_status(now=now)
                      if hasattr(ctx.strategy, "allocation_status") else {})
        if allocation and not ctx.strategy_active:
            allocation.update(can_refill=False, wait_reason="自动策略已暂停，不会自动补仓")
        elif allocation and not persistence_healthy:
            allocation.update(can_refill=False, wait_reason="账户持久化异常，已停止自动成交")
        return {
            "running": ctx.running,
            "strategy_active": ctx.strategy_active,
            "worker_alive": thread_alive,
            "worker_started": ctx.worker_started,
            "worker_healthy": worker_is_healthy(now),
            "worker_last_error": ctx.worker_last_error,
            "worker_failures": ctx.worker_failures,
            "worker_last_success": ctx.worker_last_success.isoformat() if ctx.worker_last_success else None,
            "persistence_healthy": persistence_healthy,
            "invalid_quote_count": ctx.invalid_quote_count,
            "closing_report_pending": ctx.closing_report_pending,
            "strategy_id": strat_id,
            "strategy_name": strat_id,
            "strategy_display_name": strat_display,
            "market_status": MarketFeed.get_market_status(now),
            "is_trading_day": MarketFeed.is_trading_day(now),
            "is_trading_time": MarketFeed.is_trading_time(now),
            "ignore_market_hours": ctx.ignore_market_hours,
            "interval": ctx.interval,
            "universe_count": len(ctx.symbols),
            "radar_count": len(ctx.radar_candidates),
            "candidate_count": (getattr(ctx.strategy, "data_status", {}).get("candidate_count", len(ctx.radar_candidates))
                                if ctx.radar_candidates else 0),
            "radar_preview_count": len(ctx.radar_candidates),
            "board_type": "仅限沪深主板 (10% 涨跌幅)",
            "exclude_sci_tech": True,
            "only_main_board": True,
            "symbols_sample": ctx.symbols[:10],
            "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "last_step_time": ctx.last_step_time.strftime("%Y-%m-%d %H:%M:%S") if ctx.last_step_time else None,
            "data_quality": quality,
            "strategy_parameters": asdict(ctx.strategy.config) if hasattr(ctx.strategy, "config") else {},
            "allocation_status": allocation,
            "signal_schedule": (("盘中报价触发硬止损；MA20与排名使用完整日线信号，下一交易日执行"
                                 if getattr(ctx.strategy.config, "stop_loss_mode", "daily_close") == "intraday"
                                 else "收盘确认止损、MA20与排名信号，下一交易日执行")
                                if hasattr(ctx.strategy, "config") else "旧版实验策略")
        }


@app.get("/api/history/status")
def get_history_status():
    """获取策略基于的历史数据更新情况、接口状态及不可用标的明细"""
    now = datetime.datetime.now(BEIJING)
    with ctx.lock:
        strategy = ctx.strategy
        quality = dict(getattr(strategy, "data_status", {}))
        if hasattr(strategy, "_next_failed_retry"):
            quality["next_failed_retry_seconds"] = max(0, round(strategy._next_failed_retry - time.monotonic()))
        if hasattr(strategy, "_source_health"):
            quality["source_health"] = strategy._source_health()
        held_pos = list(ctx.account.positions) if ctx.account else []
        eligible_set = getattr(strategy, "_entry_eligible", set()) if strategy else set()
        missing_held = [s for s in held_pos if s not in eligible_set]
        quality["holding_history_ready"] = (len(missing_held) == 0)
        quality["missing_holding_symbols"] = missing_held

        unavailable_symbols = []
        if strategy and hasattr(strategy, "_history_errors"):
            stock_names = getattr(strategy, "_stock_names", {}) or {}
            for sym, err in strategy._history_errors.items():
                item = dict(err)
                item["name"] = stock_names.get(sym, sym)
                unavailable_symbols.append(item)

        quotes_count = len(ctx.last_quotes)
        return {
            "strategy_id": getattr(strategy, "name", "Momentum_Rotation"),
            "strategy_name": getattr(strategy, "display_name", getattr(strategy, "name", "Momentum_Rotation")),
            "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_status": MarketFeed.get_market_status(now),
            "is_trading_time": MarketFeed.is_trading_time(now),
            "data_quality": quality,
            "unavailable_symbols": unavailable_symbols,
            "quotes_summary": {
                "active_quotes": quotes_count,
                "invalid_quotes": ctx.invalid_quote_count,
                "last_step_time": ctx.last_step_time.strftime("%Y-%m-%d %H:%M:%S") if ctx.last_step_time else None
            }
        }


@app.post("/api/history/refresh")
def refresh_history():
    """手动触发策略历史数据检测与后台更新"""
    now = datetime.datetime.now(BEIJING)
    with ctx.lock:
        strategy = ctx.strategy
    if not strategy or not hasattr(strategy, "prepare"):
        raise HTTPException(400, "当前策略不支持历史数据更新")

    with getattr(strategy, "_history_lock", threading.Lock()):
        strategy._last_attempt = 0
        strategy._next_failed_retry = 0
        if hasattr(strategy, "_symbol_retry_due"):
            strategy._symbol_retry_due.clear()
        if hasattr(strategy, "_next_industry_retry"):
            strategy._next_industry_retry = 0
        if hasattr(strategy, "_next_nonholding_refresh"):
            strategy._next_nonholding_refresh = 0

    strategy.prepare(now, synchronous=False)
    return {"success": True, "message": "已触发历史数据后台检测与更新", "refreshing": True}


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


def _catalog_symbol(value):
    """Canonical mainland main-board code, with exchange prefixes verified."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    code = value[2:] if value.startswith(("sh", "sz")) else value
    if len(code) != 6 or not code.isascii() or not code.isdigit() or not StockUniverse.is_main_board(code):
        return None
    symbol = ("sh" if code.startswith("6") else "sz") + code
    return symbol if value == code or value == symbol else None


def _stock_catalog():
    catalog = {}
    def add(symbol, name=None):
        symbol = _catalog_symbol(symbol)
        if symbol:
            record = catalog.setdefault(symbol, {"symbol": symbol, "code": symbol[-6:], "name": symbol})
            if isinstance(name, str) and name.strip():
                record["name"] = name.strip()
    # Catalog reads never probe unknown codes or change the monitoring pool.
    try:
        payload = json.loads(Path(StockUniverse.UNIVERSE_FILE).read_text(encoding="utf-8"))
        for record in payload.get("stocks", []):
            if isinstance(record, dict):
                add(record.get("symbol", record.get("code")), record.get("name"))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    with ctx.lock:
        for symbol in ctx.symbols:
            add(symbol)
        for symbol, position in (ctx.account.positions.items() if ctx.account else []):
            add(symbol, position.get("name"))
        for symbol, quote in ctx.last_quotes.items():
            if isinstance(quote, dict):
                add(symbol, quote.get("name"))
    return catalog


def _lookup_quotes(symbols):
    # Do not hold the account/strategy lock across provider IO. A failed new
    # request cannot promote a cached quote to live or reserve broker capacity.
    try:
        fetched = MarketFeed.fetch_quotes(symbols)
    except Exception:
        logger.warning("股票查询行情暂不可用")
        fetched = {}
    now = datetime.datetime.now(BEIJING)
    result = {}
    for symbol in symbols:
        raw = fetched.get(symbol) if isinstance(fetched, dict) else None
        quote = _finite_quote(raw)
        if quote and any(raw.get(key) is not None and _catalog_symbol(raw[key]) != symbol for key in ("symbol", "code")):
            quote = None
        timestamp = _quote_timestamp(quote) if quote else None
        age = (now - timestamp).total_seconds() if timestamp else None
        fresh = bool(timestamp and timestamp.date() == now.date() and 0 <= age <= 120
                     and raw.get("fresh") is not False)
        if quote:
            for side in ("bids", "asks"):
                quote[side] = [dict(level, price=float(level["price"]), volume=float(level["volume"]))
                               for level in quote.get(side, [])]
            quote.update(symbol=symbol, code=symbol[-6:], fresh=fresh)
        result[symbol] = {"quote": quote, "quote_available": quote is not None, "fresh": fresh,
                          "quote_time": timestamp.isoformat() if timestamp else None,
                          "quote_age_seconds": age,
                          "quote_status": "fresh" if fresh else "stale" if quote else "invalid" if raw is not None else "unavailable"}
    return result, now


@app.get("/api/stock/search")
def search_stock(q: str = Query(..., min_length=1, max_length=40)):
    """检索本地全市场主板目录，最多返回十只及当次查询行情。"""
    query = q.strip().lower()
    if not query or any(ord(char) < 32 for char in query):
        raise HTTPException(status_code=400, detail="请输入有效股票代码或名称")
    catalog = _stock_catalog()
    matched = [record for record in catalog.values()
               if query in record["symbol"] or query in record["code"] or query in record["name"].lower()]
    matched.sort(key=lambda record: (query not in (record["symbol"], record["code"]), record["symbol"]))
    matched = matched[:10]
    if not matched:
        raise HTTPException(status_code=404, detail="未检索到沪深主板股票")
    quotes, _ = _lookup_quotes([record["symbol"] for record in matched])
    output = []
    for record in matched:
        meta = quotes[record["symbol"]]
        item = dict(meta["quote"] or {})
        item.update(record)
        item.update({key: value for key, value in meta.items() if key != "quote"})
        output.append(item)
    return output


@app.get("/api/stock/quote")
def get_stock_quote(symbol: str = Query(..., min_length=6, max_length=8)):
    canonical = _catalog_symbol(symbol)
    if canonical is None:
        raise HTTPException(status_code=400, detail="请输入有效沪深主板股票代码")
    catalog = _stock_catalog()
    if canonical not in catalog:
        raise HTTPException(status_code=404, detail="股票不在本地主板目录中")
    quotes, now = _lookup_quotes([canonical])
    result = dict(catalog[canonical], **quotes[canonical])
    with ctx.lock:
        account = ctx.account
        position = copy.deepcopy(account.positions.get(canonical)) if account else None
        if position:
            total = position.get("total_shares", 0)
            position["available_shares"] = min(total, sum(lot.get("shares", 0) for lot in position.get("buy_lots", [])
                if str(lot.get("date", "")) < now.date().isoformat())) if account.strict_t1 else total
        storage_healthy = account is not None and getattr(account, "_persistence_error", None) is None
    quote = result["quote"]
    trading = MarketFeed.is_trading_time(now)
    quote_time = _quote_timestamp(quote) if quote else None
    executable = (storage_healthy and result["fresh"] and trading and quote_time is not None
                  and MarketFeed.is_trading_time(quote_time)
                  and quote.get("volume_lots", 0) > 0
                  and not quote.get("suspended") and not quote.get("is_suspended"))
    buy_allowed = True
    if quote:
        try:
            _risk_and_limits("BUY", quote, Decimal(str(quote["pre_close"])), now.date())
        except (ValueError, TypeError, InvalidOperation):
            buy_allowed = False
    def has_book(side):
        if not quote:
            return False
        lower, upper = _risk_and_limits("SELL", quote, Decimal(str(quote["pre_close"])), now.date())
        levels = quote.get(side, [])
        if any(level["volume"] < 0 or level["price"] < 0
               or (level["volume"] > 0 and level["price"] > 0
                   and not lower <= Decimal(str(level["price"])) <= upper) for level in levels):
            return False
        return any(level["price"] > 0 and level["volume"] > 0 for level in levels)
    available = position.get("available_shares", 0) if position else 0
    result.update(position=position, available_shares=available, market_status=MarketFeed.get_market_status(now),
                  is_trading_time=trading, can_buy=bool(executable and buy_allowed and has_book("asks")),
                  can_sell=bool(executable and available > 0 and has_book("bids")))
    return result


@app.get("/api/strategy/list")
def list_strategies():
    """获取所有可用实盘自动策略列表及当前生效状态"""
    with ctx.lock:
        current_id = getattr(ctx.strategy, "name", "Momentum_Rotation")
        current_display = getattr(ctx.strategy, "display_name", current_id)
        catalog = [
            ("Momentum_Rotation_Pro", "日线动量增强（MA120 / 高位过滤）", "20/60日动量排序、风险预算、行业限仓；支持每日空位补仓，已有持仓按周期轮动，日线信号随后交易日执行。"),
            ("Momentum_Rotation", "基础日线动量", "基础动量对照组；使用相同的数据、成交和风险框架。"),
            ("Minervini_SEPA", "趋势收缩突破实验", "借鉴SEPA思路的简化技术规则，尚未统一为日线研究模型。"),
            ("TurtleBreakout", "唐奇安通道突破实验", "20日高点突破、10日低点退出的技术规则。"),
            ("ONeil_CANSLIM", "相对强度突破实验", "简化价格规则，未实现完整CANSLIM基本面筛选。"),
            ("ShortTermResonance", "盘口量价共振实验", "旧版短线规则；不适合据日线回测推断盘口收益。"),
            ("MomentumBreakout", "价格突破实验", "旧版价格突破规则。"),
        ]
        strategies = [{"id": sid, "name": name, "description": desc,
                       "badge": "日线研究模型" if sid.startswith("Momentum_Rotation") else "旧版实验规则",
                       "recommended": False, "author": "项目规则实现",
                       "one_year_return": "待验证", "win_loss_ratio": "待验证", "max_drawdown": "待验证",
                       "performance_status": "unverified"} for sid, name, desc in catalog]
        return {
            "current_strategy": current_id,
            "active_strategy_id": current_id,
            "current_display_name": current_display,
            "strategy_active": ctx.strategy_active,
            "current_parameters": asdict(ctx.strategy.config) if hasattr(ctx.strategy, "config") else {},
            "strategies": strategies
        }


@app.post("/api/strategy/switch")
def switch_strategy(req: StrategySwitchRequest):
    """在线热切换当前实盘自动模拟策略（保留现有持仓与资金安全，无需重启）"""
    with ctx.lock:
        if req.strategy_id not in STRATEGY_REGISTRY:
            raise HTTPException(status_code=400, detail=f"不支持的策略ID: {req.strategy_id}")

        # Editing the active daily strategy must retain fields absent from the
        # small settings form (risk budget, industry cap, turnover controls).
        kwargs = (asdict(ctx.strategy.config)
                  if req.strategy_id == getattr(ctx.strategy, "name", None) and hasattr(ctx.strategy, "config")
                  else {})
        if req.max_stock_weight is not None:
            kwargs["max_stock_weight"] = req.max_stock_weight
        if req.max_positions is not None:
            kwargs["max_positions"] = req.max_positions
        if req.stop_loss is not None:
            kwargs["stop_loss_pct"] = req.stop_loss
        for key in MomentumConfig.__dataclass_fields__:
            value = getattr(req, key, None)
            if value is not None:
                kwargs[key] = value

        try:
            if req.stop_loss is not None and not -50 <= req.stop_loss < 0:
                raise ValueError("止损必须在[-50,0)范围内")
            new_strat = configured_strategy(req.strategy_id, ctx.account, ctx.symbols, kwargs)
            proposed = persisted_settings()
            proposed.update(active_strategy=req.strategy_id,
                            parameters=asdict(new_strat.config) if hasattr(new_strat, "config") else kwargs)
            if not hasattr(new_strat, "config"):
                proposed["parameters"]["participation_rate"] = req.participation_rate if req.participation_rate is not None else .05
            persist_settings(proposed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        ctx.strategy = new_strat
        sync_execution_settings(req.participation_rate if req.participation_rate is not None or hasattr(new_strat, "config") else .05)
        if ctx.engine:
            ctx.engine.strategy = new_strat
        ctx.radar_candidates = []
        display_name = getattr(new_strat, "display_name", new_strat.name)
        logger.info("自动策略切换至: %s", display_name)

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
        proposed = persisted_settings()
        proposed["strategy_active"] = req.active
        persist_settings(proposed)
        ctx.strategy_active = req.active
        status_str = "已启动" if ctx.strategy_active else "已暂停"
        logger.info("策略执行状态更改: %s", status_str)
        return {"success": True, "strategy_active": ctx.strategy_active, "message": f"自动策略{status_str}"}


@app.post("/api/strategy/config")
def update_strategy_config(req: StrategyConfigRequest):
    """动态更新策略参数与自选股列表"""
    with ctx.lock:
        try:
            symbols = normalized_watchlist(req.symbols) if req.symbols is not None else list(ctx.symbols)
            interval = validated_interval(req.interval) if req.interval is not None else validated_interval(ctx.interval)
            for key in ("min_change", "max_change", "take_profit", "stop_loss"):
                value = getattr(req, key)
                if value is not None and not math.isfinite(value):
                    raise ValueError("策略参数必须为有限数值")
            if req.stop_loss is not None and not -50 <= req.stop_loss < 0:
                raise ValueError("止损必须在[-50,0)范围内")
            proposed = persisted_settings()
            updates = {key: getattr(req, key) for key in MomentumConfig.__dataclass_fields__
                       if getattr(req, key, None) is not None}
            if req.stop_loss is not None:
                updates["stop_loss_pct"] = req.stop_loss
            if hasattr(ctx.strategy, "config"):
                parameters = asdict(MomentumConfig.from_dict(asdict(ctx.strategy.config), **updates))
            else:
                parameters = dict(proposed["parameters"])
                parameters.update({k: v for k, v in updates.items() if hasattr(ctx.strategy, k) or k == "participation_rate"})
                for field, attr in (("min_change", "min_change_pct"), ("max_change", "max_change_pct"), ("take_profit", "take_profit_pct")):
                    if getattr(req, field) is not None and hasattr(ctx.strategy, attr):
                        parameters[attr] = getattr(req, field)
            proposed.update(parameters=parameters, symbols=symbols, interval=interval)
            persist_settings(proposed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if hasattr(ctx.strategy, "set_config") and updates:
            ctx.strategy.set_config(updates)
        elif not hasattr(ctx.strategy, "config"):
            for key, value in parameters.items():
                if hasattr(ctx.strategy, key):
                    setattr(ctx.strategy, key, value)
        if req.symbols is not None:
            if hasattr(ctx.strategy, "set_watchlist"):
                ctx.strategy.set_watchlist(symbols)
            else:
                ctx.strategy.watchlist = symbols
        ctx.symbols, ctx.interval = symbols, interval
        sync_execution_settings(req.participation_rate)
        return {"success": True, "message": "策略参数已更新", "parameters": parameters,
                "symbols": symbols, "interval": interval}


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
        sync_execution_settings()
        # 获取实时行情用于市价撮合与验证
        quotes = MarketFeed.fetch_quotes([sym])
        raw_quote = quotes.get(sym)
        q = _finite_quote(raw_quote)
        if raw_quote is not None and q is None:
            return {"success": False, "status": "REJECTED", "reason": "行情包含无效或非有限价格，委托未成交"}
        quotes = {sym: q} if q is not None else {}
        ctx.account.bind_execution_quotes(quotes)
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
            res = ctx.account.execute_buy(sym, name, price, req.shares, reason=req.reason, limit_price=req.price if req.price and req.price > 0 else None)
        else:
            res = ctx.account.execute_sell(sym, price, req.shares, reason=req.reason, limit_price=req.price if req.price and req.price > 0 else None)

        return res


@app.post("/api/account/reset")
def reset_account(req: AccountResetRequest):
    """重置模拟账户资金与记录"""
    with ctx.lock:
        save_path = ctx.account.save_path
        staging = save_path.with_name(f".{save_path.name}.reset.{uuid.uuid4().hex}")
        try:
            replacement = SimAccount(initial_cash=req.cash, strict_t1=ctx.account.strict_t1,
                                     participation_rate=ctx.account.participation_rate, save_path=str(staging))
            if hasattr(ctx.strategy, "state"):
                replacement.strategy_state[ctx.strategy.name] = {
                    "cooldown": {}, "pending_exits": {}, "pending_entries": {}, "exit_signal_dates": {}}
            replacement.save()
            staging.replace(save_path)
        except OSError as exc:
            raise HTTPException(status_code=503, detail="新账户无法保存，原账户已保留") from exc
        finally:
            for temporary in (staging, staging.with_suffix(staging.suffix + ".tmp")):
                if temporary.exists():
                    temporary.unlink()
        replacement.save_path = save_path
        ctx.account = replacement
        ctx.strategy.account = replacement
        if hasattr(ctx.strategy, "state"):
            ctx.strategy.state = replacement.strategy_state[ctx.strategy.name]
        sync_execution_settings()
        setup_account_trade_notification(ctx.account)
        if ctx.engine:
            ctx.engine.account = ctx.account
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
    with ctx.lock:
        current_id = getattr(ctx.strategy, "name", None)
        current_parameters = asdict(ctx.strategy.config) if hasattr(ctx.strategy, "config") else None
        current_symbols = list(ctx.symbols)
        current_pool_count = len(current_symbols)
        industries = copy.deepcopy(getattr(ctx.strategy, "industry_map", {}))
        industry_data = copy.deepcopy(getattr(ctx.strategy, "data_status", {}).get("industry_data", {}))
    return {
        "strategies": [
            {"id": "Momentum_Rotation_Pro", "name": "日线动量增强（次日成交）"},
            {"id": "Momentum_Rotation", "name": "基础日线动量（次日成交）"},
            {"id": "Minervini_SEPA", "name": "趋势收缩突破（旧版实验）"},
            {"id": "ONeil_CANSLIM", "name": "相对强度突破（旧版实验）"},
            {"id": "TurtleBreakout", "name": "通道突破（旧版实验）"},
            {"id": "MATrendFollowing", "name": "双均线趋势（旧版实验）"},
            {"id": "ShortTermResonance", "name": "量价共振（旧版日线近似）"},
            {"id": "MomentumBreakout", "name": "价格突破（旧版实验）"}
        ],
        "pools": [
            {"id": "current_watchlist", "name": f"当前监控池快照 ({current_pool_count}只；非历史时点股票池)", "symbol_count": current_pool_count},
            {"id": "core_active", "name": "核心活跃研究池 (20只；其中16只主板)"},
            {"id": "csi300_sample", "name": "沪深300研究样本 (20只；其中18只主板)"}
        ],
        "current_strategy_id": current_id,
        "current_parameters": current_parameters,
        "momentum_defaults": {
            "Momentum_Rotation_Pro": asdict(MomentumConfig()),
            "Momentum_Rotation": asdict(MomentumConfig(use_ma120=False, use_high_filter=False)),
        },
        "current_pool_count": current_pool_count,
        "current_industry_coverage": sum(bool(industries.get(s)) for s in current_symbols),
        "industry_source": industry_data,
        "default_pool": "core_active",
        "default_use_current_config": bool(current_parameters),
        "default_start": "2024-01-01",
        "default_end": last_completed_trading_day(),
        "default_cash": 100000.0,
        "default_take_profit": 3.5,
        "default_stop_loss": -6.0,
        "default_max_holding_days": 5
    }


@app.post("/api/backtest/run")
def run_backtest_endpoint(req: BacktestRequest):
    """触发量化策略历史回测并返回绩效报告与资产曲线"""
    try:
        momentum = req.strategy_name in ("Momentum_Rotation", "Momentum_Rotation_Pro")
        supplied = req.model_fields_set if hasattr(req, "model_fields_set") else req.__fields_set__
        with ctx.lock:
            captured_at = datetime.datetime.now(BEIJING).isoformat()
            strategy_id = getattr(ctx.strategy, "name", None)
            live_parameters = asdict(ctx.strategy.config) if hasattr(ctx.strategy, "config") else None
            live_symbols = list(ctx.symbols)
            industries = copy.deepcopy(getattr(ctx.strategy, "industry_map", {}))
            industry_source = copy.deepcopy(getattr(ctx.strategy, "data_status", {}).get("industry_data", {}))
        if req.use_current_config:
            if not momentum or req.strategy_name != strategy_id or live_parameters is None:
                raise ValueError("当前配置快照仅适用于当前正在使用的同名 Momentum 日线策略")
            if req.parameters is not None:
                raise ValueError("当前配置快照不可同时传入独立研究 parameters")
            parameters = live_parameters
            parameter_source = "current_strategy_snapshot"
        elif momentum:
            values = dict(req.parameters or {})
            if req.strategy_name == "Momentum_Rotation":
                values.setdefault("use_ma120", False)
                values.setdefault("use_high_filter", False)
            # Older clients can still explicitly supply these fields. Absent
            # fields use the same full defaults as the daily strategy.
            aliases = {"stop_loss": "stop_loss_pct"}
            for key in ("stop_loss", "max_positions", "max_stock_weight", "risk_per_trade",
                        "max_sector_weight", "risk_adjusted", "market_filter", "max_entry_gap_down_pct",
                        "max_entry_gap_up_pct", "max_entry_intraday_drop_pct", "entry_require_ma20", "stop_loss_mode", "refill_vacancies"):
                if key in supplied:
                    values[aliases.get(key, key)] = getattr(req, key)
            parameters = asdict(MomentumConfig.from_dict(values))
            parameter_source = "independent_research_parameters"
        else:
            if req.parameters is not None:
                raise ValueError("parameters 仅适用于 Momentum 日线策略")
            parameters = None
            parameter_source = "legacy_form_parameters"
        if req.pool_type == "current_watchlist":
            if req.symbols is not None:
                raise ValueError("当前监控池快照不可同时传入自定义 symbols")
            symbols = live_symbols
            if not symbols:
                raise ValueError("当前监控池为空，请选择研究样本池")
        elif req.symbols is not None:
            if not req.symbols:
                raise ValueError("自定义股票池不可为空")
            symbols = [normalize_symbol_for_tencent(s) for s in req.symbols]
        else:
            if req.pool_type not in PRESET_POOLS:
                raise ValueError("不支持的回测股票池")
            symbols = list(PRESET_POOLS[req.pool_type])
        symbols = list(dict.fromkeys(symbols))
        mapped_count = sum(bool(industries.get(s)) for s in symbols)
        main_board_symbols = [s for s in symbols if StockUniverse.is_main_board(s)]
        main_board_mapped = sum(bool(industries.get(s)) for s in main_board_symbols)
        ignored = sorted(supplied & {"take_profit", "max_holding_days"}) if momentum else []
        if momentum and req.use_current_config:
            ignored += sorted(supplied & {"stop_loss", "max_positions", "max_stock_weight",
                                          "risk_per_trade", "max_sector_weight", "risk_adjusted", "market_filter",
                                          "max_entry_gap_down_pct", "max_entry_gap_up_pct",
                                          "max_entry_intraday_drop_pct", "entry_require_ma20", "stop_loss_mode", "refill_vacancies"})
        arguments = dict(
            start_date=req.start_date,
            end_date=req.end_date or last_completed_trading_day(),
            initial_cash=req.initial_cash,
            strategy_name=req.strategy_name,
            pool_type=req.pool_type,
            symbols=symbols,
        )
        if momentum:
            arguments.update(config=parameters, industry_map=industries)
        else:
            arguments.update(take_profit_trigger_pct=req.take_profit, stop_loss_pct=req.stop_loss,
                             max_holding_days=req.max_holding_days, max_positions=req.max_positions)
        # Only copies have been captured above; network/data work is outside ctx.lock.
        res = run_backtest(**arguments)
        res = dict(res)
        limitations = (["当前/固定股票池与当前行业分类不代表历史时点，结果仅供研究",
                        "未计现金分红、送股、拆并股及退市回收事件",
                        "日线MA与排名信号次日执行；盘中止损仅按日线OHLC近似，不能精确还原逐笔触发或盘口成交",
                        "从指定初始现金开始，不复刻当前账户的历史持仓与待成交订单"] if momentum else
                       ["旧版实验策略与实时执行规则存在差异，结果仅供研究"])
        if momentum and main_board_mapped < len(main_board_symbols):
            limitations.append("缺失行业分类的股票合并为未知行业，仍执行同一行业仓位上限")
        res["request_context"] = dict(
            snapshot_id=uuid.uuid4().hex, captured_at=captured_at,
            strategy_id=req.strategy_name, parameter_source=parameter_source,
            source_params=parameters if momentum else dict(take_profit=req.take_profit, stop_loss=req.stop_loss,
                                                         max_positions=req.max_positions, max_holding_days=req.max_holding_days),
            pool_source="custom_symbols" if req.symbols is not None else req.pool_type,
            pool_size=len(symbols), symbols=symbols,
            main_board_count=len(main_board_symbols),
            industry_coverage=dict(mapped=mapped_count, total=len(symbols), ratio=mapped_count/len(symbols) if symbols else 0,
                main_board_mapped=main_board_mapped, main_board_total=len(main_board_symbols),
                main_board_ratio=main_board_mapped/len(main_board_symbols) if main_board_symbols else 0),
            industry_source={"capture": "loaded_strategy_snapshot", **industry_source} if industries else dict(source="unavailable"),
            ignored_params=ignored, model_limitations=limitations)
        return {"success": True, "data": res}
    except Exception as e:
        logger.error(f"回测执行失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.get("/api/backtest/run")
def run_backtest_get(
    start: str = "2024-01-01",
    end: Optional[str] = None,
    cash: float = 100000.0,
    strategy: str = "Momentum_Rotation_Pro",
    pool: str = "core_active",
    take_profit: float = 3.5,
    stop_loss: float = -6.0,
    use_current_config: bool = False
):
    """GET 方式调用回测接口，方便快速调试"""
    return run_backtest_endpoint(BacktestRequest(start_date=start, end_date=end, initial_cash=cash,
        strategy_name=strategy, pool_type=pool, take_profit=take_profit, stop_loss=stop_loss,
        use_current_config=use_current_config))


@app.get("/health")
def health_check():
    """服务健康检查接口"""
    alive = bool(ctx.worker_thread and ctx.worker_thread.is_alive())
    storage = ctx.account is None or getattr(ctx.account, "_persistence_error", None) is None
    healthy = storage and (not ctx.worker_started or worker_is_healthy(datetime.datetime.now(BEIJING)))
    return JSONResponse(status_code=200 if healthy else 503,
                        content={"status": "ok" if healthy else "degraded", "worker_alive": alive,
                                 "storage_healthy": storage, "time": datetime.datetime.now(BEIJING).isoformat()})


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
        return {"success": True, "snapshot": SnapshotManager.public_view(snap)}
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
        with ctx.lock:
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
            with ctx.lock:
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
    parser.add_argument("--interval", type=validated_interval, default=None, help="策略轮询秒数 (1至60秒，默认读取保存配置或3秒)")
    parser.add_argument("--cash", type=float, default=100_000.0, help="初始资金 (默认 10 万元)")
    parser.add_argument("--reset", action="store_true", help="清空历史持仓与交易记录，重新初始化账户")
    parser.add_argument("--test", action="store_true", help="测试模式: 忽略交易时段限制")
    parser.add_argument("--no-t1", action="store_true", help="关闭 T+1 交易制度 (允许当日卖出)")
    parser.add_argument("--purge-non-main", action="store_true", help="启动时剔除非沪深纯主板的持仓与历史收益，重新平衡现金")
    parser.add_argument("--account-file", type=str, default="sim_account.json", help="账户存储路径")

    args = parser.parse_args()

    saved_cfg = {}
    if STRATEGY_CONFIG_PATH.exists():
        saved_cfg = json.loads(STRATEGY_CONFIG_PATH.read_text())
        validate_snapshot_settings({"strategy_state": saved_cfg})

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
    elif "symbols" in saved_cfg:
        symbols = normalized_watchlist(saved_cfg["symbols"])
        logger.info("读取保存的监控池，共%s只沪深主板标的", len(symbols))
    else:
        symbols = StockUniverse.load_universe(only_main_board=True)
        logger.info(f"已全量加载全A股监控池（严格仅限沪深主板），共监控 {len(symbols)} 只标的。")

    # 初始化上下文
    ctx.symbols = symbols
    ctx.interval = validated_interval(args.interval if args.interval is not None else saved_cfg.get("interval", 3.))
    ctx.ignore_market_hours = args.test

    ctx.account = SimAccount(
        initial_cash=args.cash,
        strict_t1=(not args.no_t1),
        save_path=str(account_file_path)
    )
    if args.purge_non_main:
        purge_res = ctx.account.purge_non_main_board()
        logger.info(f"已剥离历史非沪深主板资产与收益: {purge_res}")

    active_strat_id = saved_cfg.get("active_strategy", saved_cfg.get("current_strategy", "Momentum_Rotation"))
    strat_kwargs = saved_cfg.get("parameters", {})
    ctx.strategy_active = saved_cfg.get("strategy_active", True)
    ctx.strategy = configured_strategy(active_strat_id, ctx.account, ctx.symbols, strat_kwargs)
    sync_execution_settings(strat_kwargs.get("participation_rate"))
    persist_settings()
    logger.info(f"当前生效模拟策略: {getattr(ctx.strategy, 'display_name', ctx.strategy.name)}")

    logger.info(f"启动 Web 模拟交易服务: http://{args.host}:{args.port}")
    logger.info(f"监控标的总数: {len(ctx.symbols)} 只 | 严格限制沪深主板: 是")
    logger.info(f"刷新间隔: {ctx.interval}s | 测试模式: {ctx.ignore_market_hours} | T+1: {not args.no_t1}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

