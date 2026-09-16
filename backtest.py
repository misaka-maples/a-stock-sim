#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股拟真量化策略历史回测引擎 (A-Share Backtesting Engine)

特性：
1. 拟真 A 股交易机制：
   - 严格 T+1 交易锁仓（当日买入次日及以后方可卖出）。
   - 100 股整手数限制与多标的仓位上限约束。
   - 真实交易费率：佣金（万2.5，最低5元）+ 卖出印花税（万5）+ 过户费（十万分之1）+ 滑点撮合。
   - 杜绝未来函数：基于当日形态与量能评分选股，无超前偏误。
2. 策略逻辑：
   - ShortTermResonance: 放量起爆量价共振、硬止损、冲高回落动态追踪止盈、大涨锁定利润与周期轮动。
   - MomentumBreakout: 突破平台新高与动量增强策略。
3. 全面量化绩效报告：
   - 累计收益率、年化复合收益率 (CAGR)、基准（沪深300）同期收益与超额收益 (Alpha)。
   - 最大回撤及发生区间、夏普比率 (Sharpe Ratio)、卡玛比率 (Calmar Ratio)。
   - 交易胜率、盈亏比、持仓胜负场次、平均持仓天数。
   - 逐笔成交流水明细与每日资产净值曲线（策略 vs 沪深300）。
