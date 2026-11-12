# -*- coding: utf-8 -*-
"""
Product AB: 统一物理单账户回测引擎 (Unified Single-Account Benchmark Engine)
- 严格遵循 Product AB v1.0 生产冻结规范:
    1. 单物理账户 100,000 元，现金/持仓物理共享
    2. 最大持仓 3 只 (STRONG 模式 4 只)
    3. refill_vacancies = False (仅逢调仓日 rebalance_every = 5 开仓，禁止日常强制补齐)
    4. 调仓周期 5 天，最小持仓 5 天
    5. Regime 总仓位硬顶: RISK_OFF 0%, NEUTRAL 35%, RISK_ON 70%, STRONG 90%
    6. A/B 混动配比: RISK_OFF (1:0), NEUTRAL (1:0), RISK_ON (0.2:0.8), STRONG (0.5:0.5)
    7. 组合单票上限 35%, 行业上限 35%
    8. 风控执行: MA20 破位退出 (次日开盘卖出), 成本硬止损 -6%, 动态 ATR 跟踪止盈 (20% 激活, 3.5 ATR)
    9. 真实撮合: 次日 raw_open 撮合，双边滑点 0.10%，佣金 0.025% (最低 5 元) + 过户费 0.001% + 印花税 0.05%
    10. 严格 T+1，涨停禁止买入，跌停禁止止损卖出
    11. 闲置现金收益年化 2.5% 按日计息
"""

import sys
import os
import math
import copy
import json
import time
import hashlib
import glob
import datetime as dt
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

sys.path.insert(0, '/home/openarmx/test')

from history_data import HistoricalDataFeed

CACHE_DIR = Path("/home/openarmx/test/data/history_cache")
BENCH_300_PATH = CACHE_DIR / "idx_000300_none.json"
BENCH_500_PATH = CACHE_DIR / "sz000905_qfq.json"
BENCH_1000_PATH = CACHE_DIR / "sz000852_qfq.json"
IND_MAP_PATH = CACHE_DIR / "industry_map_eastmoney.json"

# 严格采用生产系统 product_ab_strategy.py 与 history_data.py 一致的 Core Active 标的池
_feed_temp = HistoricalDataFeed()
PRODUCT_A_CORE = [s for s in _feed_temp.get_preset_symbols("core_active") if s.startswith(("sh60", "sz00"))]

FROZEN_AB_WEIGHTS = {
    "RISK_OFF": (1.00, 0.00),
    "NEUTRAL": (1.00, 0.00),
    "RISK_ON": (0.20, 0.80),
    "STRONG_RISK_ON": (0.50, 0.50),
    "STRUCTURAL_ROTATION": (0.00, 1.00)
}

REGIME_CAPS = {
    "RISK_OFF": 0.00,
    "NEUTRAL": 0.35,
    "RISK_ON": 0.70,
    "STRONG_RISK_ON": 0.90
}


