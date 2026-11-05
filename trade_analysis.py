#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股量化交易系统 - 卖出交易持仓周期 K 线绘制与盈利机会复盘引擎 (Trade Review & K-Line Analysis)
功能：
1. 自动关联匹配卖出成交与其对应的买入建仓成交，提取完整持仓周期。
2. 提取持仓期间的日 K 线行情数据（含入场前参考 K 线与出场日数据）。
3. 精准测算持仓期间的最高潜在浮盈（Peak Profit）、最大回撤（Drawdown）与逐日盈亏轨迹。
4. 使用 Matplotlib Agg 后端无头渲染高清晰度持仓 K 线复盘图（含买入点、卖出点、最高浮盈星标、MA5/MA20均线与成交量）。
5. 生成结构化 Markdown 复盘快报，供微信推送（WxPusher/Server酱）及 Web 终端弹窗展示。
"""

import os
import math
import logging
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")  # 确保无头服务器环境稳定运行
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as patches

# 配置支持中文的字体族（兼容 Linux / Ubuntu / Debian 及跨平台系统）
plt.rcParams["font.sans-serif"] = [
    "Noto Sans CJK SC",
    "Noto Serif CJK SC",
    "WenQuanYi Zen Hei",
    "AR PL UMing CN",
    "AR PL UKai CN",
    "SimHei",
    "DejaVu Sans",
    "sans-serif"
]
plt.rcParams["axes.unicode_minus"] = False

logger = logging.getLogger("trade_analysis")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CHARTS_DIR = DATA_DIR / "trade_charts"
CHARTS_DIR.mkdir(parents=True, exist_ok=True)


def find_trade_by_id(trade_id: str, account=None) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """
    根据 trade_id 查找交易记录及包含该交易的完整成交流水列表。
    优先在内存 account.trades 中查找，未找到时在持久化快照中回溯。
    """
    if not trade_id:
        return None

    # 1. 优先从当前账户对象中查找
    if account and hasattr(account, "trades"):
        for t in reversed(account.trades):
            if t.get("trade_id") == trade_id:
                return t, account.trades

    # 2. 从快照目录查找 (从最新的每日快照向前遍历)
    snapshots_dir = DATA_DIR / "snapshots"
    if snapshots_dir.exists():
        for snap_path in sorted(snapshots_dir.glob("daily_*.json"), reverse=True):
            try:
                import json
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
                acc_state = snap.get("account_state") or snap.get("account") or {}
                trades = acc_state.get("trades", [])
                for t in reversed(trades):
                    if t.get("trade_id") == trade_id:
                        return t, trades
            except Exception:
                continue

    # 3. 从 sim_account.json 文件查找
    sim_account_file = DATA_DIR / "sim_account.json"
    if sim_account_file.exists():
        try:
            import json
            data = json.loads(sim_account_file.read_text(encoding="utf-8"))
            trades = data.get("trades", [])
            for t in reversed(trades):
                if t.get("trade_id") == trade_id:
                    return t, trades
        except Exception:
            pass

    return None


def find_matching_buy_trades(sell_trade: Dict[str, Any], trades_history: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    在账户成交流水中回溯寻找与当前卖出交易对应的买入记录。
    支持多次分批买入的加权平均成本与最早买入日期提取。
    """
    symbol = sell_trade.get("symbol", "")
    sell_time = sell_trade.get("time", "")
    sell_shares = int(sell_trade.get("shares", 0))

    matching_buys = []
    # 筛选出在卖出时间之前、同标的的所有买入成交
    candidate_buys = [
        t for t in trades_history
        if t.get("symbol") == symbol and t.get("side") == "BUY" and t.get("time", "") <= sell_time
    ]

    # 从近到远倒序回溯匹配卖出股数
    needed_shares = sell_shares
    for buy in reversed(candidate_buys):
        b_shares = int(buy.get("shares", 0))
        if b_shares <= 0:
            continue
        matching_buys.append(buy)
        needed_shares -= b_shares
        if needed_shares <= 0:
            break

    # 按时间由先至后排序
    matching_buys.sort(key=lambda t: t.get("time", ""))

    if matching_buys:
        earliest_time = matching_buys[0].get("time", sell_time)
        earliest_date = earliest_time.split(" ")[0] if " " in earliest_time else earliest_time[:10]
        total_bought_shares = sum(int(t.get("shares", 0)) for t in matching_buys)
        total_cost = sum(float(t.get("price", 0.0)) * int(t.get("shares", 0)) for t in matching_buys)
        weighted_buy_price = round(total_cost / total_bought_shares, 4) if total_bought_shares > 0 else float(matching_buys[0].get("price", 0.0))
    else:
        # 回溯未找到时的优雅降级（如单表历史被截断或独立单元测试）
        sell_price = float(sell_trade.get("price", 0.0))
        realized_pnl = float(sell_trade.get("realized_pnl", 0.0))
        if sell_shares > 0 and realized_pnl != 0.0:
            inferred_buy_price = round(sell_price - (realized_pnl / sell_shares), 4)
        else:
            inferred_buy_price = sell_price
        earliest_time = sell_time
        earliest_date = sell_time.split(" ")[0] if " " in sell_time else sell_time[:10]
        weighted_buy_price = inferred_buy_price
        total_bought_shares = sell_shares

    summary = {
        "buy_time": earliest_time,
        "buy_date": earliest_date,
        "buy_price": weighted_buy_price,
        "buy_shares": total_bought_shares,
        "matching_count": len(matching_buys)
    }
    return matching_buys, summary


