#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Product AB v2.0 统一逐笔撮合历史回测与消融实验套件
严格继承生产环境真实撮合引擎 (UnifiedSingleAccountEngine)，实现：
1. V0_v1_0: 原 Product AB v1.0 基准 (必须与统一回测基准 +50.67% 100%一致)
2. Balanced_v2_0: Product AB Balanced v2.0 (稳健增长)
3. Aggressive_v2_0: Product B Aggressive v2.0 (积极进攻)
4. Balanced_v2_no_floor: 消融实验 - Balanced 无 +3% 浮盈保护底线
5. Aggressive_v2_35atr: 消融实验 - Aggressive 采用 3.5 ATR (原宽幅)
6. Aggressive_v2_1day_ma20: 消融实验 - Aggressive 采用单日跌破 MA20 退出
"""

import os
import sys
import json
import math
import glob
import time
import csv
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple

sys.path.insert(0, '/home/openarmx/test')

from history_data import HistoricalDataFeed

CACHE_DIR = Path("/home/openarmx/test/data/history_cache")
BENCH_300_PATH = CACHE_DIR / "idx_000300_none.json"
BENCH_500_PATH = CACHE_DIR / "sz000905_qfq.json"
BENCH_1000_PATH = CACHE_DIR / "sz000852_qfq.json"
IND_MAP_PATH = CACHE_DIR / "industry_map_eastmoney.json"
OUTPUT_DIR = Path("/home/openarmx/test/research_audit/v2_backtest_results")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 生产 Product A 标的池
_feed_temp = HistoricalDataFeed()
PRODUCT_A_CORE = [s for s in _feed_temp.get_preset_symbols("core_active") if s.startswith(("sh60", "sz00"))]

REGIME_CAPS_V0 = {
    "RISK_OFF": 0.0,
    "NEUTRAL": 0.35,
    "RISK_ON": 0.70,
    "STRONG_RISK_ON": 0.90
}

FROZEN_AB_WEIGHTS_V0 = {
    "RISK_OFF": (1.00, 0.00),
    "NEUTRAL": (1.00, 0.00),
    "RISK_ON": (0.20, 0.80),
    "STRONG_RISK_ON": (0.50, 0.50)
}


class V2UnifiedBacktestSuite:
    def __init__(self, start_date="2024-01-02", end_date="2026-11-06", initial_cash=100000.0):
        self.start_date = start_date
        self.end_date = end_date
        self.initial_cash = initial_cash
        self.annual_cash_yield = 0.025
        self.slippage_rate = 0.001
        self.hard_stop_loss_pct = -0.06
        self.trailing_stop_activation_pct = 0.20
        self.atr_multiplier = 3.5
        self.min_holding_days = 5
        self.rebalance_every = 5
        self.load_data()
        self._precompute_market_states()

    def _calc_fee(self, side: str, amount: float) -> Tuple[float, Dict[str, float]]:
        commission = max(5.0, amount * 0.00025)
        transfer_fee = amount * 0.00001
        stamp_tax = amount * 0.0005 if side == "SELL" else 0.0
        tot = commission + transfer_fee + stamp_tax
        return tot, {"commission": commission, "transfer": transfer_fee, "tax": stamp_tax}

    def load_data(self):
        t0 = time.time()
        print("[*] Loading benchmark index data...")
        with open(IND_MAP_PATH, "r", encoding="utf-8") as f:
            ind_raw = json.load(f)
        self.industry_map = ind_raw.get("industries", {})

        def load_idx(path):
            with open(path) as f:
                d = json.load(f)
            bars = d.get("bars", d)
            bars_sorted = sorted(bars, key=lambda x: x["date"])
            return {b["date"]: b for b in bars_sorted}, [b["date"] for b in bars_sorted], bars_sorted

        self.csi300_by_date, self.dates300, self.bars300 = load_idx(BENCH_300_PATH)
        self.csi500_by_date, self.dates500, self.bars500 = load_idx(BENCH_500_PATH)
        self.csi1000_by_date, self.dates1000, self.bars1000 = load_idx(BENCH_1000_PATH)

        self.eval_dates = [d for d in self.dates300 if self.start_date <= d <= self.end_date
                           and d in self.csi500_by_date and d in self.csi1000_by_date]
        print(f"[*] Evaluation trading days: {len(self.eval_dates)} (from {self.eval_dates[0]} to {self.eval_dates[-1]})")

        print(f"[*] Loading stock cache from {CACHE_DIR}...")
        stock_files = glob.glob(str(CACHE_DIR / "*_qfq.json"))
        self.stocks = {}
        for fpath in stock_files:
            sym = os.path.basename(fpath).replace("_qfq.json", "")
            code = sym[-6:]
            if not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as fp:
                    d = json.load(fp)
                bars = d.get("bars", d)
                if not bars or len(bars) < 125:
                    continue
                name = d.get("name", sym)
                if "ST" in name.upper() or "退" in name:
                    continue

                bars_sorted = sorted(bars, key=lambda x: x["date"])
                n = len(bars_sorted)
                closes = [float(b["close"]) for b in bars_sorted]
                opens = [float(b["open"]) for b in bars_sorted]
                highs = [float(b["high"]) for b in bars_sorted]
                lows = [float(b["low"]) for b in bars_sorted]
                amounts = [float(b.get("amount", 0.0) or 0.0) for b in bars_sorted]
                raw_opens = [float(b.get("raw_open", b["open"])) for b in bars_sorted]
                raw_closes = [float(b.get("raw_close", b["close"])) for b in bars_sorted]
                raw_highs = [float(b.get("raw_high", b["high"])) for b in bars_sorted]
                raw_lows = [float(b.get("raw_low", b["low"])) for b in bars_sorted]

                atr = [0.0] * n
                for i in range(14, n):
                    tr_sum = sum(max(highs[j] - lows[j], abs(highs[j] - closes[j-1]), abs(lows[j] - closes[j-1]))
                                 for j in range(i-13, i+1))
                    atr[i] = tr_sum / 14.0

                self.stocks[sym] = {
                    "name": name,
                    "industry": self.industry_map.get(sym, "其他"),
                    "bars": bars_sorted,
                    "closes": closes,
                    "opens": opens,
                    "highs": highs,
                    "lows": lows,
                    "amounts": amounts,
                    "raw_opens": raw_opens,
                    "raw_closes": raw_closes,
                    "raw_highs": raw_highs,
                    "raw_lows": raw_lows,
                    "atr": atr,
                    "date_to_idx": {b["date"]: i for i, b in enumerate(bars_sorted)}
                }
            except Exception:
                continue
        print(f"[*] Loaded {len(self.stocks)} valid main-board stocks in {time.time()-t0:.2f}s.")

    def _precompute_market_states(self):
        print("[*] Precomputing daily CSI300 market regimes...")
        self.index_regimes = {}
        for i, b in enumerate(self.bars300):
            d = b["date"]
            if i < 120:
                continue
            c300 = float(b["close"])
            ma20 = sum(float(self.bars300[j]["close"]) for j in range(i-19, i+1)) / 20.0
            ma60 = sum(float(self.bars300[j]["close"]) for j in range(i-59, i+1)) / 60.0
            ma120 = sum(float(self.bars300[j]["close"]) for j in range(i-119, i+1)) / 120.0

            if c300 <= ma120:
                base_regime = "RISK_OFF"
            elif c300 <= ma20:
                base_regime = "NEUTRAL"
            elif ma20 <= ma60:
                base_regime = "RISK_ON"
            else:
                base_regime = "STRONG_RISK_ON"

            self.index_regimes[d] = {
                "base_regime": base_regime,
                "c300": c300,
                "ma20": ma20,
                "ma60": ma60,
                "ma120": ma120
            }

    def _eval_signal(self, sym: str, dt_str: str) -> Optional[Dict[str, Any]]:
        sinfo = self.stocks.get(sym)
        if not sinfo:
            return None
        b_idx = sinfo["date_to_idx"].get(dt_str)
        if b_idx is None or b_idx < 125:
            return None

        cl = sinfo["closes"][b_idx]
        op = sinfo["opens"][b_idx]
        if cl < op:
            return None  # 阳线过滤

        mean_amt = sum(sinfo["amounts"][b_idx-19:b_idx+1]) / 20.0
        if mean_amt < 30000000.0:
            return None  # 流动性过滤

        c_20 = sinfo["closes"][b_idx-20]
        c_60 = sinfo["closes"][b_idx-60]
        if c_20 <= 0 or c_60 <= 0:
            return None

        ret20 = (cl / c_20 - 1.0) * 100.0
        ret60 = (cl / c_60 - 1.0) * 100.0
        if ret20 <= 0 or ret60 <= 0:
            return None  # 动量为正

        ma20 = sum(sinfo["closes"][b_idx-19:b_idx+1]) / 20.0
        ma60 = sum(sinfo["closes"][b_idx-59:b_idx+1]) / 60.0
        ma120 = sum(sinfo["closes"][b_idx-119:b_idx+1]) / 120.0
        prev_ma60 = sum(sinfo["closes"][b_idx-64:b_idx-4]) / 60.0 if b_idx >= 65 else ma60
        high20 = max(sinfo["highs"][b_idx-19:b_idx+1])

        # 趋势与新高过滤
        if not (cl > ma20 > ma60 and ma60 > prev_ma60 and cl > ma120):
            return None
        if cl < 0.95 * high20:
            return None

        score = ret20 * 2.0 + ret60 * 1.0
        atr_val = sinfo["atr"][b_idx]
        atr_pct = (atr_val / cl) if cl > 0 else 0.035

        return {
            "symbol": sym,
            "name": sinfo["name"],
            "industry": sinfo["industry"],
            "score": score,
            "ret20": ret20,
            "ret60": ret60,
            "close": cl,
            "ma20": ma20,
            "ma60": ma60,
            "ma120": ma120,
            "atr": atr_val,
            "atr_pct": atr_pct
        }

    def run_simulation(self, variant: str) -> Dict[str, Any]:
        """运行单账户逐笔撮合回测"""
        cash = self.initial_cash
        positions: Dict[str, Dict[str, Any]] = {}
        closed_trades: List[Dict[str, Any]] = []
        orders_log: List[Dict[str, Any]] = []
        daily_nav: List[Dict[str, Any]] = []

        prior_dates = {}
        for d in self.eval_dates:
            idx_in_b300 = next(k for k, b in enumerate(self.bars300) if b["date"] == d)
            prior_dates[d] = self.bars300[idx_in_b300 - 1]["date"] if idx_in_b300 > 0 else None

        active_dynamic_b_pool = None
        peak_equity = self.initial_cash
        freeze_new_entries = False

        exit_counts = defaultdict(int)
        profit_givebacks = []
        regime_trades = defaultdict(list)

        for day_idx, date in enumerate(self.eval_dates):
            as_of = prior_dates[date]
            if not as_of:
                continue

            rebalance_day = (day_idx % self.rebalance_every == 0)

            # 1. 判定早盘 Market Regime
            reg_info = self.index_regimes.get(as_of)
            base_regime = reg_info["base_regime"] if reg_info else "RISK_OFF"
            effective_regime = base_regime

            # 参数配置
            if variant == "V0_v1_0":
                max_stock_cap = REGIME_CAPS_V0.get(base_regime, 0.0)
                allowed_new_buys = (base_regime != "RISK_OFF")
                w_a, w_b = FROZEN_AB_WEIGHTS_V0.get(base_regime, (1.00, 0.00))
                max_pos = 4 if effective_regime == "STRONG_RISK_ON" else 3
                max_single_stock_wt = 0.35
                max_sector_wt = 0.35
                atr_mult = 3.5
                use_floor = False
                ma20_consecutive = 1
                review_drawdown_thresh = 1.0
            elif variant in ("Balanced_v2", "Balanced_no_floor"):
                caps = {"RISK_OFF": 0.30, "NEUTRAL": 0.45, "RISK_ON": 0.65, "STRONG_RISK_ON": 0.80}
                max_stock_cap = caps.get(base_regime, 0.0)
                allowed_new_buys = True
                ab_ratios = {"RISK_OFF": (0.70, 0.30), "NEUTRAL": (0.60, 0.40), "RISK_ON": (0.20, 0.80), "STRONG_RISK_ON": (0.50, 0.50)}
                w_a, w_b = ab_ratios.get(base_regime, (0.70, 0.30))
                max_pos = 3
                max_single_stock_wt = 0.15
                max_sector_wt = 0.30
                atr_mult = 3.5
                use_floor = (variant == "Balanced_v2")
                ma20_consecutive = 1
                review_drawdown_thresh = 0.12
            elif variant in ("Aggressive_v2", "Aggressive_35atr", "Aggressive_1day_ma20"):
                caps = {"RISK_OFF": 0.50, "NEUTRAL": 0.70, "RISK_ON": 0.90, "STRONG_RISK_ON": 1.00}
                max_stock_cap = caps.get(base_regime, 0.0)
                allowed_new_buys = True
                w_a, w_b = (0.0, 1.0)
                max_pos = 5
                max_single_stock_wt = 0.25
                max_sector_wt = 0.40
                atr_mult = 3.5 if variant == "Aggressive_35atr" else 4.5
                use_floor = False
                ma20_consecutive = 1 if variant == "Aggressive_1day_ma20" else 2
                review_drawdown_thresh = 0.20

            # 2. 候选池维护 (无条件在调仓日更新，消除状态污染)
            if rebalance_day or active_dynamic_b_pool is None:
                qualified_by_ind = defaultdict(list)
                for sym, sinfo in self.stocks.items():
                    if sym in PRODUCT_A_CORE:
                        continue
                    sig = self._eval_signal(sym, as_of)
                    if sig:
                        qualified_by_ind[sig["industry"]].append(sig)

                ind_stats = []
                for ind, s_list in qualified_by_ind.items():
                    if len(s_list) < 3:
                        continue
                    avg_ret20 = sum(s["ret20"] for s in s_list) / len(s_list)
                    breadth_ma20 = sum(1 for s in s_list if s["close"] > s["ma20"]) / len(s_list)
                    if avg_ret20 > 0 and breadth_ma20 >= 0.40:
                        ind_score = avg_ret20 * (1.0 + breadth_ma20)
                        ind_stats.append((ind, ind_score, breadth_ma20, s_list))

                ind_stats.sort(key=lambda x: x[1], reverse=True)
                top4_inds = ind_stats[:4]

                active_dynamic_b_pool = []
                for ind, iscore, b_ma20, s_list in top4_inds:
                    s_list.sort(key=lambda x: x["score"], reverse=True)
                    active_dynamic_b_pool.extend([s["symbol"] for s in s_list[:12]])

            candidates_b = []
            if w_b > 0 and active_dynamic_b_pool:
                for sym in active_dynamic_b_pool:
                    sig = self._eval_signal(sym, as_of)
                    if sig:
                        candidates_b.append(sig)
                candidates_b.sort(key=lambda x: x["score"], reverse=True)

            candidates_a = []
            if w_a > 0:
                for sym in PRODUCT_A_CORE:
                    sig = self._eval_signal(sym, as_of)
                    if sig:
                        candidates_a.append(sig)
                candidates_a.sort(key=lambda x: x["score"], reverse=True)

            # RISK_OFF 专属强化过滤 (收盘>MA60, 收盘>MA120, ret20>0, ret60>0)
            if base_regime == "RISK_OFF" and variant != "V0_v1_0":
                candidates_a = [c for c in candidates_a if c["close"] > c["ma60"] and c["close"] > c["ma120"] and c["ret20"] > 0 and c["ret60"] > 0]
                candidates_b = [c for c in candidates_b if c["close"] > c["ma60"] and c["close"] > c["ma120"] and c["ret20"] > 0 and c["ret60"] > 0]

            # 3. 检查现有持仓的退出信号 (开盘执行)
            exited_symbols = []
            for sym, pos in list(positions.items()):
                sinfo = self.stocks.get(sym)
                if not sinfo:
                    continue
                d_idx = sinfo["date_to_idx"].get(date)
                p_idx = sinfo["date_to_idx"].get(as_of)
                if d_idx is None or p_idx is None:
                    continue

                raw_open = sinfo["raw_opens"][d_idx]
                raw_close = sinfo["raw_closes"][d_idx]
                raw_high = sinfo["raw_highs"][d_idx]
                raw_low = sinfo["raw_lows"][d_idx]
                prev_raw_close = sinfo["raw_closes"][p_idx]
                prev_close = sinfo["closes"][p_idx]
                prev_ma20 = sum(sinfo["closes"][p_idx-19:p_idx+1]) / 20.0

                pos["peak_raw_price"] = max(pos["peak_raw_price"], raw_high)
                cost_p = pos["cost_price"]

                exit_reason = None
                exit_price = raw_open

                # 3.1 跌破 MA20 退出
                if ma20_consecutive == 1:
                    # 单日收盘跌破 MA20 次日开盘卖出
                    if prev_close < prev_ma20:
                        exit_reason = "跌破MA20退出"
                        exit_price = raw_open
                elif ma20_consecutive == 2:
                    # 连续两日收盘低于 MA20 99%
                    if p_idx >= 21:
                        p_idx2 = p_idx - 1
                        prev2_close = sinfo["closes"][p_idx2]
                        prev2_ma20 = sum(sinfo["closes"][p_idx2-19:p_idx2+1]) / 20.0
                        if prev_close < prev_ma20 * 0.99 and prev2_close < prev2_ma20 * 0.99:
                            exit_reason = "连续两日跌破MA20退出"
                            exit_price = raw_open

                # 3.2 成本硬止损 -6%
                if not exit_reason and (raw_low / cost_p - 1.0) <= self.hard_stop_loss_pct:
                    exit_reason = "硬止损触发"
                    exit_price = min(raw_open, cost_p * (1.0 + self.hard_stop_loss_pct))

                # 3.3 动态 ATR 跟踪止盈 (最高浮盈 >= 20%)
                peak_pnl_pct = (pos["peak_raw_price"] / cost_p - 1.0) * 100.0
                pos["peak_profit_pct"] = max(pos.get("peak_profit_pct", 0.0), peak_pnl_pct)

                if not exit_reason and (pos["peak_raw_price"] / cost_p - 1.0) >= self.trailing_stop_activation_pct:
                    atr_drop = atr_mult * pos["atr"]
                    atr_stop_level = pos["peak_raw_price"] - atr_drop

                    if use_floor:
                        floor_level = cost_p * 1.03
                        cand_level = max(atr_stop_level, floor_level)
                    else:
                        floor_level = None
                        cand_level = atr_stop_level

                    pos["trailing_stop_line"] = max(pos.get("trailing_stop_line", 0.0), cand_level)
                    effective_stop = pos["trailing_stop_line"]

                    if raw_low <= effective_stop:
                        exit_reason = "浮盈保护锁定(+3%)" if (use_floor and effective_stop >= floor_level) else "动态ATR止盈"
                        exit_price = min(raw_open, effective_stop)

                # 3.4 定期调仓轮动退出 (持有 >= 5 天且跌出前列)
                if not exit_reason and rebalance_day:
                    holding_d = day_idx - pos["entry_day_idx"]
                    if holding_d >= self.min_holding_days:
                        top_syms = [c["symbol"] for c in (candidates_a if pos["sub_engine"] == "A" else candidates_b)[:max_pos]]
                        if sym not in top_syms:
                            if variant.startswith("Aggressive") and prev_close > prev_ma20 and (prev_close / cost_p - 1.0) > 0.05:
                                pass  # 仍处强势主升浪，保留持仓
                            else:
                                exit_reason = "调仓动量轮动退出"
                                exit_price = raw_open

                # 检查跌停不可卖出保护
                limit_down_p = round(prev_raw_close * 0.90, 2)
                if raw_open <= limit_down_p and raw_low <= limit_down_p and raw_high <= limit_down_p:
                    exit_reason = None

                if exit_reason:
                    exec_price = exit_price * (1.0 - self.slippage_rate)
                    gross_amt = pos["shares"] * exec_price
                    fee, _ = self._calc_fee("SELL", gross_amt)
                    net_proceeds = gross_amt - fee
                    cash += net_proceeds
                    pnl = net_proceeds - pos["shares"] * pos["cost_price"]
                    pnl_pct = (exec_price / pos["cost_price"] - 1.0) * 100.0

                    giveback = max(0.0, pos["peak_profit_pct"] - pnl_pct) if pos["peak_profit_pct"] > 0 else 0.0
                    profit_givebacks.append(giveback)
                    exit_counts[exit_reason] += 1
                    regime_trades[pos["entry_regime"]].append({"pnl": pnl, "pnl_pct": pnl_pct})

                    closed_trades.append({
                        "trade_id": f"T_SELL_{len(closed_trades)+1:04d}",
                        "date": date,
                        "symbol": sym,
                        "name": pos["name"],
                        "side": "SELL",
                        "shares": pos["shares"],
                        "price": round(exec_price, 2),
                        "amount": round(gross_amt, 2),
                        "fees": round(fee, 2),
                        "slippage": round(gross_amt * self.slippage_rate, 2),
                        "realized_pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "peak_profit_pct": round(pos["peak_profit_pct"], 2),
                        "giveback_pct": round(giveback, 2),
                        "holding_days": day_idx - pos["entry_day_idx"],
                        "entry_regime": pos["entry_regime"],
                        "reason": exit_reason
                    })
                    orders_log.append({
                        "order_id": f"O_{len(orders_log)+1:04d}",
                        "date": date,
                        "symbol": sym,
                        "name": pos["name"],
                        "side": "SELL",
                        "shares": pos["shares"],
                        "price": round(exec_price, 2),
                        "amount": round(gross_amt, 2),
                        "status": "FILLED",
                        "reason": exit_reason
                    })
                    exited_symbols.append(sym)

            for sym in exited_symbols:
                del positions[sym]

            # 4. 评估开仓意图与资金分配
            current_stk_val = sum(
                p["shares"] * self.stocks[s]["raw_opens"][self.stocks[s]["date_to_idx"][date]]
                for s, p in positions.items()
                if date in self.stocks[s]["date_to_idx"]
            )
            total_eq_open = cash + current_stk_val
            peak_equity = max(peak_equity, total_eq_open)
            current_drawdown = (peak_equity - total_eq_open) / peak_equity if peak_equity > 0 else 0.0

            if current_drawdown >= review_drawdown_thresh:
                freeze_new_entries = True
            elif current_drawdown < review_drawdown_thresh * 0.7:
                freeze_new_entries = False

            current_stk_wt = (current_stk_val / total_eq_open) if total_eq_open > 0 else 0.0
            remaining_allowed_wt = max(0.0, max_stock_cap - current_stk_wt)
            vacancies = max(0, max_pos - len(positions))

            can_buy_today = (rebalance_day or len(positions) == 0) and allowed_new_buys and not freeze_new_entries
            if can_buy_today and vacancies > 0 and remaining_allowed_wt >= 0.05:
                buy_targets = []
                if w_a > 0 and candidates_a:
                    buy_targets.append((candidates_a[0], "A"))
                if w_b > 0 and candidates_b:
                    for cb in candidates_b[:2]:
                        buy_targets.append((cb, "B"))
                if not buy_targets and w_b > 0 and candidates_b:
                    for cb in candidates_b[:vacancies]:
                        buy_targets.append((cb, "B"))

                sector_vals = defaultdict(float)
                for s, p in positions.items():
                    s_ind = self.stocks[s]["industry"]
                    s_open = self.stocks[s]["raw_opens"][self.stocks[s]["date_to_idx"][date]]
                    sector_vals[s_ind] += p["shares"] * s_open

                for cand, sub_eng in buy_targets[:vacancies]:
                    if remaining_allowed_wt < 0.05 or vacancies <= 0:
                        break
                    sym = cand["symbol"]
                    if sym in positions:
                        continue

                    sinfo = self.stocks.get(sym)
                    if not sinfo or date not in sinfo["date_to_idx"] or as_of not in sinfo["date_to_idx"]:
                        continue

                    d_idx = sinfo["date_to_idx"][date]
                    p_idx = sinfo["date_to_idx"][as_of]
                    raw_open = sinfo["raw_opens"][d_idx]
                    prev_raw_close = sinfo["raw_closes"][p_idx]

                    limit_up_p = round(prev_raw_close * 1.099, 2)
                    if raw_open >= limit_up_p:
                        continue

                    target_wt = min(max_single_stock_wt, remaining_allowed_wt / vacancies)
                    ind = sinfo["industry"]
                    allowed_sec_wt = max(0.0, max_sector_wt - (sector_vals[ind] / total_eq_open))
                    target_wt = min(target_wt, allowed_sec_wt)

                    if target_wt < 0.05:
                        continue

                    exec_price = raw_open * (1.0 + self.slippage_rate)
                    target_amt = total_eq_open * target_wt
                    invest_budget = min(target_amt, allowed_sec_wt * total_eq_open, cash / 1.0005)
                    shares = int(invest_budget / exec_price / 100) * 100

                    if shares >= 100:
                        gross_amt = shares * exec_price
                        fee, fee_dict = self._calc_fee("BUY", gross_amt)
                        tot_cost = gross_amt + fee
                        if cash >= tot_cost:
                            cash -= tot_cost
                            positions[sym] = {
                                "symbol": sym,
                                "name": cand["name"],
                                "industry": ind,
                                "shares": shares,
                                "cost_price": round(exec_price, 2),
                                "peak_raw_price": raw_open,
                                "trailing_stop_line": 0.0,
                                "entry_date": date,
                                "entry_day_idx": day_idx,
                                "entry_regime": base_regime,
                                "sub_engine": sub_eng,
                                "atr": cand["atr"]
                            }
                            sector_vals[ind] += gross_amt
                            remaining_allowed_wt -= (gross_amt / total_eq_open)
                            orders_log.append({
                                "order_id": f"O_{len(orders_log)+1:04d}",
                                "date": date,
                                "symbol": sym,
                                "name": cand["name"],
                                "side": "BUY",
                                "shares": shares,
                                "price": round(exec_price, 2),
                                "amount": round(gross_amt, 2),
                                "status": "FILLED",
                                "reason": f"{sub_eng}_开仓"
                            })

            # 5. 每日收盘对账与闲置资金计息
            eod_stock_val = sum(
                pos["shares"] * self.stocks[sym]["raw_closes"][self.stocks[sym]["date_to_idx"][date]]
                for sym, pos in positions.items()
            )
            daily_interest = round(cash * (self.annual_cash_yield / 252.0), 2)
            cash += daily_interest
            total_equity = cash + eod_stock_val

            daily_nav.append({
                "date": date,
                "cash": round(cash, 2),
                "stock_val": round(eod_stock_val, 2),
                "equity": round(total_equity, 2),
                "stock_wt": round((eod_stock_val / total_equity) * 100, 2) if total_equity > 0 else 0.0,
                "positions_count": len(positions),
                "regime": base_regime
            })

        navs = [d["equity"] for d in daily_nav]
        final_equity = navs[-1]
        cum_ret = (final_equity / self.initial_cash - 1.0) * 100.0
        n_days = len(daily_nav)
        cagr = ((final_equity / self.initial_cash) ** (252.0 / n_days) - 1.0) * 100.0

        peak = navs[0]
        max_dd = 0.0
        for val in navs:
            if val > peak:
                peak = val
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd
        mdd_pct = max_dd * 100.0

        daily_rets = [(navs[i] / navs[i-1] - 1.0) for i in range(1, len(navs))]
        mean_ret = sum(daily_rets) / len(daily_rets)
        std_ret = math.sqrt(sum((r - mean_ret)**2 for r in daily_rets) / len(daily_rets))
        sharpe = ((mean_ret - 0.025 / 252.0) / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0
        calmar = (cagr / mdd_pct) if mdd_pct > 0 else 0.0

        wins = [t for t in closed_trades if t["realized_pnl"] > 0]
        losses = [t for t in closed_trades if t["realized_pnl"] <= 0]
        win_rate = (len(wins) / len(closed_trades) * 100.0) if closed_trades else 0.0

        gross_win = sum(t["realized_pnl"] for t in wins)
        gross_loss = abs(sum(t["realized_pnl"] for t in losses))
        pl_ratio = (gross_win / gross_loss) if gross_loss > 0 else 0.0

        avg_win = (gross_win / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        avg_win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0

        avg_stock_wt = sum(d["stock_wt"] for d in daily_nav) / len(daily_nav)
        avg_giveback = sum(profit_givebacks) / len(profit_givebacks) if profit_givebacks else 0.0

        total_buy_vol = sum(t["shares"] * t["price"] for t in closed_trades)
        total_sell_vol = sum(t["shares"] * t["price"] for t in closed_trades)
        turnover_annual = ((total_buy_vol + total_sell_vol) / 2.0 / self.initial_cash) / (n_days / 252.0)
        tot_fees = sum(t.get("fees", 0.0) for t in closed_trades)

        regime_breakdown = {}
        for reg in ("RISK_OFF", "NEUTRAL", "RISK_ON", "STRONG_RISK_ON"):
            r_trades = regime_trades.get(reg, [])
            r_wins = [t for t in r_trades if t["pnl"] > 0]
            regime_breakdown[reg] = {
                "trades": len(r_trades),
                "win_rate": round(len(r_wins) / len(r_trades) * 100.0, 2) if r_trades else 0.0,
                "net_pnl": round(sum(t["pnl"] for t in r_trades), 2)
            }

        return {
            "variant": variant,
            "final_equity": round(final_equity, 2),
            "cum_return_pct": round(cum_ret, 2),
            "cagr_pct": round(cagr, 2),
            "mdd_pct": round(mdd_pct, 2),
            "sharpe": round(sharpe, 2),
            "calmar": round(calmar, 2),
            "win_rate_pct": round(win_rate, 2),
            "pl_ratio": round(pl_ratio, 2),
            "avg_win_loss_ratio": round(avg_win_loss_ratio, 2),
            "avg_stock_weight_pct": round(avg_stock_wt, 2),
            "trade_count": len(closed_trades),
            "annualized_turnover": round(turnover_annual, 2),
            "total_fees": round(tot_fees, 2),
            "exit_counts": dict(exit_counts),
            "avg_giveback_pct": round(avg_giveback, 2),
            "regime_breakdown": regime_breakdown,
            "daily_nav": daily_nav,
            "closed_trades": closed_trades,
            "orders": orders_log
        }


def main():
    suite = V2UnifiedBacktestSuite()
    variants = [
        "V0_v1_0",
        "Balanced_v2",
        "Aggressive_v2",
        "Balanced_no_floor",
        "Aggressive_35atr",
        "Aggressive_1day_ma20"
    ]

    summary_rows = []
    for v in variants:
        t0 = time.time()
        res = suite.run_simulation(v)
        elapsed = time.time() - t0

        trades_csv = OUTPUT_DIR / f"{v.lower()}_trades.csv"
        with open(trades_csv, "w", newline="", encoding="utf-8") as f:
            if res["closed_trades"]:
                writer = csv.DictWriter(f, fieldnames=list(res["closed_trades"][0].keys()))
                writer.writeheader()
                writer.writerows(res["closed_trades"])

        nav_csv = OUTPUT_DIR / f"{v.lower()}_daily_nav.csv"
        with open(nav_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["date", "cash", "stock_val", "equity", "stock_wt", "positions_count", "regime"])
            writer.writeheader()
            writer.writerows(res["daily_nav"])

        row = {
            "Variant": v,
            "CumReturn(%)": res["cum_return_pct"],
            "CAGR(%)": res["cagr_pct"],
            "MDD(%)": res["mdd_pct"],
            "Sharpe": res["sharpe"],
            "Calmar": res["calmar"],
            "WinRate(%)": res["win_rate_pct"],
            "PL_Ratio": res["pl_ratio"],
            "AvgStockWt(%)": res["avg_stock_weight_pct"],
            "Trades": res["trade_count"],
            "Turnover": res["annualized_turnover"],
            "TotalFees": res["total_fees"],
            "AvgGiveback(%)": res["avg_giveback_pct"],
            "HardStopExits": res["exit_counts"].get("硬止损触发", 0),
            "ATRExits": res["exit_counts"].get("动态ATR止盈", 0),
            "FloorExits": res["exit_counts"].get("浮盈保护锁定(+3%)", 0),
            "MA20Exits": res["exit_counts"].get("跌破MA20退出", 0) + res["exit_counts"].get("连续两日跌破MA20退出", 0),
            "RotationExits": res["exit_counts"].get("调仓动量轮动退出", 0),
            "Regime_RISK_OFF_Trades": res["regime_breakdown"]["RISK_OFF"]["trades"],
            "Regime_RISK_OFF_PnL": res["regime_breakdown"]["RISK_OFF"]["net_pnl"],
            "Regime_NEUTRAL_Trades": res["regime_breakdown"]["NEUTRAL"]["trades"],
            "Regime_NEUTRAL_PnL": res["regime_breakdown"]["NEUTRAL"]["net_pnl"],
            "Regime_RISK_ON_Trades": res["regime_breakdown"]["RISK_ON"]["trades"],
            "Regime_RISK_ON_PnL": res["regime_breakdown"]["RISK_ON"]["net_pnl"],
            "Regime_STRONG_Trades": res["regime_breakdown"]["STRONG_RISK_ON"]["trades"],
            "Regime_STRONG_PnL": res["regime_breakdown"]["STRONG_RISK_ON"]["net_pnl"],
        }
        summary_rows.append(row)
        print(f">> {v:<20} | Ret: {res['cum_return_pct']:>6.2f}% | MDD: {res['mdd_pct']:>5.2f}% | Sharpe: {res['sharpe']:>5.2f} | Trades: {res['trade_count']:>3d} ({elapsed:.2f}s)")

    comp_csv = OUTPUT_DIR / "v2_comparison_summary.csv"
    with open(comp_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)

    comp_json = OUTPUT_DIR / "v2_comparison_summary.json"
    with open(comp_json, "w", encoding="utf-8") as f:
        json.dump(summary_rows, f, indent=2, ensure_ascii=False)

    print("\n==================== BACKTEST COMPARISON SUMMARY ====================")
    header = f"{'Variant':<22} | {'Return':<8} | {'CAGR':<7} | {'MDD':<7} | {'Sharpe':<6} | {'Calmar':<6} | {'WinRate':<7} | {'PL_Ratio':<8} | {'Trades':<6}"
    print(header)
    print("-" * len(header))
    for r in summary_rows:
        print(f"{r['Variant']:<22} | {r['CumReturn(%)']:>6.2f}% | {r['CAGR(%)']:>5.2f}% | {r['MDD(%)']:>5.2f}% | {r['Sharpe']:>6.2f} | {r['Calmar']:>6.2f} | {r['WinRate(%)']:>6.2f}% | {r['PL_Ratio']:>8.2f} | {r['Trades']:>6d}")
    print("=====================================================================")


if __name__ == "__main__":
    main()