class UnifiedSingleAccountEngine:
    """统一物理单账户回测引擎"""

    def __init__(self,
                 start_date: str = "2024-01-02",
                 end_date: str = "2026-11-06",
                 initial_cash: float = 100000.0,
                 annual_cash_yield: float = 2.5,
                 commission_rate: float = 0.00025,
                 min_commission: float = 5.0,
                 transfer_fee_rate: float = 0.00001,
                 stamp_tax_rate: float = 0.0005,
                 slippage_rate: float = 0.0010):
        self.start_date = start_date
        self.end_date = end_date
        self.initial_cash = float(initial_cash)
        self.annual_cash_yield = float(annual_cash_yield)
        self.commission_rate = float(commission_rate)
        self.min_commission = float(min_commission)
        self.transfer_fee_rate = float(transfer_fee_rate)
        self.stamp_tax_rate = float(stamp_tax_rate)
        self.slippage_rate = float(slippage_rate)

        self.rebalance_every = 5
        self.min_holding_days = 5
        self.hard_stop_loss_pct = -0.06
        self.trailing_stop_activation_pct = 0.20
        self.atr_multiplier = 3.5

        self.load_data()

    def _calc_fee(self, side: str, amount: float) -> Tuple[float, Dict[str, float]]:
        comm = max(self.min_commission, amount * self.commission_rate)
        transfer = amount * self.transfer_fee_rate
        stamp = (amount * self.stamp_tax_rate) if side == "SELL" else 0.0
        tot = comm + transfer + stamp
        return tot, {"commission": round(comm, 2), "transfer_fee": round(transfer, 2), "stamp_tax": round(stamp, 2)}

    def load_data(self):
        t0 = time.time()
        print(f"[*] Loading benchmark index data...")
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

        # Load stocks
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
                    "dates": [b["date"] for b in bars_sorted],
                    "date_to_idx": {b["date"]: i for i, b in enumerate(bars_sorted)},
                    "atr": atr
                }
            except Exception:
                continue

        print(f"[*] Loaded {len(self.stocks)} valid stocks in {time.time()-t0:.2f}s")
        self._precompute_market_states()

    def _precompute_market_states(self):
        b300_idx = {b["date"]: i for i, b in enumerate(self.bars300)}
        b500_idx = {b["date"]: i for i, b in enumerate(self.bars500)}
        b1000_idx = {b["date"]: i for i, b in enumerate(self.bars1000)}

        self.index_regimes = {}
        for d in self.eval_dates:
            i3 = b300_idx[d]
            i5 = b500_idx[d]
            i1 = b1000_idx[d]

            c3 = float(self.bars300[i3]["close"])
            ma120_3 = sum(float(b["close"]) for b in self.bars300[i3-119:i3+1]) / 120.0
            ma60_3 = sum(float(b["close"]) for b in self.bars300[i3-59:i3+1]) / 60.0
            ma20_3 = sum(float(b["close"]) for b in self.bars300[i3-19:i3+1]) / 20.0
            prev_ma60_3 = sum(float(b["close"]) for b in self.bars300[i3-64:i3-4]) / 60.0 if i3 >= 65 else ma60_3

            c5 = float(self.bars500[i5]["close"])
            ma120_5 = sum(float(b["close"]) for b in self.bars500[i5-119:i5+1]) / 120.0

            c1 = float(self.bars1000[i1]["close"])
            ma120_1 = sum(float(b["close"]) for b in self.bars1000[i1-119:i1+1]) / 120.0

            if c3 <= ma120_3:
                base_regime = "RISK_OFF"
            elif c3 <= ma20_3:
                base_regime = "NEUTRAL"
            elif ma20_3 <= ma60_3:
                base_regime = "RISK_ON"
            else:
                base_regime = "STRONG_RISK_ON"

            self.index_regimes[d] = {
                "base_regime": base_regime,
                "c300": c3,
                "ma120_300": ma120_3,
                "c500_above_ma120": (c5 > ma120_5),
                "c1000_above_ma120": (c1 > ma120_1),
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
            return None

        mean_amt = sum(sinfo["amounts"][b_idx-19:b_idx+1]) / 20.0
        if mean_amt < 30000000.0:
            return None

        c_20 = sinfo["closes"][b_idx-20]
        c_60 = sinfo["closes"][b_idx-60]
        if c_20 <= 0 or c_60 <= 0:
            return None

        ret20 = (cl / c_20 - 1.0) * 100.0
        ret60 = (cl / c_60 - 1.0) * 100.0
        if ret20 <= 0 or ret60 <= 0:
            return None

        ma20 = sum(sinfo["closes"][b_idx-19:b_idx+1]) / 20.0
        ma60 = sum(sinfo["closes"][b_idx-59:b_idx+1]) / 60.0
        ma120 = sum(sinfo["closes"][b_idx-119:b_idx+1]) / 120.0
        prev_ma60 = sum(sinfo["closes"][b_idx-64:b_idx-4]) / 60.0 if b_idx >= 65 else ma60
        high20 = max(sinfo["highs"][b_idx-19:b_idx+1])

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
            "atr": atr_val,
            "atr_pct": atr_pct
        }

    def run_simulation(self, variant_name: str = "V0") -> Dict[str, Any]:
        """
        运行严格统一单账户逐笔撮合
        variant_name: 'V0', 'V1', 'V2_10', 'V2_20', 'V3'
        """
        cash = self.initial_cash
        positions: Dict[str, Dict[str, Any]] = {}
        closed_trades: List[Dict[str, Any]] = []
        orders_log: List[Dict[str, Any]] = []
        daily_nav: List[Dict[str, Any]] = []

        total_days = len(self.eval_dates)
        prior_dates = {}
        for i, d in enumerate(self.eval_dates):
            idx_in_b300 = next(k for k, b in enumerate(self.bars300) if b["date"] == d)
            prior_dates[d] = self.bars300[idx_in_b300 - 1]["date"] if idx_in_b300 > 0 else None

        active_dynamic_b_pool = None

        for day_idx, date in enumerate(self.eval_dates):
            as_of = prior_dates[date]
            if not as_of:
                continue

            rebalance_day = (day_idx % self.rebalance_every == 0)

            # 1. 判定早盘 Market Regime (基于 as_of 收盘数据)
            reg_info = self.index_regimes.get(as_of)
            base_regime = reg_info["base_regime"] if reg_info else "RISK_OFF"
            effective_regime = base_regime
            max_stock_cap = REGIME_CAPS.get(base_regime, 0.0)
            allowed_new_buys = (base_regime != "RISK_OFF")
            w_a, w_b = FROZEN_AB_WEIGHTS.get(base_regime, (1.00, 0.00))

            # 实验版本规则分支 (仅在 RISK_OFF 下差异化)
            if base_regime == "RISK_OFF":
                if variant_name == "V0":
                    max_stock_cap = 0.0
                    allowed_new_buys = False
                    w_a, w_b = 1.00, 0.00
                elif variant_name == "V1":
                    max_stock_cap = 0.10
                    allowed_new_buys = True
                    w_a, w_b = 0.00, 1.00
                elif variant_name == "V2_10":
                    max_stock_cap = 0.10
                    allowed_new_buys = "V2_CHECK"
                    w_a, w_b = 0.00, 1.00
                elif variant_name == "V2_20":
                    max_stock_cap = 0.20
                    allowed_new_buys = "V2_CHECK"
                    w_a, w_b = 0.00, 1.00
                elif variant_name == "V3":
                    c500_up = reg_info["c500_above_ma120"] if reg_info else False
                    c1000_up = reg_info["c1000_above_ma120"] if reg_info else False
                    if c500_up or c1000_up:
                        max_stock_cap = 0.20
                        allowed_new_buys = True
                        effective_regime = "STRUCTURAL_ROTATION"
                        w_a, w_b = 0.00, 1.00
                    else:
                        max_stock_cap = 0.0
                        allowed_new_buys = False
                        w_a, w_b = 1.00, 0.00

            # 2. 候选池筛选 (在 as_of 收盘后生成)
            # A 候选
            candidates_a = []
            if w_a > 0:
                for sym in PRODUCT_A_CORE:
                    sig = self._eval_signal(sym, as_of)
                    if sig:
                        candidates_a.append(sig)
                candidates_a.sort(key=lambda x: x["score"], reverse=True)

            # B 候选 (Top 4 行业 x 12 龙头)
            candidates_b = []
            if w_b > 0:
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
                    if allowed_new_buys == "V2_CHECK":
                        for ind, iscore, b_ma20, s_list in top4_inds:
                            if b_ma20 >= 0.50:
                                s_list.sort(key=lambda x: x["score"], reverse=True)
                                active_dynamic_b_pool.extend([s["symbol"] for s in s_list[:3]])
                    else:
                        for ind, iscore, b_ma20, s_list in top4_inds:
                            s_list.sort(key=lambda x: x["score"], reverse=True)
                            active_dynamic_b_pool.extend([s["symbol"] for s in s_list[:12]])

                for sym in active_dynamic_b_pool:
                    sig = self._eval_signal(sym, as_of)
                    if sig:
                        candidates_b.append(sig)
                candidates_b.sort(key=lambda x: x["score"], reverse=True)

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

                # 3.1 跌破 MA20 退出 (收盘破位，次日开盘卖出)
                if prev_close < prev_ma20:
                    exit_reason = "跌破MA20退出"
                    exit_price = raw_open
                # 3.2 成本硬止损 -6%
                elif (raw_low / cost_p - 1.0) <= self.hard_stop_loss_pct:
                    exit_reason = "硬止损触发"
                    exit_price = min(raw_open, cost_p * (1.0 + self.hard_stop_loss_pct))
                # 3.3 动态 ATR 跟踪止盈 (最高浮盈 >= 20%，自高点回落 3.5 * ATR)
                elif (pos["peak_raw_price"] / cost_p - 1.0) >= self.trailing_stop_activation_pct:
                    atr_drop = self.atr_multiplier * pos["atr"]
                    trail_level = pos["peak_raw_price"] - atr_drop
                    if raw_low <= trail_level:
                        exit_reason = "动态ATR止盈"
                        exit_price = min(raw_open, trail_level)
                # 3.4 定期调仓轮动退出 (持有 >= 5 天且跌出前三)
                elif rebalance_day:
                    holding_d = day_idx - pos["entry_day_idx"]
                    if holding_d >= self.min_holding_days:
                        top_syms = [c["symbol"] for c in (candidates_a if pos["sub_engine"] == "A" else candidates_b)[:3]]
                        if sym not in top_syms:
                            exit_reason = "调仓动量轮动退出"
                            exit_price = raw_open

                # 检查跌停不可卖出保护
                limit_down_p = round(prev_raw_close * 0.90, 2)
                if raw_open <= limit_down_p and raw_low <= limit_down_p and raw_high <= limit_down_p:
                    exit_reason = None  # 一字跌停无法卖出

                if exit_reason:
                    exec_price = exit_price * (1.0 - self.slippage_rate)
                    gross_amt = pos["shares"] * exec_price
                    fee, fee_dict = self._calc_fee("SELL", gross_amt)
                    net_proceeds = gross_amt - fee
                    cash += net_proceeds
                    pnl = net_proceeds - pos["shares"] * pos["cost_price"]
                    pnl_pct = (exec_price / pos["cost_price"] - 1.0) * 100.0

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

            # 4. 评估开仓意图与资金分配 (仅逢 rebalance_day 或空仓时执行，refill_vacancies = False)
            current_stk_val = sum(
                p["shares"] * self.stocks[s]["raw_opens"][self.stocks[s]["date_to_idx"][date]]
                for s, p in positions.items()
                if date in self.stocks[s]["date_to_idx"]
            )
            total_eq_open = cash + current_stk_val
            current_stk_wt = (current_stk_val / total_eq_open) if total_eq_open > 0 else 0.0
            remaining_allowed_wt = max(0.0, max_stock_cap - current_stk_wt)

            max_pos = 4 if effective_regime == "STRONG_RISK_ON" else 3
            vacancies = max(0, max_pos - len(positions))

            can_buy_today = (rebalance_day or len(positions) == 0) and (allowed_new_buys is True or allowed_new_buys == "V2_CHECK")
            if can_buy_today and vacancies > 0 and remaining_allowed_wt >= 0.05:
                # 确定意图标的
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

                    # 涨停不可买入保护
                    limit_up_p = round(prev_raw_close * 1.099, 2)
                    if raw_open >= limit_up_p:
                        continue

                    target_wt = min(0.35, remaining_allowed_wt / vacancies)
                    if base_regime == "RISK_OFF":
                        if variant_name in ("V1", "V2_10"):
                            target_wt = min(target_wt, 0.10)
                        elif variant_name in ("V2_20", "V3"):
                            target_wt = min(target_wt, 0.20)

                    # 行业上限 35% 约束
                    ind = sinfo["industry"]
                    allowed_sec_wt = max(0.0, 0.35 - (sector_vals[ind] / total_eq_open))
                    target_wt = min(target_wt, allowed_sec_wt)

                    if target_wt < 0.05:
                        continue

                    exec_price = raw_open * (1.0 + self.slippage_rate)
                    target_amt = total_eq_open * target_wt
                    shares = int(target_amt / exec_price / 100.0) * 100
                    if shares < 100:
                        continue

                    gross_amt = shares * exec_price
                    fee, fee_dict = self._calc_fee("BUY", gross_amt)
                    total_cost = gross_amt + fee

                    if total_cost > cash:
                        avail_shares = int((cash - self.min_commission) / (exec_price * (1 + self.commission_rate)) / 100.0) * 100
                        shares = max(0, avail_shares)
                        gross_amt = shares * exec_price
                        fee, fee_dict = self._calc_fee("BUY", gross_amt)
                        total_cost = gross_amt + fee

                    if shares >= 100 and total_cost <= cash:
                        cash -= total_cost
                        positions[sym] = {
                            "symbol": sym,
                            "name": sinfo["name"],
                            "industry": ind,
                            "shares": shares,
                            "cost_price": exec_price,
                            "peak_raw_price": exec_price,
                            "entry_date": date,
                            "entry_day_idx": day_idx,
                            "entry_regime": effective_regime,
                            "sub_engine": sub_eng,
                            "atr": cand["atr"]
                        }
                        sector_vals[ind] += gross_amt
                        vacancies -= 1
                        remaining_allowed_wt = max(0.0, remaining_allowed_wt - (gross_amt / total_eq_open))

                        orders_log.append({
                            "order_id": f"O_{len(orders_log)+1:04d}",
                            "date": date,
                            "symbol": sym,
                            "name": sinfo["name"],
                            "side": "BUY",
                            "shares": shares,
                            "price": round(exec_price, 2),
                            "amount": round(gross_amt, 2),
                            "status": "FILLED",
                            "reason": f"Product_AB_{effective_regime}_轮动建仓"
                        })

            # 5. 每日收盘计提闲置现金利息
            daily_interest = cash * (self.annual_cash_yield / 100.0) / 252.0
            cash += daily_interest

            # 6. 收盘市值结算
            close_stk_val = sum(
                p["shares"] * self.stocks[s]["raw_closes"][self.stocks[s]["date_to_idx"][date]]
                for s, p in positions.items()
                if date in self.stocks[s]["date_to_idx"]
            )
            total_equity = cash + close_stk_val
            curr_exposure = (close_stk_val / total_equity) if total_equity > 0 else 0.0

            daily_nav.append({
                "date": date,
                "equity": round(total_equity, 2),
                "cash": round(cash, 2),
                "stock_val": round(close_stk_val, 2),
                "exposure_pct": round(curr_exposure * 100.0, 2),
                "regime": effective_regime,
                "weight_a": w_a,
                "weight_b": w_b,
                "positions_count": len(positions),
                "daily_interest": round(daily_interest, 2)
            })

        # 结算期末指标
        initial_eq = self.initial_cash
        final_eq = daily_nav[-1]["equity"]
        tot_ret = (final_eq / initial_eq - 1.0) * 100.0
        n_days = len(daily_nav)
        cagr = ((final_eq / initial_eq) ** (252.0 / max(1, n_days)) - 1.0) * 100.0

        peak = initial_eq
        max_dd = 0.0
        for pt in daily_nav:
            eq_val = pt["equity"]
            if eq_val > peak:
                peak = eq_val
            dd = (peak - eq_val) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

        daily_rets = []
        for i in range(1, n_days):
            r = daily_nav[i]["equity"] / daily_nav[i-1]["equity"] - 1.0
            daily_rets.append(r)

        rf_daily = (self.annual_cash_yield / 100.0) / 252.0
        ex_rets = [r - rf_daily for r in daily_rets]
        mean_ex = sum(ex_rets) / len(ex_rets) if ex_rets else 0.0
        var_ex = sum((r - mean_ex)**2 for r in ex_rets) / max(1, len(ex_rets)-1)
        stdev = math.sqrt(var_ex)
        sharpe = (mean_ex / stdev * math.sqrt(252)) if stdev > 1e-8 else 0.0
        calmar = (cagr / max_dd) if max_dd > 0 else 0.0

        sells = closed_trades
        wins = [t for t in sells if t["realized_pnl"] > 0]
        losses = [t for t in sells if t["realized_pnl"] <= 0]
        win_rate = (len(wins) / len(sells) * 100.0) if sells else 0.0
        gross_win = sum(t["realized_pnl"] for t in wins)
        gross_loss = abs(sum(t["realized_pnl"] for t in losses))
        pl_ratio = (gross_win / gross_loss) if gross_loss > 0 else (gross_win if gross_win > 0 else 1.0)

        total_fees = sum(t["fees"] for t in closed_trades) + sum(o["amount"] * self.commission_rate for o in orders_log if o["side"] == "BUY")

        ro_trades = [t for t in closed_trades if "RISK_OFF" in t.get("entry_regime", "")]
        ro_pnl = sum(t["realized_pnl"] for t in ro_trades)

        return {
            "variant": variant_name,
            "total_return_pct": round(tot_ret, 2),
            "cagr_pct": round(cagr, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe_ratio": round(sharpe, 2),
            "calmar_ratio": round(calmar, 2),
            "final_equity": round(final_eq, 2),
            "total_trades": len(closed_trades),
            "win_rate_pct": round(win_rate, 2),
            "profit_loss_ratio": round(pl_ratio, 2),
            "total_fees": round(total_fees, 2),
            "risk_off_trades": len(ro_trades),
            "risk_off_pnl": round(ro_pnl, 2),
            "daily_nav": daily_nav,
            "orders": orders_log,
            "trades": closed_trades
        }

def run_unified_suite():
    print("=================================================================")
    print("[*] Starting Product AB Unified Single-Account Benchmark Suite")
    print("=================================================================")
    engine = UnifiedSingleAccountEngine(start_date="2024-01-02", end_date="2026-11-06")

    variants = ["V0", "V1", "V2_10", "V2_20", "V3"]
    all_results = {}

    out_dir = Path("/home/openarmx/test/research_audit/unified_results")
    out_dir.mkdir(parents=True, exist_ok=True)

    for var in variants:
        t0 = time.time()
        res = engine.run_simulation(variant_name=var)
        all_results[var] = res
        print(f">> {var:<6} | Return: {res['total_return_pct']:>6.2f}% | CAGR: {res['cagr_pct']:>5.2f}% | MDD: {res['max_drawdown_pct']:>5.2f}% | Sharpe: {res['sharpe_ratio']:>4.2f} | Trades: {res['total_trades']:>3} | WinRate: {res['win_rate_pct']:>5.1f}% | RO PnL: ¥{res['risk_off_pnl']:>8.1f} ({time.time()-t0:.1f}s)")

        # Export V0 daily NAV, orders, and trades to CSV
        if var == "V0":
            # Export Daily NAV CSV
            nav_csv = out_dir / "v0_daily_nav.csv"
            with open(nav_csv, "w", encoding="utf-8") as fp:
                fp.write("date,equity,cash,stock_val,exposure_pct,regime,weight_a,weight_b,positions_count,daily_interest\n")
                for pt in res["daily_nav"]:
                    fp.write(f"{pt['date']},{pt['equity']},{pt['cash']},{pt['stock_val']},{pt['exposure_pct']},{pt['regime']},{pt['weight_a']},{pt['weight_b']},{pt['positions_count']},{pt['daily_interest']}\n")

            # Export Orders CSV
            orders_csv = out_dir / "v0_orders.csv"
            with open(orders_csv, "w", encoding="utf-8") as fp:
                fp.write("order_id,date,symbol,name,side,shares,price,amount,status,reason\n")
                for o in res["orders"]:
                    fp.write(f"{o['order_id']},{o['date']},{o['symbol']},{o['name']},{o['side']},{o['shares']},{o['price']},{o['amount']},{o['status']},{o['reason']}\n")

            # Export Trades CSV
            trades_csv = out_dir / "v0_trades.csv"
            with open(trades_csv, "w", encoding="utf-8") as fp:
                fp.write("trade_id,date,symbol,name,side,shares,price,amount,fees,slippage,realized_pnl,pnl_pct,holding_days,reason\n")
                for t in res["trades"]:
                    fp.write(f"{t['trade_id']},{t['date']},{t['symbol']},{t['name']},{t['side']},{t['shares']},{t['price']},{t['amount']},{t['fees']},{t['slippage']},{t['realized_pnl']},{t['pnl_pct']},{t['holding_days']},{t['reason']}\n")

    # Export comparison summary JSON and CSV
    summary_export = {}
    csv_lines = ["variant,total_return_pct,cagr_pct,max_drawdown_pct,sharpe_ratio,calmar_ratio,final_equity,total_trades,win_rate_pct,profit_loss_ratio,total_fees,risk_off_trades,risk_off_pnl\n"]
    for var, res in all_results.items():
        summary_export[var] = {
            "variant": var,
            "total_return_pct": res["total_return_pct"],
            "cagr_pct": res["cagr_pct"],
            "max_drawdown_pct": res["max_drawdown_pct"],
            "sharpe_ratio": res["sharpe_ratio"],
            "calmar_ratio": res["calmar_ratio"],
            "final_equity": res["final_equity"],
            "total_trades": res["total_trades"],
            "win_rate_pct": res["win_rate_pct"],
            "profit_loss_ratio": res["profit_loss_ratio"],
            "total_fees": res["total_fees"],
            "risk_off_trades": res["risk_off_trades"],
            "risk_off_pnl": res["risk_off_pnl"]
        }
        csv_lines.append(f"{var},{res['total_return_pct']},{res['cagr_pct']},{res['max_drawdown_pct']},{res['sharpe_ratio']},{res['calmar_ratio']},{res['final_equity']},{res['total_trades']},{res['win_rate_pct']},{res['profit_loss_ratio']},{res['total_fees']},{res['risk_off_trades']},{res['risk_off_pnl']}\n")

    with open(out_dir / "v0_to_v3_comparison.json", "w", encoding="utf-8") as fp:
        json.dump(summary_export, fp, ensure_ascii=False, indent=2)

    with open(out_dir / "v0_to_v3_comparison.csv", "w", encoding="utf-8") as fp:
        fp.writelines(csv_lines)

    # Compute data sha256 checksums
    checksums = {}
    for p in [BENCH_300_PATH, BENCH_500_PATH, BENCH_1000_PATH, IND_MAP_PATH]:
        if os.path.exists(p):
            with open(p, "rb") as f:
                checksums[os.path.basename(p)] = hashlib.sha256(f.read()).hexdigest()
    with open(out_dir / "data_checksums.json", "w", encoding="utf-8") as fp:
        json.dump(checksums, fp, indent=2)

    print(f"\n[*] All outputs successfully saved to {out_dir}")

if __name__ == "__main__":
    run_unified_suite()