4. 终端 Rich 美化呈现与标准 JSON API 导出支持。
"""

import sys
import os
import math
import json
import time
import argparse
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from history_data import HistoricalDataFeed, PRESET_POOLS

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class BacktestEngine:
    """A股拟真历史回测引擎"""

    def __init__(
        self,
        start_date: str = "2024-01-01",
        end_date: str = "2026-09-15",
        initial_cash: float = 100000.0,
        strategy_name: str = "ShortTermResonance",
        symbols: Optional[List[str]] = None,
        pool_type: str = "core_active",
        benchmark_code: str = "000300",
        # 资金与仓位
        max_positions: int = 3,
        max_stock_weight: float = 0.30,
        # 费率设置
        commission_rate: float = 0.00025,
        min_commission: float = 5.0,
        stamp_tax_rate: float = 0.0005,
        transfer_fee_rate: float = 0.00001,
        slippage_rate: float = 0.001,
        strict_t1: bool = True,
        # 策略参数
        min_change_pct: float = 2.0,
        max_change_pct: float = 6.5,
        min_volume_ratio: float = 1.3,
        min_turnover_rate: float = 1.5,
        take_profit_trigger_pct: float = 3.5,
        trailing_callback_pct: float = 1.5,
        stop_loss_pct: float = -2.5,
        max_take_profit_pct: float = 7.0,
        max_holding_days: int = 5,
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.initial_cash = float(initial_cash)
        self.strategy_name = strategy_name
        self.pool_type = pool_type
        self.symbols = symbols or HistoricalDataFeed.get_preset_symbols(pool_type)
        self.benchmark_code = benchmark_code

        # 仓位与风控
        self.max_positions = max_positions
        self.max_stock_weight = max_stock_weight
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax_rate = stamp_tax_rate
        self.transfer_fee_rate = transfer_fee_rate
        self.slippage_rate = slippage_rate
        self.strict_t1 = strict_t1

        # 策略参数
        self.min_change_pct = min_change_pct
        self.max_change_pct = max_change_pct
        self.min_volume_ratio = min_volume_ratio
        self.min_turnover_rate = min_turnover_rate
        self.take_profit_trigger_pct = take_profit_trigger_pct
        self.trailing_callback_pct = trailing_callback_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_take_profit_pct = max_take_profit_pct
        self.max_holding_days = max_holding_days

        self.feed = HistoricalDataFeed()

    def _calculate_buy_fees(self, gross_amount: float) -> float:
        comm = max(self.min_commission, gross_amount * self.commission_rate)
        transfer = gross_amount * self.transfer_fee_rate
        return comm + transfer

    def _calculate_sell_fees(self, gross_amount: float) -> float:
        comm = max(self.min_commission, gross_amount * self.commission_rate)
        tax = gross_amount * self.stamp_tax_rate
        transfer = gross_amount * self.transfer_fee_rate
        return comm + tax + transfer

    def run(self) -> Dict[str, Any]:
        """运行历史回测，返回全套量化绩效指标与逐笔记录"""
        # 1. 抓取基准与股票历史行情
        benchmark_bars = self.feed.fetch_benchmark_kline(
            index_code=self.benchmark_code,
            start_date=self.start_date,
            end_date=self.end_date
        )
        if not benchmark_bars:
            raise ValueError(f"无法获取基准指数 {self.benchmark_code} 历史数据")

        # 确定基准日期序列（以基准指数交易日为全市场交易日历）
        trading_dates = [b["date"] for b in benchmark_bars if self.start_date <= b["date"] <= self.end_date]
        bench_by_date = {b["date"]: b for b in benchmark_bars}

        # 抓取股票历史数据
        stocks_data = self.feed.fetch_universe_klines(
            self.symbols,
            start_date=self.start_date,
            end_date=self.end_date
        )

        # 2. 模拟账户初始状态
        cash = self.initial_cash
        positions: Dict[str, Dict[str, Any]] = {}
        # positions[sym] = {
        #    "symbol": sym, "name": name, "shares": int, "cost_price": float,
        #    "buy_date": date, "buy_price": float, "peak_price": float,
        #    "holding_days": int, "fees_paid": float
        # }
        trades: List[Dict[str, Any]] = []
        equity_curve: List[Dict[str, Any]] = []

        peak_equity = self.initial_cash
        initial_bench_close = benchmark_bars[0]["close"] if benchmark_bars else 1.0

        # 逐日回测循环
        for current_date in trading_dates:
            bench_bar = bench_by_date.get(current_date)
            bench_close = bench_bar["close"] if bench_bar else initial_bench_close
            bench_return_pct = round(((bench_close / initial_bench_close) - 1) * 100, 2)

            # -------------------------------------------------------------
            # 阶段 A: 持仓标的追踪与止盈止损检查 (Exit & Risk Control)
            # -------------------------------------------------------------
            just_sold = set()
            for sym, pos in list(positions.items()):
                stock_info = stocks_data.get(sym)
                if not stock_info:
                    continue
                bar = stock_info["by_date"].get(current_date)
                if not bar:
                    continue

                pos["holding_days"] += 1
                op = bar["open"]
                hi = bar["high"]
                lo = bar["low"]
                cl = bar["close"]
                cost_price = pos["cost_price"]
                shares = pos["shares"]

                # 更新最高峰值价格
                if hi > pos["peak_price"]:
                    pos["peak_price"] = hi

                # A股严格 T+1: 当日买入的不可当日卖出
                if self.strict_t1 and pos["buy_date"] == current_date:
                    continue

                should_sell = False
                sell_price = cl
                sell_reason = ""

                bars_list = stock_info["bars"]
                try:
                    cur_idx = next(i for i, b in enumerate(bars_list) if b["date"] == current_date)
                except StopIteration:
                    cur_idx = -1

                if self.strategy_name == "TurtleBreakout" and cur_idx >= 10:
                    # 经典海龟法则出场：跌破10日低点退出，或触发大波段硬止损 (-6%)
                    min_low_10 = min(b["low"] for b in bars_list[cur_idx-10:cur_idx])
                    gain_pct = ((cl / cost_price) - 1.0) * 100.0
                    if lo < min_low_10:
                        should_sell = True
                        sell_price = round(min(op, min_low_10) * (1 - self.slippage_rate), 3)
                        sell_reason = "海龟法则: 跌破10日唐奇安通道低点平仓"
                    elif gain_pct <= -6.0:
                        should_sell = True
                        sell_price = round(min(op, cost_price * 0.94) * (1 - self.slippage_rate), 3)
                        sell_reason = "海龟法则: 触发防守硬止损 (-6.0%)"

                elif self.strategy_name == "Minervini_SEPA" and cur_idx >= 20:
                    # 马克·米奈尔维尼 (Mark Minervini) SEPA/VCP 冠军策略出场法则：
                    # 1. 严格防守硬止损 (-6%)
                    # 2. 均线生命线追踪：持仓满 3 天后跌破 MA20 趋势破位离场
                    # 3. 大波段超额利润锁定 (+30%)
                    ma20 = sum(b["close"] for b in bars_list[cur_idx-19:cur_idx+1]) / 20.0
                    gain_pct = ((cl / cost_price) - 1.0) * 100.0
                    if lo <= cost_price * 0.94:
                        should_sell = True
                        sell_price = round(min(op, cost_price * 0.94) * (1 - self.slippage_rate), 3)
                        sell_reason = "米奈尔维尼SEPA: 触发防守硬止损 (-6.0%)"
                    elif pos["holding_days"] >= 3 and cl < ma20:
                        should_sell = True
                        sell_price = round(cl * (1 - self.slippage_rate), 3)
                        sell_reason = "米奈尔维尼SEPA: 跌破MA20均线生命线平仓"
                    elif gain_pct >= 30.0:
                        should_sell = True
                        sell_price = round(cl * (1 - self.slippage_rate), 3)
                        sell_reason = "米奈尔维尼SEPA: 大波段超额利润锁定 (+30.0%)"

                elif self.strategy_name == "ONeil_CANSLIM" and cur_idx >= 20:
                    # 威廉·欧奈尔 (William O'Neil) CAN SLIM 领头羊出场法则：
                    # 1. 铁律防守硬止损 (-6%)
                    # 2. 均线生命线追踪：持仓满 3 天后跌破 MA20 平仓
                    # 3. 领头羊大波段主升浪止盈 (+25%)
                    ma20 = sum(b["close"] for b in bars_list[cur_idx-19:cur_idx+1]) / 20.0
                    gain_pct = ((cl / cost_price) - 1.0) * 100.0
                    if lo <= cost_price * 0.94:
                        should_sell = True
                        sell_price = round(min(op, cost_price * 0.94) * (1 - self.slippage_rate), 3)
                        sell_reason = "欧奈尔CAN SLIM: 触发铁律硬止损 (-6.0%)"
                    elif pos["holding_days"] >= 3 and cl < ma20:
                        should_sell = True
                        sell_price = round(cl * (1 - self.slippage_rate), 3)
                        sell_reason = "欧奈尔CAN SLIM: 跌破MA20生命线平仓"
                    elif gain_pct >= 25.0:
                        should_sell = True
                        sell_price = round(cl * (1 - self.slippage_rate), 3)
                        sell_reason = "欧奈尔CAN SLIM: 领头羊主升浪止盈 (+25.0%)"

                elif self.strategy_name == "MATrendFollowing" and cur_idx >= 20:
                    # 双均线趋势跟踪出场：跌破MA20趋势线，或硬止损 (-5%)，或大波段止盈 (+35%)
                    ma20 = sum(b["close"] for b in bars_list[cur_idx-19:cur_idx+1]) / 20.0
                    gain_pct = ((cl / cost_price) - 1.0) * 100.0
                    if cl < ma20:
                        should_sell = True
                        sell_price = round(cl * (1 - self.slippage_rate), 3)
                        sell_reason = "均线跟踪: 跌破MA20生命线平仓"
                    elif gain_pct <= -5.0:
                        should_sell = True
                        sell_price = round(min(op, cost_price * 0.95) * (1 - self.slippage_rate), 3)
                        sell_reason = "均线跟踪: 触发硬止损 (-5.0%)"
                    elif gain_pct >= 35.0:
                        should_sell = True
                        sell_price = round(cl * (1 - self.slippage_rate), 3)
                        sell_reason = "均线跟踪: 大波段利润兑现 (+35.0%)"

                else:
                    # 1. 硬止损检查 (以当日最低价判定是否击穿止损线)
                    loss_trigger_price = cost_price * (1 + self.stop_loss_pct / 100.0)
                    if lo <= loss_trigger_price:
                        should_sell = True
                        exec_p = min(op, loss_trigger_price) * (1 - self.slippage_rate)
                        sell_price = round(max(0.01, exec_p), 3)
                        sell_reason = f"硬止损触发 (触及 {self.stop_loss_pct:.1f}% 止损线)"

                    # 2. 绝对大涨止盈检查 (以当日最高价判定是否达到暴涨线)
                    elif hi >= cost_price * (1 + self.max_take_profit_pct / 100.0):
                        should_sell = True
                        take_price = cost_price * (1 + self.max_take_profit_pct / 100.0)
                        exec_p = max(op, take_price) * (1 - self.slippage_rate)
                        sell_price = round(exec_p, 3)
                        sell_reason = f"大涨止盈 (触及 +{self.max_take_profit_pct:.1f}% 暴涨止盈线)"

                    # 3. 动态追踪止盈检查 (若最高涨幅曾突破门槛，回撤超过指定幅度则锁定利润)
                    elif not should_sell:
                        peak_gain_pct = ((pos["peak_price"] / cost_price) - 1) * 100.0
                        if peak_gain_pct >= self.take_profit_trigger_pct:
                            trail_stop_price = pos["peak_price"] * (1 - self.trailing_callback_pct / 100.0)
                            if lo <= trail_stop_price:
                                should_sell = True
                                exec_p = min(op, trail_stop_price) * (1 - self.slippage_rate)
                                sell_price = round(max(0.01, exec_p), 3)
                                sell_reason = f"动态追踪止盈 (最高冲至 +{peak_gain_pct:.1f}%, 从高点回撤 {self.trailing_callback_pct:.1f}%)"

                    # 4. 时间周期轮动 (超期未达到止盈止损则调仓)
                    if not should_sell and pos["holding_days"] >= self.max_holding_days:
                        should_sell = True
                        sell_price = round(cl * (1 - self.slippage_rate), 3)
                        sell_reason = f"持仓满 {self.max_holding_days} 天轮动调仓"

                # 执行卖出结算
                if should_sell:
                    gross_sell = shares * sell_price
                    sell_fees = self._calculate_sell_fees(gross_sell)
                    net_proceeds = gross_sell - sell_fees
                    cash += net_proceeds

                    total_fees = pos.get("fees_paid", 0.0) + sell_fees
                    buy_gross = shares * pos["buy_price"]
                    net_pnl = round(net_proceeds - buy_gross - pos.get("fees_paid", 0.0), 2)
                    pnl_pct = round((net_pnl / buy_gross) * 100.0, 2) if buy_gross > 0 else 0.0

                    trades.append({
                        "symbol": sym,
                        "name": pos["name"],
                        "buy_date": pos["buy_date"],
                        "buy_price": pos["buy_price"],
                        "sell_date": current_date,
                        "sell_price": sell_price,
                        "shares": shares,
                        "buy_amount": round(buy_gross, 2),
                        "sell_amount": round(gross_sell, 2),
                        "net_pnl": net_pnl,
                        "pnl_pct": pnl_pct,
                        "fees": round(total_fees, 2),
                        "holding_days": pos["holding_days"],
                        "reason": sell_reason
                    })
                    just_sold.add(sym)
                    del positions[sym]

            # -------------------------------------------------------------
            # 阶段 B: 标的池全量扫描与买入机会撮合 (Entry Scan)
            # -------------------------------------------------------------
            # 计算当前账户总资产评估
            current_market_val = 0.0
            for sym, pos in positions.items():
                s_info = stocks_data.get(sym)
                if s_info and current_date in s_info["by_date"]:
                    current_market_val += pos["shares"] * s_info["by_date"][current_date]["close"]
                else:
                    current_market_val += pos["shares"] * pos["cost_price"]

            total_equity = cash + current_market_val
            holding_count = len(positions)

            if holding_count < self.max_positions:
                candidates = []
                for sym in self.symbols:
                    if sym in positions or sym in just_sold:
                        continue
                    stock_info = stocks_data.get(sym)
                    if not stock_info:
                        continue
                    by_date = stock_info["by_date"]
                    bar = by_date.get(current_date)
                    if not bar:
                        continue

                    bars_list = stock_info["bars"]
                    # 找到当前日期索引，计算历史均量
                    try:
                        cur_idx = next(i for i, b in enumerate(bars_list) if b["date"] == current_date)
                    except StopIteration:
                        continue

                    # 至少需要 5 天历史数据计算均量
                    if cur_idx < 5:
                        continue

                    prev_5_bars = bars_list[cur_idx-5:cur_idx]
                    avg_vol_5 = sum(b["volume"] for b in prev_5_bars) / 5.0

                    vol = bar["volume"]
                    chg = bar["change_pct"]
                    op = bar["open"]
                    cl = bar["close"]
                    hi = bar["high"]
                    lo = bar["low"]
                    turnover = bar.get("turnover_rate", 0.0)

                    # 策略买入条件判断
                    if self.strategy_name == "ShortTermResonance":
                        # 1. 阳线收盘且处于合理放量起爆区间
                        if cl <= op:
                            continue
                        if not (self.min_change_pct <= chg <= self.max_change_pct):
                            continue
                        # 2. 相对 5日均量明显放量
                        vol_ratio = vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
                        if vol_ratio < self.min_volume_ratio:
                            continue
                        # 3. 换手率过滤 (若有提供)
                        if turnover > 0 and turnover < self.min_turnover_rate:
                            continue

                        # 共振强度打分
                        score = round(vol_ratio * 2.0 + (chg / 5.0) * 1.5 + (turnover / 3.0) * 0.5, 2)
                        candidates.append({
                            "symbol": sym,
                            "name": stock_info["name"],
                            "bar": bar,
                            "score": score,
                            "vol_ratio": vol_ratio
                        })

                    elif self.strategy_name == "MomentumBreakout":
                        # 创 20 日新高突破
                        lookback = min(cur_idx, 20)
                        high_20 = max(b["high"] for b in bars_list[cur_idx-lookback:cur_idx])
                        if cl > high_20 and cl > op and (self.min_change_pct <= chg <= self.max_change_pct):
                            vol_ratio = vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
                            score = round(vol_ratio * 1.5 + chg, 2)
                            candidates.append({
                                "symbol": sym,
                                "name": stock_info["name"],
                                "bar": bar,
                                "score": score,
                                "vol_ratio": vol_ratio
                            })

                    elif self.strategy_name == "TurtleBreakout":
                        # 经典海龟交易法则 (20日高点唐奇安通道突破 + MA60中长期趋势过滤)
                        if cur_idx >= 60:
                            ma60 = sum(b["close"] for b in bars_list[cur_idx-59:cur_idx+1]) / 60.0
                            high_20 = max(b["high"] for b in bars_list[cur_idx-20:cur_idx])
                            # 突破20日新高 + 价格在MA60多头均线之上 + 阳线
                            if cl > high_20 and cl > ma60 and cl > op:
                                trend_strength = (cl / ma60 - 1.0) * 100.0
                                vol_ratio = vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
                                score = round(trend_strength * 1.5 + vol_ratio * 2.0, 2)
                                candidates.append({
                                    "symbol": sym,
                                    "name": stock_info["name"],
                                    "bar": bar,
                                    "score": score,
                                    "vol_ratio": vol_ratio
                                })

                    elif self.strategy_name == "MATrendFollowing":
                        # 双均线多头趋势跟踪 (MA20 > MA60 中长均线多头，回踩或突破 MA20 介入)
                        if cur_idx >= 60:
                            ma20 = sum(b["close"] for b in bars_list[cur_idx-19:cur_idx+1]) / 20.0
                            ma60 = sum(b["close"] for b in bars_list[cur_idx-59:cur_idx+1]) / 60.0
                            prev_cl = bars_list[cur_idx-1]["close"]
                            prev_ma20 = sum(b["close"] for b in bars_list[cur_idx-20:cur_idx]) / 20.0
                            if ma20 > ma60 and cl > ma20 and cl > op and (prev_cl <= prev_ma20 or lo <= ma20 * 1.01):
                                trend_strength = (ma20 / ma60 - 1.0) * 100.0
                                vol_ratio = vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
                                score = round(trend_strength * 2.0 + vol_ratio * 1.5, 2)
                                candidates.append({
                                    "symbol": sym,
                                    "name": stock_info["name"],
                                    "bar": bar,
                                    "score": score,
                                    "vol_ratio": vol_ratio
                                })

                    elif self.strategy_name == "Minervini_SEPA":
                        # 马克·米奈尔维尼 (Mark Minervini) SEPA + VCP 波动率收缩起爆策略
                        # 1. 趋势模板：Close > MA20 > MA60, 阳线收盘
                        # 2. VCP 波动率收敛：突破前 10 日振幅收窄在 25% 以内 (洗盘充分)
                        # 3. 关键点突破 (Pivot Breakout)：收盘突破 10 日新高，成交量放大 1.2 倍以上
                        if cur_idx >= 60:
                            ma20 = sum(b["close"] for b in bars_list[cur_idx-19:cur_idx+1]) / 20.0
                            ma60 = sum(b["close"] for b in bars_list[cur_idx-59:cur_idx+1]) / 60.0
                            if cl > ma20 and ma20 > ma60 and cl > op:
                                prev_10 = bars_list[cur_idx-10:cur_idx]
                                max_hi_10 = max(b["high"] for b in prev_10)
                                min_lo_10 = min(b["low"] for b in prev_10)
                                vcp_spread = (max_hi_10 - min_lo_10) / ma20
                                if cl > max_hi_10 and vcp_spread <= 0.25:
                                    vol_ratio = vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
                                    if vol_ratio >= 1.2:
                                        trend_score = (cl / ma60 - 1.0) * 2.0
                                        vcp_score = (0.25 - vcp_spread) * 10.0
                                        score = round(trend_score + vol_ratio * 1.5 + vcp_score, 2)
                                        candidates.append({
                                            "symbol": sym,
                                            "name": stock_info["name"],
                                            "bar": bar,
                                            "score": score,
                                            "vol_ratio": vol_ratio
                                        })

                    elif self.strategy_name == "ONeil_CANSLIM":
                        # 威廉·欧奈尔 (William O'Neil) CAN SLIM 相对强度与领头羊突破策略
                        # 1. 60日相对强度 (RS Alpha)：过去 60 日涨幅超越沪深300指数 5% 以上 (只买领头羊)
                        # 2. 突破 20 日高点阻力位 (Base Breakout) + MA60 多头生命线
                        # 3. 主力放量确认：成交量为 5日均量 1.3 倍以上
                        if cur_idx >= 60:
                            ma60 = sum(b["close"] for b in bars_list[cur_idx-59:cur_idx+1]) / 60.0
                            high_20 = max(b["high"] for b in bars_list[cur_idx-20:cur_idx])
                            if cl > high_20 and cl > ma60 and cl > op:
                                stock_ret_60 = (cl / bars_list[cur_idx-60]["close"] - 1.0) * 100.0
                                b_cur_idx = next((i for i, b in enumerate(benchmark_bars) if b["date"] == current_date), -1)
                                bench_ret_60 = 0.0
                                if b_cur_idx >= 60:
                                    bench_ret_60 = (benchmark_bars[b_cur_idx]["close"] / benchmark_bars[b_cur_idx-60]["close"] - 1.0) * 100.0
                                rs_alpha = stock_ret_60 - bench_ret_60
                                if rs_alpha >= 5.0:
                                    vol_ratio = vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
                                    if vol_ratio >= 1.3:
                                        score = round(rs_alpha * 1.5 + vol_ratio * 2.0, 2)
                                        candidates.append({
                                            "symbol": sym,
                                            "name": stock_info["name"],
                                            "bar": bar,
                                            "score": score,
                                            "vol_ratio": vol_ratio
                                        })

                # 按得分从高到低排序择优买入
                candidates.sort(key=lambda x: x["score"], reverse=True)

                for cand in candidates:
                    if len(positions) >= self.max_positions:
                        break
                    sym = cand["symbol"]
                    bar = cand["bar"]
                    # 尾盘收盘买入价 (加滑点)
                    buy_price = round(bar["close"] * (1 + self.slippage_rate), 3)

                    # 目标金额分配
                    target_amount = total_equity * self.max_stock_weight
                    target_shares = int(target_amount / (buy_price * 100)) * 100
                    if target_shares < 100:
                        continue

                    gross_buy = target_shares * buy_price
                    buy_fees = self._calculate_buy_fees(gross_buy)
                    total_needed = gross_buy + buy_fees

                    if cash >= total_needed:
                        cash -= total_needed
                        positions[sym] = {
                            "symbol": sym,
                            "name": cand["name"],
                            "shares": target_shares,
                            "cost_price": round(total_needed / target_shares, 4),
                            "buy_price": buy_price,
                            "buy_date": current_date,
                            "peak_price": buy_price,
                            "holding_days": 0,
                            "fees_paid": buy_fees
                        }

            # -------------------------------------------------------------
            # 阶段 C: 每日收盘资产净值核算 (Daily Valuation)
            # -------------------------------------------------------------
            end_market_val = 0.0
            for sym, pos in positions.items():
                s_info = stocks_data.get(sym)
                if s_info and current_date in s_info["by_date"]:
                    end_market_val += pos["shares"] * s_info["by_date"][current_date]["close"]
                else:
                    end_market_val += pos["shares"] * pos["cost_price"]

            current_equity = round(cash + end_market_val, 2)
            if current_equity > peak_equity:
                peak_equity = current_equity

            drawdown = round(peak_equity - current_equity, 2)
            drawdown_pct = round((drawdown / peak_equity) * 100.0, 2) if peak_equity > 0 else 0.0
            pnl_pct = round(((current_equity / self.initial_cash) - 1) * 100.0, 2)

            benchmark_equity = round(self.initial_cash * (bench_close / initial_bench_close), 2)

            equity_curve.append({
                "date": current_date,
                "equity": current_equity,
                "cash": round(cash, 2),
                "market_value": round(end_market_val, 2),
                "pnl": round(current_equity - self.initial_cash, 2),
                "pnl_pct": pnl_pct,
                "benchmark_equity": benchmark_equity,
                "benchmark_return_pct": bench_return_pct,
                "drawdown": drawdown,
                "drawdown_pct": drawdown_pct,
                "holding_count": len(positions)
            })

        # 3. 统计综合量化绩效指标
        final_equity = equity_curve[-1]["equity"] if equity_curve else self.initial_cash
        total_pnl = round(final_equity - self.initial_cash, 2)
        total_return_pct = round((total_pnl / self.initial_cash) * 100.0, 2)

        total_days = len(trading_dates)
        years = total_days / 250.0 if total_days > 0 else 1.0
        if final_equity > 0 and years > 0:
            cagr = round(((final_equity / self.initial_cash) ** (1.0 / years) - 1) * 100.0, 2)
        else:
            cagr = 0.0

        bench_final_return_pct = equity_curve[-1]["benchmark_return_pct"] if equity_curve else 0.0
        alpha_pct = round(total_return_pct - bench_final_return_pct, 2)

        max_drawdown_pct = max((p["drawdown_pct"] for p in equity_curve), default=0.0)
        max_drawdown_val = max((p["drawdown"] for p in equity_curve), default=0.0)

        # 日收益率与夏普比率计算 (无风险年化按 2.0%)
        daily_returns = []
        for i in range(1, len(equity_curve)):
            prev_eq = equity_curve[i-1]["equity"]
            cur_eq = equity_curve[i]["equity"]
            if prev_eq > 0:
                daily_returns.append((cur_eq - prev_eq) / prev_eq)

        if daily_returns and len(daily_returns) > 1:
            mean_r = sum(daily_returns) / len(daily_returns)
            var_r = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std_r = math.sqrt(var_r)
            rf_daily = 0.02 / 250.0
            if std_r > 1e-6:
                sharpe = round(((mean_r - rf_daily) / std_r) * math.sqrt(250.0), 2)
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        calmar = round(cagr / max_drawdown_pct, 2) if max_drawdown_pct > 0 else 0.0

        # 胜率与盈亏比
        win_trades = [t for t in trades if t["net_pnl"] > 0]
        loss_trades = [t for t in trades if t["net_pnl"] <= 0]
        win_count = len(win_trades)
        loss_count = len(loss_trades)
        total_trades = len(trades)
        win_rate = round((win_count / total_trades) * 100.0, 2) if total_trades > 0 else 0.0

        total_win_amt = sum(t["net_pnl"] for t in win_trades)
        total_loss_amt = abs(sum(t["net_pnl"] for t in loss_trades))
        avg_win = round(total_win_amt / win_count, 2) if win_count > 0 else 0.0
        avg_loss = round(total_loss_amt / loss_count, 2) if loss_count > 0 else 0.0
        profit_loss_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else (99.9 if avg_win > 0 else 0.0)

        total_fees = round(sum(t["fees"] for t in trades), 2)
        avg_holding_days = round(sum(t["holding_days"] for t in trades) / total_trades, 1) if total_trades > 0 else 0.0

        result = {
            "summary": {
                "strategy_name": self.strategy_name,
                "pool_type": self.pool_type,
                "symbol_count": len(self.symbols),
                "start_date": self.start_date,
                "end_date": self.end_date,
                "trading_days": total_days,
                "initial_cash": self.initial_cash,
                "final_equity": final_equity,
                "total_pnl": total_pnl,
                "total_return_pct": total_return_pct,
                "cagr_pct": cagr,
                "benchmark_code": self.benchmark_code,
                "benchmark_return_pct": bench_final_return_pct,
                "alpha_pct": alpha_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "max_drawdown_val": max_drawdown_val,
                "sharpe_ratio": sharpe,
                "calmar_ratio": calmar,
                "total_trades": total_trades,
                "win_rate": win_rate,
                "win_count": win_count,
                "loss_count": loss_count,
                "profit_loss_ratio": profit_loss_ratio,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "avg_holding_days": avg_holding_days,
                "total_fees": total_fees,
            },
            "parameters": {
                "max_positions": self.max_positions,
                "max_stock_weight": self.max_stock_weight,
                "min_change_pct": self.min_change_pct,
                "max_change_pct": self.max_change_pct,
                "take_profit_trigger_pct": self.take_profit_trigger_pct,
                "trailing_callback_pct": self.trailing_callback_pct,
                "stop_loss_pct": self.stop_loss_pct,
                "max_take_profit_pct": self.max_take_profit_pct,
                "max_holding_days": self.max_holding_days,
            },
            "equity_curve": equity_curve,
            "trades": trades
        }

        return result


def run_backtest(
    start_date: str = "2024-01-01",
    end_date: str = "2026-09-15",
    initial_cash: float = 100000.0,
    strategy_name: str = "ShortTermResonance",
    pool_type: str = "core_active",
    symbols: Optional[List[str]] = None,
    **kwargs
) -> Dict[str, Any]:
    """统一回测调用入口函数（供 API 与脚本调用）"""
    engine = BacktestEngine(
        start_date=start_date,
        end_date=end_date,
        initial_cash=initial_cash,
        strategy_name=strategy_name,
        pool_type=pool_type,
        symbols=symbols,
        **kwargs
    )
    return engine.run()


def print_backtest_report(res: Dict[str, Any]):
    """终端渲染回测综合报告"""
    s = res["summary"]
    p = res["parameters"]

    if HAS_RICH:
        console = Console()
        console.print("\n")
        console.print(Panel.fit(
            f"[bold cyan]A股量化策略历史回测报告[/bold cyan] | [yellow]{s['strategy_name']}[/yellow] "
            f"({s['start_date']} ~ {s['end_date']}, 共 {s['trading_days']} 交易日)",
            border_style="cyan"
        ))

        # 核心指标表格
        t = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
        t.add_column("量化绩效指标", style="dim")
        t.add_column("策略数值", justify="right")
        t.add_column("基准对比 / 说明", style="dim")

        ret_color = "red" if s["total_return_pct"] >= 0 else "green"
        t.add_row("初始资金", f"¥{s['initial_cash']:,.2f}", "起始模拟资金")
        t.add_row("期末总资产", f"¥{s['final_equity']:,.2f}", f"净盈利: ¥{s['total_pnl']:+,.2f}")
        t.add_row("累计收益率", f"[{ret_color}]{s['total_return_pct']:+.2f}%[/{ret_color}]", f"同期沪深300: {s['benchmark_return_pct']:+.2f}%")
        t.add_row("超额收益 (Alpha)", f"[bold red]{s['alpha_pct']:+.2f}%[/bold red]", "跑赢沪深300指数超额")
        t.add_row("年化收益率 (CAGR)", f"[{ret_color}]{s['cagr_pct']:+.2f}%[/{ret_color}]", "年化复合增长率")
        t.add_row("最大回撤 (MDD)", f"[bold green]{s['max_drawdown_pct']:.2f}%[/bold green]", f"最大回撤金额: ¥{s['max_drawdown_val']:,.2f}")
        t.add_row("夏普比率 (Sharpe)", f"{s['sharpe_ratio']:.2f}", "无风险利率按 2.0% 计")
        t.add_row("卡玛比率 (Calmar)", f"{s['calmar_ratio']:.2f}", "年化收益 / 最大回撤")
        t.add_row("交易胜率 (Win Rate)", f"{s['win_rate']:.1f}%", f"{s['win_count']}胜 {s['loss_count']}负 (共{s['total_trades']}笔)")
        t.add_row("盈亏比 (P/L Ratio)", f"{s['profit_loss_ratio']:.2f}", f"均赢 ¥{s['avg_win']:,.2f} / 均亏 ¥{s['avg_loss']:,.2f}")
        t.add_row("平均持仓天数", f"{s['avg_holding_days']:.1f} 天", "严格执行超短线与波段纪律")
        t.add_row("累计扣除税费", f"¥{s['total_fees']:,.2f}", "券商佣金 + 印花税 + 过户费")

        console.print(t)

        # 近期交易清单
        trades = res.get("trades", [])
        if trades:
            tt = Table(title=f"历史交易明细 (共 {len(trades)} 笔，展示近 15 笔)", box=box.SIMPLE_HEAVY)
            tt.add_column("标的代码", style="cyan")
            tt.add_column("股票名称")
            tt.add_column("买入时间")
            tt.add_column("买入价", justify="right")
            tt.add_column("卖出时间")
            tt.add_column("卖出价", justify="right")
            tt.add_column("持仓天数", justify="right")
            tt.add_column("净盈亏", justify="right")
            tt.add_column("收益率", justify="right")
            tt.add_column("出场原因")

            for tr in trades[-15:]:
                c = "red" if tr["net_pnl"] >= 0 else "green"
                tt.add_row(
                    tr["symbol"],
                    tr["name"],
                    tr["buy_date"],
                    f"¥{tr['buy_price']:.2f}",
                    tr["sell_date"],
                    f"¥{tr['sell_price']:.2f}",
                    f"{tr['holding_days']}天",
                    f"[{c}]¥{tr['net_pnl']:+,.2f}[/{c}]",
                    f"[{c}]{tr['pnl_pct']:+.2f}%[/{c}]",
                    tr["reason"]
                )
            console.print(tt)
    else:
        print("=" * 60)
        print(f"回测结果: {s['strategy_name']} ({s['start_date']} ~ {s['end_date']})")
        print(f"累计收益率: {s['total_return_pct']:+.2f}% (沪深300: {s['benchmark_return_pct']:+.2f}%, Alpha: {s['alpha_pct']:+.2f}%)")
        print(f"年化复合收益率 (CAGR): {s['cagr_pct']:+.2f}% | 最大回撤: {s['max_drawdown_pct']:.2f}%")
        print(f"夏普比率: {s['sharpe_ratio']:.2f} | 交易胜率: {s['win_rate']:.1f}% ({s['win_count']}胜/{s['loss_count']}负) | 盈亏比: {s['profit_loss_ratio']:.2f}")
        print(f"总交易笔数: {s['total_trades']} | 累计税费: ¥{s['total_fees']:,.2f}")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="A股量化策略历史回测与收益率测试系统")
    parser.add_argument("--start", default="2024-01-01", help="回测起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-09-15", help="回测截止日期 (YYYY-MM-DD)")
    parser.add_argument("--cash", type=float, default=100000.0, help="初始资金 (默认 100000)")
    parser.add_argument(
        "--strategy",
        default="Minervini_SEPA",
        choices=["Minervini_SEPA", "ONeil_CANSLIM", "TurtleBreakout", "MATrendFollowing", "ShortTermResonance", "MomentumBreakout"],
        help="回测策略名称"
    )
    parser.add_argument("--pool", default="core_active", choices=["core_active", "csi300_sample"], help="回测标的池")
    parser.add_argument("--take-profit", type=float, default=3.5, help="动态追踪止盈触发点 (%)")
    parser.add_argument("--stop-loss", type=float, default=-2.5, help="硬止损线 (%)")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    args = parser.parse_args()

    res = run_backtest(
        start_date=args.start,
        end_date=args.end,
        initial_cash=args.cash,
        strategy_name=args.strategy,
        pool_type=args.pool,
        take_profit_trigger_pct=args.take_profit,
        stop_loss_pct=args.stop_loss
    )

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_backtest_report(res)


if __name__ == "__main__":
    main()