def get_bars_for_symbol(symbol: str, history_cache: Optional[Dict[str, Any]] = None, feed=None) -> List[Dict[str, Any]]:
    """
    从内存缓存或持久化历史数据中读取标的的完整日 K 线序列。
    """
    if history_cache and symbol in history_cache:
        cached = history_cache[symbol]
        if isinstance(cached, list):
            return cached
        if isinstance(cached, dict) and "bars" in cached:
            return cached["bars"]

    # 尝试从磁盘缓存文件读取
    cache_file = DATA_DIR / "history_cache" / f"{symbol.replace('.', '_')}_qfq.json"
    if cache_file.exists():
        try:
            import json
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "bars" in data:
                return data["bars"]
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.warning(f"读取历史行情缓存文件 {cache_file} 失败: {e}")

    # 尝试通过 feed 获取
    if feed is not None and hasattr(feed, "fetch_stock_kline"):
        try:
            _, _, bars = feed.fetch_stock_kline(symbol, use_cache=True)
            if bars:
                return bars
        except Exception as e:
            logger.warning(f"通过 feed 获取 {symbol} 行情失败: {e}")

    return []


def extract_holding_kline_data(
    sell_trade: Dict[str, Any],
    trades_history: Optional[List[Dict[str, Any]]] = None,
    history_cache: Optional[Dict[str, Any]] = None,
    feed=None,
    current_quote: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    提取持仓期间的日 K 线行情与复盘指标（最高浮盈、最大回撤、逐日轨迹等）。
    """
    trades_history = trades_history or []
    matching_buys, buy_summary = find_matching_buy_trades(sell_trade, trades_history)

    symbol = sell_trade.get("symbol", "")
    name = sell_trade.get("name", symbol)
    trade_id = sell_trade.get("trade_id", "")
    sell_price = float(sell_trade.get("price", 0.0))
    sell_time = sell_trade.get("time", "")
    sell_date = sell_time.split(" ")[0] if " " in sell_time else sell_time[:10]
    sell_shares = int(sell_trade.get("shares", 0))
    realized_pnl = float(sell_trade.get("realized_pnl", 0.0))
    reason = sell_trade.get("reason", "")

    buy_date = buy_summary["buy_date"]
    buy_price = buy_summary["buy_price"]
    buy_time = buy_summary["buy_time"]

    # 读取标的历史日线
    all_bars = get_bars_for_symbol(symbol, history_cache=history_cache, feed=feed)

    # 确保 bars 按日期升序排列
    sorted_bars = sorted(all_bars, key=lambda b: b.get("date", ""))

    # 检查卖出当天的数据是否已在历史 bars 中，若尚无（如盘中触发），使用卖出价与盘口行情合成当日 bar
    dates_in_bars = {b.get("date") for b in sorted_bars}
    if sell_date and sell_date not in dates_in_bars and sell_price > 0:
        q_open = float(current_quote.get("open", sell_price)) if current_quote else sell_price
        q_high = max(sell_price, float(current_quote.get("high", sell_price))) if current_quote else sell_price
        q_low = min(sell_price, float(current_quote.get("low", sell_price))) if current_quote else sell_price
        q_vol = float(current_quote.get("volume", 0.0)) if current_quote else 0.0
        synthetic_bar = {
            "date": sell_date,
            "open": q_open if q_open > 0 else sell_price,
            "high": q_high if q_high > 0 else sell_price,
            "low": q_low if q_low > 0 else sell_price,
            "close": sell_price,
            "volume": q_vol,
            "is_synthetic": True
        }
        sorted_bars.append(synthetic_bar)
        sorted_bars.sort(key=lambda b: b.get("date", ""))

    # 提取持仓区间内的日线 (buy_date <= date <= sell_date)
    holding_bars = [b for b in sorted_bars if buy_date <= b.get("date", "") <= sell_date]

    # 若未找到任何历史 bar（如极早期数据），构造最小保底 bar 序列
    if not holding_bars:
        holding_bars = [
            {"date": buy_date, "open": buy_price, "high": max(buy_price, sell_price), "low": min(buy_price, sell_price), "close": buy_price, "volume": 0},
            {"date": sell_date, "open": buy_price, "high": max(buy_price, sell_price), "low": min(buy_price, sell_price), "close": sell_price, "volume": 0}
        ]

    # 提取用于图表展示的 bars（持仓区间 + 前置 2-3 个交易日作为走势参照）
    buy_idx_in_all = -1
    for i, b in enumerate(sorted_bars):
        if b.get("date") == buy_date:
            buy_idx_in_all = i
            break
    chart_start_idx = max(0, buy_idx_in_all - 3) if buy_idx_in_all >= 0 else 0
    sell_idx_in_all = len(sorted_bars)
    for i, b in enumerate(sorted_bars):
        if b.get("date") == sell_date:
            sell_idx_in_all = i + 1
            break
    chart_bars = sorted_bars[chart_start_idx:sell_idx_in_all] if sorted_bars else holding_bars

    # 计算持仓期间的核心复盘指标
    holding_days = len(holding_bars)
    max_high = max(float(b.get("high", b.get("close", buy_price))) for b in holding_bars)
    min_low = min(float(b.get("low", b.get("close", buy_price))) for b in holding_bars)

    max_high_bar = next((b for b in holding_bars if float(b.get("high", b.get("close", buy_price))) == max_high), holding_bars[0])
    min_low_bar = next((b for b in holding_bars if float(b.get("low", b.get("close", buy_price))) == min_low), holding_bars[0])

    max_high_date = max_high_bar.get("date", buy_date)
    min_low_date = min_low_bar.get("date", buy_date)

    max_profit_pct = round(((max_high / buy_price) - 1.0) * 100.0, 2) if buy_price > 0 else 0.0
    max_drawdown_pct = round(((min_low / buy_price) - 1.0) * 100.0, 2) if buy_price > 0 else 0.0
    realized_pnl_pct = round(((sell_price / buy_price) - 1.0) * 100.0, 2) if buy_price > 0 else 0.0

    had_profit_opportunity = bool(max_profit_pct >= 2.0)

    # 机会诊断判定文本
    if realized_pnl > 0 or realized_pnl_pct > 0:
        verdict = f"🎉 顺利锁定利润：持仓期间最高浮盈 +{max_profit_pct:.2f}%，最终平仓获利 +{realized_pnl_pct:.2f}%。"
        opportunity_type = "PROFIT_LOCKED"
    elif max_profit_pct >= 4.0:
        verdict = f"⚠️ 曾有绝佳盈利机会（最高浮盈曾达 +{max_profit_pct:.2f}%，于 {max_high_date}），冲高后趋势走弱触发止损。"
        opportunity_type = "PEAK_RETREAT_STOP"
    elif max_profit_pct >= 1.5:
        verdict = f"⚠️ 曾有轻度冲高脉冲（最高浮盈仅 +{max_profit_pct:.2f}%），未能形成持续主升浪，回落触发止损。"
        opportunity_type = "MINOR_PULSE_STOP"
    else:
        verdict = f"⛔ 入场即承压走弱（最高浮盈仅 +{max_profit_pct:.2f}%，最低下探至 {max_drawdown_pct:.2f}%），触发硬止损防守平仓。"
        opportunity_type = "STRAIGHT_DRAWDOWN_STOP"

    # 生成逐日表现轨迹明细
    daily_trajectory = []
    prev_close = None
    for b in holding_bars:
        d = b.get("date", "")
        o = float(b.get("open", 0.0))
        h = float(b.get("high", 0.0))
        l = float(b.get("low", 0.0))
        c = float(b.get("close", 0.0))
        vol = float(b.get("volume", 0.0))

        day_chg_pct = round(((c / prev_close) - 1.0) * 100.0, 2) if prev_close and prev_close > 0 else 0.0
        high_profit_pct = round(((h / buy_price) - 1.0) * 100.0, 2) if buy_price > 0 else 0.0
        close_pnl_pct = round(((c / buy_price) - 1.0) * 100.0, 2) if buy_price > 0 else 0.0

        daily_trajectory.append({
            "date": d,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": vol,
            "day_change_pct": day_chg_pct,
            "high_profit_pct": high_profit_pct,
            "close_pnl_pct": close_pnl_pct
        })
        prev_close = c

    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "name": name,
        "buy_date": buy_date,
        "buy_time": buy_time,
        "buy_price": buy_price,
        "sell_date": sell_date,
        "sell_time": sell_time,
        "sell_price": sell_price,
        "shares": sell_shares,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
        "reason": reason,
        "holding_days": holding_days,
        "max_high": max_high,
        "max_high_date": max_high_date,
        "max_profit_pct": max_profit_pct,
        "min_low": min_low,
        "min_low_date": min_low_date,
        "max_drawdown_pct": max_drawdown_pct,
        "had_profit_opportunity": had_profit_opportunity,
        "opportunity_type": opportunity_type,
        "verdict": verdict,
        "holding_bars": holding_bars,
        "chart_bars": chart_bars,
        "daily_trajectory": daily_trajectory
    }


def generate_trade_kline_chart(review_data: Dict[str, Any], output_path: Optional[Path] = None) -> str:
    """
    使用 Matplotlib 绘制持仓复盘 K 线图（上层蜡烛图+均线+买卖标注+最高点星标，下层成交量），并保存为高清 PNG。
    """
    trade_id = review_data.get("trade_id") or f"T_{review_data.get('symbol')}_{review_data.get('sell_date')}"
    if output_path is None:
        output_path = CHARTS_DIR / f"{trade_id}.png"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chart_bars = review_data.get("chart_bars", [])
    if not chart_bars:
        chart_bars = review_data.get("holding_bars", [])

    n = len(chart_bars)
    if n == 0:
        return ""

    dates = [b.get("date", "") for b in chart_bars]
    opens = [float(b.get("open", 0.0)) for b in chart_bars]
    highs = [float(b.get("high", 0.0)) for b in chart_bars]
    lows = [float(b.get("low", 0.0)) for b in chart_bars]
    closes = [float(b.get("close", 0.0)) for b in chart_bars]
    volumes = [float(b.get("volume", 0.0)) for b in chart_bars]

    # 计算均线 MA5 和 MA20
    def calc_ma(period):
        ma = []
        for i in range(n):
            if i + 1 < period:
                ma.append(None)
            else:
                ma.append(sum(closes[i - period + 1 : i + 1]) / period)
        return ma

    ma5 = calc_ma(5)
    ma20 = calc_ma(20)

    # 确定买入日与卖出日对应的柱子索引
    buy_date = review_data.get("buy_date", "")
    sell_date = review_data.get("sell_date", "")
    buy_price = float(review_data.get("buy_price", 0.0))
    sell_price = float(review_data.get("sell_price", 0.0))
    max_high = float(review_data.get("max_high", 0.0))
    max_high_date = review_data.get("max_high_date", "")
    max_profit_pct = float(review_data.get("max_profit_pct", 0.0))
    realized_pnl_pct = float(review_data.get("realized_pnl_pct", 0.0))

    buy_idx = dates.index(buy_date) if buy_date in dates else 0
    sell_idx = dates.index(sell_date) if sell_date in dates else n - 1
    max_high_idx = dates.index(max_high_date) if max_high_date in dates else buy_idx

    # 创建绘图画布（2 个子图：主图 K 线 + 副图成交量）
    fig, (ax1, ax2) = plt.subplots(
        2, 1,
        figsize=(10.5, 6.2),
        dpi=140,
        gridspec_kw={"height_ratios": [3.6, 1.2], "hspace": 0.08}
    )

    # 配色方案（经典 A 股红涨绿跌，深色高雅背景）
    bg_color = "#181a20"
    panel_color = "#1e222d"
    text_color = "#e0e3eb"
    sub_text_color = "#848e9c"
    up_color = "#f6465d"    # A股红涨
    down_color = "#0ecb81"  # A股绿跌
    buy_color = "#2979ff"   # 买入点蓝色
    gold_color = "#ffb300"  # 最高点金色

    fig.patch.set_facecolor(bg_color)
    ax1.set_facecolor(panel_color)
    ax2.set_facecolor(panel_color)

    # 1. 绘制蜡烛图（ax1）
    width = 0.55
    wick_width = 1.3
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        is_up = c >= o
        bar_color = up_color if is_up else down_color

        # 影线
        ax1.plot([i, i], [l, h], color=bar_color, linewidth=wick_width, zorder=2)
        # 实体矩形
        rect_y = o if not is_up else o
        rect_h = abs(c - o)
        if rect_h < 0.005:  # 十字星或平盘处理
            rect_h = 0.01
            rect_y = o - 0.005
        else:
            rect_y = min(o, c)

        rect = patches.Rectangle(
            (i - width / 2, rect_y),
            width, rect_h,
            facecolor=bar_color if is_up else bar_color,
            edgecolor=bar_color,
            linewidth=0.8,
            zorder=3
        )
        ax1.add_patch(rect)

        # 2. 绘制成交量柱（ax2）
        vol_rect = patches.Rectangle(
            (i - width / 2, 0),
            width, volumes[i],
            facecolor=bar_color,
            edgecolor=bar_color,
            alpha=0.85
        )
        ax2.add_patch(vol_rect)

    # 绘制均线
    valid_ma5_x = [i for i, v in enumerate(ma5) if v is not None]
    valid_ma5_y = [v for v in ma5 if v is not None]
    if valid_ma5_x:
        ax1.plot(valid_ma5_x, valid_ma5_y, color="#f0a500", linewidth=1.2, label="MA5", alpha=0.85, zorder=4)

    valid_ma20_x = [i for i, v in enumerate(ma20) if v is not None]
    valid_ma20_y = [v for v in ma20 if v is not None]
    if valid_ma20_x:
        ax1.plot(valid_ma20_x, valid_ma20_y, color="#4aa3df", linewidth=1.2, label="MA20", alpha=0.85, zorder=4)

    # 绘制买入价参考水平线与买入标注
    ax1.hlines(
        y=buy_price, xmin=buy_idx, xmax=sell_idx,
        colors="#00b0ff", linestyles="--", linewidth=1.5, alpha=0.9, zorder=5
    )
    ax1.scatter([buy_idx], [buy_price], color="#00b0ff", s=110, marker="^", zorder=7, edgecolors="white", linewidth=1.2)
    ax1.annotate(
        f"▲ 买入价 ¥{buy_price:.2f}\n  ({dates[buy_idx]})",
        xy=(buy_idx, buy_price),
        xytext=(buy_idx + 0.15, buy_price * 0.985),
        color="#80d8ff",
        fontsize=9,
        fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.25", facecolor="#1e3a5f", edgecolor="#00b0ff", alpha=0.9),
        zorder=8
    )

    # 绘制卖出价参考水平线与卖出标注
    ax1.scatter([sell_idx], [sell_price], color=up_color if realized_pnl_pct >= 0 else down_color,
                s=110, marker="v", zorder=7, edgecolors="white", linewidth=1.2)
    pnl_sign = "+" if realized_pnl_pct > 0 else ""
    ax1.annotate(
        f"▼ 卖出价 ¥{sell_price:.2f}\n  ({dates[sell_idx]}, {pnl_sign}{realized_pnl_pct:.2f}%)",
        xy=(sell_idx, sell_price),
        xytext=(max(0, sell_idx - 1.6), sell_price * 1.015),
        color="#ff8a80" if realized_pnl_pct < 0 else "#b9f6ca",
        fontsize=9,
        fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.25", facecolor="#3e2723" if realized_pnl_pct < 0 else "#1b5e20",
                  edgecolor=up_color if realized_pnl_pct >= 0 else down_color, alpha=0.9),
        zorder=8
    )

    # 绘制持仓最高点（Peak Profit）金色星标
    if max_high > 0:
        ax1.scatter([max_high_idx], [max_high], color=gold_color, s=150, marker="*", zorder=9, edgecolors="#fff9c4", linewidth=1.0)
        ax1.annotate(
            f"★ 最高浮盈 +{max_profit_pct:.2f}%\n  (¥{max_high:.2f} @ {dates[max_high_idx]})",
            xy=(max_high_idx, max_high),
            xytext=(max_high_idx + 0.2, max_high * 1.012),
            color=gold_color,
            fontsize=9.5,
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#332a00", edgecolor=gold_color, alpha=0.9),
            zorder=10
        )

    # 持仓区间背景高亮阴影带
    ax1.axvspan(buy_idx - 0.4, sell_idx + 0.4, color="#2979ff", alpha=0.08, zorder=1)
    ax2.axvspan(buy_idx - 0.4, sell_idx + 0.4, color="#2979ff", alpha=0.08, zorder=1)

    # 设置标题与副标题
    name = review_data.get("name", review_data.get("symbol", ""))
    symbol = review_data.get("symbol", "")
    holding_days = review_data.get("holding_days", 1)
    realized_pnl = review_data.get("realized_pnl", 0.0)
    verdict = review_data.get("verdict", "")

    pnl_str = f"{'+' if realized_pnl > 0 else ''}¥{realized_pnl:,.2f} ({pnl_sign}{realized_pnl_pct:.2f}%)"
    title_text = f"【持仓周期复盘】{name} ({symbol}) · 持仓 {holding_days} 天"
    sub_title = f"买入 ¥{buy_price:.2f} -> 卖出 ¥{sell_price:.2f} | 最终实现盈亏：{pnl_str} | 持仓最高浮盈：+{max_profit_pct:.2f}%"

    fig.suptitle(title_text, color=text_color, fontsize=13, fontweight="bold", y=0.97)
    ax1.set_title(sub_title, color=sub_text_color, fontsize=9.5, pad=6)

    # 设置坐标轴范围与网格
    ax1.set_xlim(-0.8, n - 0.2)
    ax2.set_xlim(-0.8, n - 0.2)
    all_prices = [l for l in lows if l > 0] + [h for h in highs if h > 0] + [buy_price, sell_price]
    if all_prices:
        min_p = min(all_prices) * 0.97
        max_p = max(all_prices) * 1.03
        ax1.set_ylim(min_p, max_p)

    ax1.grid(True, linestyle="--", alpha=0.15, color="#ffffff")
    ax2.grid(True, linestyle="--", alpha=0.15, color="#ffffff")

    # 格式化 X 轴日期标签
    tick_step = max(1, n // 8)
    tick_indices = list(range(0, n, tick_step))
    if (n - 1) not in tick_indices:
        tick_indices.append(n - 1)

    ax2.set_xticks(tick_indices)
    ax2.set_xticklabels([dates[i][5:] if len(dates[i]) >= 10 else dates[i] for i in tick_indices],
                        color=sub_text_color, fontsize=8.5, rotation=0)

    # 隐藏 ax1 的 x 刻度标签
    ax1.set_xticklabels([])
    ax1.tick_params(axis="x", length=0)

    # Y 轴刻度样式
    ax1.tick_params(axis="y", colors=sub_text_color, labelsize=8.5)
    ax2.tick_params(axis="y", colors=sub_text_color, labelsize=7.5)
    ax1.yaxis.tick_right()
    ax2.yaxis.tick_right()

    # 边框样式
    for ax in (ax1, ax2):
        for spine in ax.spines.values():
            spine.set_color("#333b4d")
            spine.set_linewidth(0.8)

    # 图例
    handles, labels = ax1.get_legend_handles_labels()
    if handles and labels:
        ax1.legend(handles, labels, loc="upper left", facecolor="#1e222d", edgecolor="#333b4d", labelcolor=text_color, fontsize=8)

    # 保存图表
    plt.savefig(output_path, facecolor=fig.get_facecolor(), edgecolor="none", bbox_inches="tight")
    plt.close(fig)
    logger.info(f"持仓 K 线复盘图已生成：{output_path}")
    return str(output_path)


def format_trade_review_markdown(review_data: Dict[str, Any], chart_base_url: str = "") -> str:
    """
    生成用于微信推送与系统消息的结构化 Markdown 复盘报告。
    """
    name = review_data.get("name", "")
    symbol = review_data.get("symbol", "")
    trade_id = review_data.get("trade_id", "")
    buy_date = review_data.get("buy_date", "")
    sell_date = review_data.get("sell_date", "")
    buy_price = review_data.get("buy_price", 0.0)
    sell_price = review_data.get("sell_price", 0.0)
    shares = review_data.get("shares", 0)
    realized_pnl = review_data.get("realized_pnl", 0.0)
    realized_pnl_pct = review_data.get("realized_pnl_pct", 0.0)
    holding_days = review_data.get("holding_days", 1)
    max_high = review_data.get("max_high", 0.0)
    max_high_date = review_data.get("max_high_date", "")
    max_profit_pct = review_data.get("max_profit_pct", 0.0)
    min_low = review_data.get("min_low", 0.0)
    min_low_date = review_data.get("min_low_date", "")
    max_drawdown_pct = review_data.get("max_drawdown_pct", 0.0)
    verdict = review_data.get("verdict", "")
    daily_traj = review_data.get("daily_trajectory", [])

    pnl_sign = "+" if realized_pnl > 0 else ""
    pnl_color_tag = "🔴 浮亏止损" if realized_pnl < 0 else "🟢 止盈离场"

    lines = [
        f"### 📈 持仓周期 K 线与盈利机会复盘 ({pnl_color_tag})",
        f"- **标的信息**：`{name}` (`{symbol}`) | **持仓时长**：**{holding_days}** 个交易日",
        f"- **建仓卖出**：买入均价 **¥{buy_price:.2f}** ({buy_date}) ➔ 卖出均价 **¥{sell_price:.2f}** ({sell_date})",
        f"- **平仓盈亏**：**{pnl_sign}¥{realized_pnl:,.2f}** (**{pnl_sign}{realized_pnl_pct:.2f}%**)",
        f"- ⭐ **持仓最高潜在浮盈**：**+{max_profit_pct:.2f}%** (于 {max_high_date} 触及最高价 ¥{max_high:.2f})",
        f"- ⚠️ **持仓最低价格回撤**：**{max_drawdown_pct:.2f}%** (于 {min_low_date} 触及最低价 ¥{min_low:.2f})",
        f"- 💡 **盈利机会诊断**：{verdict}",
        "",
        "#### 📅 持仓期间逐日价格与浮盈轨迹"
    ]

    if daily_traj:
        lines.append("| 交易日期 | 开盘价 | 最高价 | 最低价 | 收盘价 | 日涨跌幅 | 当日最高浮盈 | 收盘浮盈 |")
        lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
        for row in daily_traj:
            d = row.get("date", "")
            o = f"¥{row.get('open', 0.0):.2f}"
            h = f"¥{row.get('high', 0.0):.2f}"
            l = f"¥{row.get('low', 0.0):.2f}"
            c = f"¥{row.get('close', 0.0):.2f}"
            chg = f"{'+' if row.get('day_change_pct', 0.0) > 0 else ''}{row.get('day_change_pct', 0.0):.2f}%"
            h_pnl = f"{'+' if row.get('high_profit_pct', 0.0) > 0 else ''}{row.get('high_profit_pct', 0.0):.2f}%"
            c_pnl = f"{'+' if row.get('close_pnl_pct', 0.0) > 0 else ''}{row.get('close_pnl_pct', 0.0):.2f}%"
            lines.append(f"| {d} | {o} | {h} | {l} | {c} | {chg} | **{h_pnl}** | {c_pnl} |")
    else:
        lines.append("*(暂无持仓期间逐日分时明细)*")

    if trade_id:
        chart_url = f"{chart_base_url.rstrip('/')}/api/trade/chart/{trade_id}" if chart_base_url else f"/api/trade/chart/{trade_id}"
        lines.extend([
            "",
            f"🔍 **复盘图谱**：[点击在浏览器查看高清持仓 K 线图]({chart_url})"
        ])

    return "\n".join(lines)
