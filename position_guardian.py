#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股量化交易系统 - 持仓股票实时策略评估与全息风控守护引擎 (Position Real-time Guardian)
功能：
1. 毫秒级采集当前持仓股票的实时分时、盘口报价与极值信息。
2. 维护持仓生命周期内的历史峰值状态（建仓以来的最高价、最高浮盈、回撤幅度）。
3. 动态计算实时衍生技术指标：包含当日现价的动态实时 MA20、持仓浮动盈亏等。
4. 执行 4 重全息实时防护策略规则：
   - 冲高回落动态跟踪止盈 (Trailing Take-Profit)：浮盈曾达标后，从最高点回撤达阈值时主动锁定利润；
   - 保本止损防守机制 (Breakeven Defense)：浮盈达到保本激活线后，若回落至成本线附近（覆盖佣金税费），平价保本离场；
   - 盘中跌破关键均线避险 (Real-time MA20 Breakdown)：早盘/盘中破位即刻防守，无需苦等收盘与次日；
   - 涨停板封板保护与炸板适度止盈 (Limit-up & Unseal Guard)：涨停时锁仓享受溢价，封板松动炸板时择机落袋；
   - 底线硬止损 (Hard Stop Loss)：跌破止损线（如 -6%）绝对防守。
5. 适配 A 股 T+1 交易规则（仅对 available_shares > 0 的可卖筹码触发执行，今日新买筹码仅跟踪监控）。
"""

import math
import json
import logging
import datetime
from pathlib import Path
from collections.abc import Sequence
from typing import Dict, Any, List, Optional, Tuple, Union

logger = logging.getLogger("position_guardian")


def _valid_price(val: Any) -> Optional[float]:
    try:
        if val is None or isinstance(val, bool):
            return None
        v = float(val)
        if not math.isfinite(v) or v <= 0:
            return None
        return v
    except (ValueError, TypeError):
        return None


def _get_history_bars(symbol: str, history_cache: Dict[str, Any]) -> Sequence[Any]:
    bars = history_cache.get(symbol, [])
    if isinstance(bars, dict) and "bars" in bars:
        bars = bars["bars"]
    if bars and isinstance(bars, (list, tuple, Sequence)) and not isinstance(bars, (str, bytes)):
        return bars
    # 回退到本地磁盘历史 K 线缓存（处理服务刚重启或后台异步加载期间的持仓均线保护）
    base_dirs = [
        Path("data/history_cache"),
        Path(__file__).resolve().parent / "data" / "history_cache",
    ]
    for bdir in base_dirs:
        cache_path = bdir / f"{symbol}_qfq.json"
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                file_bars = data.get("bars", [])
                if file_bars:
                    return file_bars
            except Exception:
                pass
    return []


def update_position_monitors(
    positions: Dict[str, Dict[str, Any]],
    quotes: Dict[str, Dict[str, Any]],
    history_cache: Dict[str, Any],
    monitors: Dict[str, Dict[str, Any]],
    now: Optional[datetime.datetime] = None
) -> Dict[str, Dict[str, Any]]:
    """
    更新所有持仓标的的实时全息监控状态字典。
    记录自买入以来的绝对最高价 (peak_price)、最高浮盈 (peak_profit_pct)、当日最高价等。
    """
    now = now or datetime.datetime.now()
    now_str = now.isoformat()

    # 清理已平仓标的的监控记录
    for sym in list(monitors.keys()):
        if sym not in positions:
            monitors.pop(sym, None)

    for sym, pos in positions.items():
        quote = quotes.get(sym, {})
        shares = int(pos.get("total_shares", 0))
        if shares <= 0:
            continue

        cost_price = _valid_price(pos.get("cost_price")) or 0.0
        basis_cents = pos.get("cost_basis_cents")
        if isinstance(basis_cents, (int, float)) and not isinstance(basis_cents, bool) and shares > 0:
            calc_cost = float(basis_cents) / 100.0 / shares
            if math.isfinite(calc_cost) and calc_cost > 0:
                cost_price = calc_cost

        current = _valid_price(quote.get("current")) if quote else None
        high = _valid_price(quote.get("high")) if quote else None
        low = _valid_price(quote.get("low")) if quote else None

        current_price = current if current is not None else cost_price
        quote_high = high if high is not None else current_price
        quote_low = low if low is not None else current_price

        init_peak = cost_price
        if quote_high > 0:
            init_peak = max(init_peak, quote_high)
        if current_price > 0:
            init_peak = max(init_peak, current_price)

        init_trough = cost_price
        if quote_low > 0:
            init_trough = min(cost_price, quote_low) if cost_price > 0 else quote_low

        mon = monitors.setdefault(sym, {
            "symbol": sym,
            "cost_price": cost_price,
            "buy_date": min((lot.get("date", "") for lot in pos.get("buy_lots", [])), default=now.strftime("%Y-%m-%d")),
            "peak_price": init_peak,
            "trough_price": init_trough,
            "peak_profit_pct": 0.0,
            "current_profit_pct": 0.0,
            "drawdown_from_peak_pct": 0.0,
            "peak_time": now_str,
            "is_locked_limit_up": False,
            "created_at": now_str
        })

        # 检查建仓以来的历史 bars 极值（补充开盘前或跨日的历史最高价）
        buy_date = mon.get("buy_date", "")
        hist_bars = _get_history_bars(sym, history_cache)
        if hist_bars and len(hist_bars) > 0:
            for b in hist_bars:
                b_date = b.get("date", "")
                if buy_date and b_date >= buy_date:
                    b_high = _valid_price(b.get("high"))
                    if b_high is not None and b_high > mon["peak_price"]:
                        mon["peak_price"] = b_high
                        mon["peak_time"] = f"{b_date} 15:00:00"

        # 更新实时行情极值
        if quote_high is not None and quote_high > mon["peak_price"]:
            mon["peak_price"] = quote_high
            mon["peak_time"] = now_str
        elif current_price is not None and current_price > mon["peak_price"]:
            mon["peak_price"] = current_price
            mon["peak_time"] = now_str

        if quote_low is not None and quote_low > 0 and (mon.get("trough_price", 0) <= 0 or quote_low < mon["trough_price"]):
            mon["trough_price"] = quote_low

        # 测算浮盈与回撤百分比
        if cost_price > 0 and current_price > 0:
            mon["peak_profit_pct"] = round(((mon["peak_price"] / cost_price) - 1.0) * 100.0, 2)
            mon["current_profit_pct"] = round(((current_price / cost_price) - 1.0) * 100.0, 2)
        else:
            mon["peak_profit_pct"] = 0.0
            mon["current_profit_pct"] = 0.0

        if mon["peak_price"] > 0 and current_price > 0:
            mon["drawdown_from_peak_pct"] = round(((current_price / mon["peak_price"]) - 1.0) * 100.0, 2)
        else:
            mon["drawdown_from_peak_pct"] = 0.0

        # 判断是否封死涨停
        pre_close = _valid_price(quote.get("pre_close")) if quote else None
        limit_up = _valid_price(quote.get("limit_up")) if quote else None
        if (limit_up is None or limit_up <= 0) and pre_close and pre_close > 0:
            limit_up = round(pre_close * 1.10, 2)

        is_limit_up = False
        if limit_up and limit_up > 0 and current_price and current_price >= limit_up - 0.01:
            is_limit_up = True
        mon["is_locked_limit_up"] = is_limit_up
        mon["last_price"] = current_price
        mon["last_update"] = now_str

    return monitors


def compute_live_ma20(symbol: str, current_price: float, history_cache: Dict[str, Any]) -> Optional[float]:
    """
    结合今日实时价格与历史日 K 线收盘价，动态计算包含当日最新价的实时 MA20。
    """
    bars = _get_history_bars(symbol, history_cache)
    if not bars or len(bars) < 19:
        return None
    valid_cp = _valid_price(current_price)
    if valid_cp is None:
        return None

    recent_closes = []
    for b in bars[-19:]:
        c = _valid_price(b.get("close"))
        if c is None:
            return None
        recent_closes.append(c)

    return round((sum(recent_closes) + valid_cp) / 20.0, 3)


def compute_live_atr(symbol: str, history_cache: Dict[str, Any], period: int = 14) -> Optional[float]:
    bars = _get_history_bars(symbol, history_cache)
    if not bars or len(bars) < period + 1:
        return None
    true_ranges = []
    for i in range(len(bars) - period, len(bars)):
        hi = _valid_price(bars[i].get("high"))
        lo = _valid_price(bars[i].get("low"))
        prev_c = _valid_price(bars[i - 1].get("close"))
        if hi is None or lo is None or prev_c is None:
            return None
        true_ranges.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
    return round(sum(true_ranges) / len(true_ranges), 3)


def evaluate_position_realtime_rules(
    symbol: str,
    pos: Dict[str, Any],
    quote: Dict[str, Any],
    history_cache: Dict[str, Any],
    config: Any,
    monitor: Dict[str, Any],
    now: Optional[datetime.datetime] = None,
    available_shares: int = 0,
    validated_price: Optional[float] = None,
    is_past_buy: bool = True
) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    对单个持仓标的执行实时风控守护评估。
    必须传入经过验证的 validated_price；若行情未通过新鲜度/时效校验则跳过评估。
    返回: (exit_reason or None, guardian_info_dict)
    """
    if validated_price is None or validated_price <= 0:
        return None, {"status": "awaiting_quote", "summary": "等待120秒内有效行情报价"}

    cost_price = float(pos.get("cost_price", 0.0))
    shares = int(pos.get("total_shares", 0))
    basis_cents = pos.get("cost_basis_cents")
    if isinstance(basis_cents, (int, float)) and not isinstance(basis_cents, bool) and shares > 0:
        cost_price = float(basis_cents) / 100.0 / shares

    current_price = float(validated_price)
    if cost_price <= 0:
        return None, {"status": "awaiting_quote", "summary": "缺少有效持仓成本"}

    peak_price = max(float(monitor.get("peak_price", current_price)), current_price)
    peak_profit = float(monitor.get("peak_profit_pct", 0.0))
    current_profit = round(((current_price / cost_price) - 1.0) * 100.0, 2)
    drawdown_pct = abs(round(((current_price / peak_price) - 1.0) * 100.0, 2)) if peak_price > 0 else 0.0
    is_locked_limit_up = bool(monitor.get("is_locked_limit_up", False))

    live_ma20 = compute_live_ma20(symbol, current_price, history_cache)

    # 1. 移动止盈与保本防守参考价测算
    trailing_activation = float(getattr(config, "trailing_stop_activation_pct", 4.0) or 4.0)
    trailing_mode = getattr(config, "trailing_stop_mode", "fixed_pct")
    if trailing_mode == "atr":
        mult = float(getattr(config, "atr_trailing_multiplier", 2.0) or 2.0)
        live_atr = compute_live_atr(symbol, history_cache, period=14)
        atr_pct = (live_atr / current_price) if (live_atr and current_price > 0) else float(pos.get("atr_pct", 0.035) or 0.035)
        trailing_drop = mult * atr_pct * 100.0
    else:
        trailing_drop = float(getattr(config, "trailing_stop_drop_pct", 2.0) or 2.0)
    trailing_stop_price = round(peak_price * (1.0 - trailing_drop / 100.0), 2)
    trailing_stop_active = bool(getattr(config, "trailing_stop_enabled", True) and peak_profit >= trailing_activation)

    be_activation = float(getattr(config, "breakeven_activation_pct", 2.5) or 2.5)
    be_buffer = float(getattr(config, "breakeven_buffer_pct", 0.3) or 0.3)
    breakeven_price = round(cost_price * (1.0 + be_buffer / 100.0), 2)
    breakeven_active = bool(getattr(config, "breakeven_enabled", True) and peak_profit >= be_activation)

    hard_stop_pct = float(getattr(config, "stop_loss_pct", -6.0) or -6.0)
    hard_stop_price = round(cost_price * (1.0 + hard_stop_pct / 100.0), 2)

    # 构造实时诊断信息结构
    info = {
        "symbol": symbol,
        "cost_price": cost_price,
        "current_price": current_price,
        "peak_price": peak_price,
        "peak_profit_pct": peak_profit,
        "current_profit_pct": current_profit,
        "drawdown_pct": drawdown_pct,
        "live_ma20": live_ma20,
        "trailing_stop_active": trailing_stop_active,
        "trailing_stop_price": trailing_stop_price,
        "breakeven_active": breakeven_active,
        "breakeven_price": breakeven_price,
        "hard_stop_price": hard_stop_price,
        "is_locked_limit_up": is_locked_limit_up,
        "available_shares": available_shares
    }

    # 优先级 1：底线硬止损防守 (Hard Stop Loss - 最高优先级防空警报)
    if current_profit <= hard_stop_pct:
        reason = "盘中硬止损信号"
        info["status"] = "triggered_hard_stop"
        info["summary"] = f"⛔ 触发硬止损 (浮亏 {current_profit:.2f}% <= {hard_stop_pct:.1f}%)"
        return reason, info

    # 若为今日新建仓位（buy_day > as_of），在 A 股 T+1 约束下不执行后续盈利卖出评估
    if not is_past_buy:
        info["status"] = "t1_locked"
        info["summary"] = f"🔒 T+1 锁仓中 (现价¥{current_price:.2f}, 浮盈{'+' if current_profit>=0 else ''}{current_profit:.2f}%)"
        return None, info

    # 涨停板封板保护：若当前牢牢封死涨停，不执行卖出，锁仓享受溢价
    if is_locked_limit_up and getattr(config, "protect_limit_up", True):
        info["status"] = "limit_up_locked"
        info["summary"] = f"🚀 封死涨停板 (¥{current_price:.2f}) · 锁仓享受溢价"
        return None, info

    if not getattr(config, "enable_realtime_guardian", True):
        info["status"] = "normal_holding"
        info["summary"] = f"⚡ 实时守护已停用 (浮盈{'+' if current_profit>=0 else ''}{current_profit:.2f}%)"
        return None, info

    # 优先级 2：冲高回落动态跟踪止盈 (Trailing Stop)
    # 条件：曾冲高达到激活门槛 + 自最高点回撤达阈值 + 确保依然盈利出局
    if (getattr(config, "trailing_stop_enabled", True)
            and trailing_stop_active
            and current_price <= trailing_stop_price
            and current_profit > 0.0):
        reason = f"盘中动态跟踪止盈信号 (最高浮盈 +{peak_profit:.2f}%，自高点回落 {drawdown_pct:.2f}%，锁定浮盈 +{current_profit:.2f}%)"
        info["status"] = "triggered_trailing_stop"
        info["summary"] = f"🎯 触发跟踪止盈 (锁定浮盈 +{current_profit:.2f}%)"
        return reason, info

    # 优先级 3：保本止损防守机制 (Breakeven Defense)
    # 条件：曾有明显浮盈(>=2.5%) + 现已回落到保本缓冲线附近(<=成本+0.3%)
    if (getattr(config, "breakeven_enabled", True)
            and breakeven_active
            and current_price <= breakeven_price):
        reason = f"盘中保本止损防守信号 (曾最高浮盈 +{peak_profit:.2f}%，现已跌回成本线 ¥{cost_price:.2f} 附近，安全离场)"
        info["status"] = "triggered_breakeven"
        info["summary"] = f"🛡️ 触发保本离场 (曾浮盈 +{peak_profit:.2f}%)"
        return reason, info

    # 优先级 4：盘中跌破关键 MA20 均线避险 (Real-time MA20 Breakdown)
    # 条件：实时价格有效跌穿今日动态 MA20 超过 1.0%
    if (getattr(config, "realtime_ma20_exit", True)
            and live_ma20 is not None
            and current_price < live_ma20 * 0.99):
        reason = f"盘中跌破MA20避险信号 (现价 ¥{current_price:.2f} < 实时MA20 ¥{live_ma20:.2f})"
        info["status"] = "triggered_ma20_breakdown"
        info["summary"] = f"⚠️ 盘中跌破MA20避险 (¥{current_price:.2f} < MA20 ¥{live_ma20:.2f})"
        return reason, info

    # 未触发退出：生成当前运行状态摘要
    if trailing_stop_active:
        info["status"] = "trailing_stop_armed"
        info["summary"] = f"🎯 冲高跟踪止盈中 (最高+{peak_profit:.2f}%, 止盈线¥{trailing_stop_price:.2f})"
    elif breakeven_active:
        info["status"] = "breakeven_armed"
        info["summary"] = f"🛡️ 保本防守激活 (曾最高+{peak_profit:.2f}%, 保本线¥{breakeven_price:.2f})"
    else:
        info["status"] = "normal_holding"
        info["summary"] = f"⚡ 实时守护监控中 (现价¥{current_price:.2f}, 浮盈{'+' if current_profit>=0 else ''}{current_profit:.2f}%)"

    return None, info
