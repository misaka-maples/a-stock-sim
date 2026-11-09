"""Dated daily momentum research: next-open signals and optional OHLC stops.

Raw prices account for cash and holdings; adjusted bars only supply indicators.
The daily-open/whole-day-volume model is an approximation to the live arrival
book. Cash dividends, splits and delisting recoveries need a separate event feed.
"""
from dataclasses import asdict, fields
import datetime as dt
import math
import re
import statistics
import time
import os
import glob
import json
from pathlib import Path

from history_data import HistoricalDataFeed, previous_trading_day, normalize_symbol_for_tencent
from trade_calendar import is_trading_day
from strategy_rules import (
    MomentumConfig, evaluate_momentum, holding_signal, exit_reason,
    target_weight, allowed_by_sector, sector_of, entry_guard_reason,
)
from execution import match_daily


MOMENTUM_STRATEGIES = {"Momentum_Rotation", "Momentum_Rotation_Pro"}
_DYNAMIC_STOCKS_CACHE = None


def _main_board(symbol):
    value = str(symbol).lower()
    if not re.fullmatch(r"(?:sh|sz)\d{6}", value):
        return False
    return (value.startswith("sh") and value[2:].startswith(("600", "601", "603", "605"))
            or value.startswith("sz") and value[2:].startswith(("000", "001", "002", "003")))


def eligible_on(symbol, date, intervals):
    """Optional historical eligibility; endpoints are inclusive ISO dates."""
    if intervals is None:
        return True
    records = intervals.get(symbol, [])
    if isinstance(records, dict):
        records = [records]
    return any((row.get("start") or row.get("eligible_from") or "0000-01-01") <= date
               <= (row.get("end") or row.get("eligible_to") or "9999-12-31")
               for row in records)


class MomentumBacktestEngine:
    def __init__(self, start_date="2024-01-01", end_date=None,
                 initial_cash=100000.0, strategy_name="Momentum_Rotation_Pro",
                 symbols=None, pool_type="core_active", benchmark_code="000300",
                 config=None, feed=None, industry_map=None, universe_intervals=None,
                 commission_rate=.00025, min_commission=5.0,
                 stamp_tax_rate=.0005, transfer_fee_rate=.00001,
                 slippage_rate=.001, strict_t1=True, warmup_calendar_days=400,
                 streaming=False, spill_results=False, work_dir=None, progress=None, **kwargs):
        if strategy_name not in MOMENTUM_STRATEGIES:
            raise ValueError("Unknown momentum strategy")
        self.streaming, self.spill_results = streaming, spill_results
        self.work_dir, self.progress = work_dir, progress
        self._progress_at = 0
        self._stream_store = None
        self.feed = feed if feed is not None else HistoricalDataFeed()
        if end_date is None:
            from history_data import last_completed_trading_day
            end_date = last_completed_trading_day()
        if start_date > end_date:
            raise ValueError("start_date must precede end_date")
        if initial_cash <= 0 or not math.isfinite(initial_cash):
            raise ValueError("initial_cash must be positive and finite")
        self.start_date, self.end_date = start_date, end_date
        self.initial_cash, self.strategy_name = float(initial_cash), strategy_name
        self.symbols = list(dict.fromkeys(normalize_symbol_for_tencent(s)
                                         for s in (symbols or self.feed.get_preset_symbols(pool_type))))
        if any(not re.fullmatch(r"(?:sh|sz|bj)\d{6}", s) for s in self.symbols):
            raise ValueError("symbols must be exchange prefixes plus six digits or six-digit codes")
        self.pool_type, self.benchmark_code = pool_type, benchmark_code
        if isinstance(config, MomentumConfig):
            values = asdict(config)
        else:
            values = dict(config or {})
            if strategy_name == "Momentum_Rotation":
                values.setdefault("use_ma120", False)
                values.setdefault("use_high_filter", False)
        config_keys = {f.name for f in fields(MomentumConfig)}
        legacy_keys = {"min_change_pct", "max_change_pct", "min_volume_ratio",
                       "min_turnover_rate", "take_profit_trigger_pct",
                       "trailing_callback_pct", "max_take_profit_pct", "max_holding_days"}
        unknown = set(kwargs) - config_keys - legacy_keys
        if unknown:
            raise TypeError("Unknown momentum arguments: " + ", ".join(sorted(unknown)))
        values.update({k: v for k, v in kwargs.items() if k in config_keys})
        self.config = MomentumConfig.from_dict(values)
        self.ignored_legacy_parameters = sorted(set(kwargs) & legacy_keys)
        self.industry_map = {normalize_symbol_for_tencent(s): industry
                             for s, industry in (industry_map or {}).items()}
        if not self.industry_map:
            cache_dir = getattr(self.feed, "cache_dir", None)
            if cache_dir is not None:
                from pathlib import Path
                ind_path = Path(cache_dir) / "industry_map_eastmoney.json"
                if ind_path.exists():
                    try:
                        raw_ind = json.loads(ind_path.read_text(encoding="utf-8"))
                        self.industry_map = {normalize_symbol_for_tencent(s): ind
                                             for s, ind in raw_ind.get("industries", {}).items()}
                    except Exception:
                        pass
        self.universe_intervals = ({normalize_symbol_for_tencent(s): intervals for s, intervals in universe_intervals.items()}
                                   if universe_intervals is not None else None)
        self.commission_rate, self.min_commission = commission_rate, min_commission
        self.stamp_tax_rate, self.transfer_fee_rate = stamp_tax_rate, transfer_fee_rate
        self.slippage_rate, self.strict_t1 = slippage_rate, strict_t1
        costs = (commission_rate, min_commission, stamp_tax_rate, transfer_fee_rate, slippage_rate)
        if not all(math.isfinite(v) and v >= 0 for v in costs) or slippage_rate >= 1:
            raise ValueError("Fees and slippage must be nonnegative finite values; slippage must be below 1")
        self.warmup_calendar_days = warmup_calendar_days

    def _fees(self, side, amount):
        return (max(self.min_commission, amount * self.commission_rate)
                + amount * self.transfer_fee_rate
                + (amount * self.stamp_tax_rate if side == "SELL" else 0))

    @staticmethod
    def _raw_valid(bar):
        try:
            return all(math.isfinite(float(bar[k])) and float(bar[k]) > 0
                       for k in ("raw_open", "raw_close", "raw_high", "raw_low"))
        except (KeyError, TypeError, ValueError):
            return False

    def notify_progress(self, phase, done, total, throttle=False):
        if self.progress and (not throttle or time.monotonic()-self._progress_at >= 1):
            self.progress(phase, done, total)
            self._progress_at = time.monotonic()

    def close(self):
        if self._stream_store is not None:
            self._stream_store.close()
            self._stream_store = None

    def run(self):
        try:
            result = self._run()
        except BaseException:
            self.close()
            raise
        if not self.spill_results:
            self.close()
        return result

    def _run(self):
        warmup_start = (dt.date.fromisoformat(self.start_date)
                        - dt.timedelta(days=self.warmup_calendar_days)).isoformat()
        benchmark = sorted(self.feed.fetch_benchmark_kline(
            index_code=self.benchmark_code, start_date=warmup_start,
            end_date=self.end_date), key=lambda b: b["date"])
        benchmark = [b for b in benchmark if warmup_start <= b["date"] <= self.end_date]
        if len({b["date"] for b in benchmark}) != len(benchmark):
            raise ValueError("Duplicate benchmark dates")
        dates = [b["date"] for b in benchmark if self.start_date <= b["date"] <= self.end_date]
        if not dates:
            raise ValueError("No benchmark bars in requested period")
        if self.streaming:
            from streaming_history import StreamingStocks
            signal_dates = {previous_trading_day(dt.date.fromisoformat(d)) for d in dates}
            stocks = StreamingStocks(self, warmup_start, dates, signal_dates)
            self._stream_store = stocks
        elif self.config.dynamic_universe_enabled and getattr(self.feed, "cache_dir", None) is not None:
            global _DYNAMIC_STOCKS_CACHE
            if _DYNAMIC_STOCKS_CACHE is None:
                qfq_files = glob.glob(str(Path(self.feed.cache_dir) / "*_qfq.json"))
                dyn_symbols = []
                for p in qfq_files:
                    sym = os.path.basename(p).replace("_qfq.json", "")
                    if _main_board(sym) and sym in self.industry_map:
                        dyn_symbols.append(sym)
                dyn_symbols.sort()
                loaded = {}
                for sym in dyn_symbols:
                    p = Path(self.feed.cache_dir) / f"{sym}_qfq.json"
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            cached = json.load(f)
                    except Exception:
                        cached = None
                    if cached and cached.get("bars"):
                        bars = sorted(cached["bars"], key=lambda b: b["date"])
                        if len(bars) >= 125 and self._raw_valid(bars[-1]):
                            loaded[sym] = dict(cached, bars=bars)
                _DYNAMIC_STOCKS_CACHE = loaded
            stocks = {}
            for sym, s_data in _DYNAMIC_STOCKS_CACHE.items():
                bars = [b for b in s_data["bars"] if b.get("date", "") <= self.end_date]
                if len(bars) >= 125:
                    stocks[sym] = dict(s_data, bars=bars, by_date={b["date"]: b for b in bars},
                                       date_to_idx={b["date"]: i for i, b in enumerate(bars)})
            self.symbols = list(stocks.keys())
        else:
            cached_stocks = {}
            if getattr(self.feed, "cache_dir", None) is not None:
                for sym in self.symbols:
                    cached = self.feed._read_cache(sym, warmup_start, self.end_date, "qfq")
                    if cached and cached.get("bars"):
                        bars = sorted([b for b in cached["bars"] if b.get("date", "") <= self.end_date],
                                      key=lambda b: b["date"])
                        if len(bars) >= 125 and self._raw_valid(bars[-1]):
                            cached_stocks[sym] = dict(cached, bars=bars, by_date={b["date"]: b for b in bars},
                                                      date_to_idx={b["date"]: i for i, b in enumerate(bars)})
            if len(cached_stocks) == len(self.symbols):
                stocks = cached_stocks
            else:
                stocks = self.feed.fetch_universe_klines(
                    self.symbols, start_date=warmup_start, end_date=self.end_date)
        warnings = ["daily_open_approximation: signals use previous close; fills use next raw open",
                    "capacity_approximation: daily total volume limits fills, not opening-auction depth",
                    "corporate_actions_not_accounted: cash dividends, splits and delisting recoveries require an event feed"]
        if self.config.stop_loss_mode == "intraday":
            warnings.append("intraday_stop_ohlc_approximation: existing holdings stop at raw open on gaps or threshold minus slippage on low crossing; daily OHLC cannot prove tick order, depth or exact fills; T+1 and unfilled stop intents persist")
        if self.config.refill_vacancies:
            warnings.append("vacancy_refill_open_only: real vacant slots use previous-close candidates at each open, after completed opening exits; partial or pending exits still occupy slots; intraday exits can only be replaced at a later open, unlike live tick execution; existing holdings are never topped up")
        if any(not _main_board(s) for s in self.symbols):
            warnings.append("non_main_board_excluded: ranking and execution both use Shanghai/Shenzhen main-board stocks only")
        warnings.append("historical_risk_status_unverified: dated ST/price-limit metadata takes precedence; current names are only a conservative fallback")
        if self.universe_intervals is None:
            warnings.append("fixed_pool_research_only: historical universe eligibility/survivorship unverified")
        if self.streaming:
            missing_industry = stocks.missing_industry
            data_coverage = stocks.coverage
            if missing_industry:
                warnings.append("missing_industry_mapping: unknown names share one capped industry bucket")
            if self.ignored_legacy_parameters:
                warnings.append("ignored_legacy_parameters: " + ", ".join(self.ignored_legacy_parameters))
            warnings.extend(stocks.warnings)
        elif self.config.dynamic_universe_enabled:
            missing_industry = 0
            data_coverage = {}
        else:
            missing_industry = sum(not self.industry_map.get(s) and not any(
                b.get("industry") or b.get("sector") for b in stocks.get(s, {}).get("bars", [])
                if b.get("date", "") <= self.end_date) for s in self.symbols)
            if missing_industry:
                warnings.append("missing_industry_mapping: unknown names share one capped industry bucket")
            if self.ignored_legacy_parameters:
                warnings.append("ignored_legacy_parameters: " + ", ".join(self.ignored_legacy_parameters))
            data_coverage = {}
            for symbol in self.symbols:
                info = stocks.get(symbol) or {"bars": []}
                bars = sorted([b for b in info.get("bars", []) if b.get("date", "") <= self.end_date],
                              key=lambda b: b["date"])
                if len({b["date"] for b in bars}) != len(bars):
                    raise ValueError("Duplicate stock dates: " + symbol)
                info = dict(info, bars=bars, by_date={b["date"]: b for b in bars},
                            date_to_idx={b["date"]: i for i, b in enumerate(bars)})
                stocks[symbol] = info
                raw_missing = [b["date"] for b in bars if not self._raw_valid(b)]
                if raw_missing:
                    raise ValueError("raw_prices_missing: " + symbol + "; fail closed")
                in_period = [b["date"] for b in bars if self.start_date <= b["date"] <= self.end_date]
                pre_count = sum(b["date"] < dates[0] for b in bars)
                data_coverage[symbol] = dict(start=bars[0]["date"] if bars else None,
                                             end=bars[-1]["date"] if bars else None,
                                             count=len(bars), pre_start_bars=pre_count,
                                             period_count=len(in_period), metadata=info.get("metadata", {}))
                if not bars:
                    warnings.append("missing_stock_data: " + symbol)
                if pre_count < 125:
                    warnings.append("insufficient_warmup: " + symbol)
                if any(d not in info["by_date"] for d in dates):
                    warnings.append("missing_bars_or_suspension: " + symbol)
        bench_by_date = {b["date"]: b for b in benchmark}
        prior_date = {date: previous_trading_day(dt.date.fromisoformat(date)) for date in dates}
        missing_benchmark = []
        cursor = dt.date.fromisoformat(self.start_date)
        while cursor.isoformat() <= self.end_date:
            if is_trading_day(cursor) and cursor.isoformat() not in bench_by_date:
                missing_benchmark.append(cursor.isoformat())
            cursor += dt.timedelta(days=1)
        data_coverage["_benchmark"] = dict(start=benchmark[0]["date"], end=benchmark[-1]["date"],
                                            count=len(benchmark), missing_trading_dates=missing_benchmark,
                                            metadata=getattr(self.feed, "metadata", {}).get("idx_"+self.benchmark_code, {}))
        if missing_benchmark or any(prior_date[d] not in bench_by_date for d in dates):
            warnings.append("benchmark_coverage_gap: previous signal dates use the exchange calendar, never the previous available row")
        first_previous = prior_date[dates[0]]
        bench_initial = float(bench_by_date[first_previous]["close"] if first_previous in bench_by_date
                              else bench_by_date[dates[0]].get("open", bench_by_date[dates[0]]["close"]))
        cash, positions, cooldown, pending_exits = self.initial_cash, {}, {}, {}
        if self.spill_results:
            if not self.streaming:
                raise ValueError("spill_results requires streaming")
            from streaming_history import DiskList
            fills, trades, rejections, curve = [DiskList(stocks.db, name) for name in ('fills','trades','rejections','curve')]
        else:
            fills, trades, rejections, curve = [], [], [], []
        peak, total_fees = self.initial_cash, 0.0
        observed_factors = {}

        def marks(at_date, opening=False):
            values = {}
            for symbol, pos in positions.items():
                bar = stocks[symbol]["by_date"].get(at_date)
                price = float(bar["raw_open" if opening else "raw_close"]) if bar else pos["last_price"]
                values[symbol] = price * pos["shares"]
            return values

        def execution_bar(symbol, bar):
            name = bar.get("name") or stocks[symbol].get("name", symbol)
            # A dated False means the security was not ST on that date; a
            # current risk-warning name cannot override explicit historical data.
            if bar.get("is_st") is False and not bar.get("name"):
                name = symbol
            return dict(bar, symbol=symbol, name=name)

        def industries_on(date):
            mapping = dict(self.industry_map)
            for symbol in self.symbols:
                try:
                    s_info = stocks.get(symbol) if isinstance(stocks, dict) else (stocks.info.get(symbol) if hasattr(stocks, "info") else stocks[symbol])
                except Exception:
                    s_info = None
                if s_info and "by_date" in s_info:
                    bar = s_info["by_date"].get(date, {})
                    if "industry" in bar or "sector" in bar:
                        mapping[symbol] = bar.get("industry") or bar.get("sector")
            return mapping

        def select_dynamic_universe(as_of, dated_inds):
            top_n_industries = getattr(self.config, "dynamic_top_industries", 4)
            top_n_stocks_per_ind = getattr(self.config, "dynamic_stocks_per_industry", 12)
            min_amount = getattr(self.config, "dynamic_min_amount", 30000000.0)

            qualified_by_ind = {}
            for sym, s_data in stocks.items():
                idx = s_data.get("date_to_idx", {}).get(as_of)
                if idx is None or idx < 125:
                    continue
                bars = s_data["bars"]
                b = bars[idx]
                name = str(b.get("name") or s_data.get("name", "")).upper()
                if b.get("is_st") or "ST" in name or "退" in name:
                    continue
                recent_bars = bars[idx-19:idx+1]
                mean_amt = sum(float(x.get("amount", 0) or 0) for x in recent_bars) / 20.0
                if mean_amt < min_amount:
                    continue
                c = float(b["close"])
                c_20 = float(bars[idx-20]["close"])
                c_60 = float(bars[idx-60]["close"])
                if c <= 0 or c_20 <= 0 or c_60 <= 0:
                    continue
                ret20 = (c / c_20 - 1.0) * 100.0
                ret60 = (c / c_60 - 1.0) * 100.0
                ma20 = sum(float(x["close"]) for x in recent_bars) / 20.0

                score = ret20 * 2.0 + ret60 * 1.0
                ind = dated_inds.get(sym) or s_data.get("industry") or self.industry_map.get(sym, "其他")
                qualified_by_ind.setdefault(ind, []).append({
                    "symbol": sym, "name": name, "ret20": ret20, "ret60": ret60,
                    "score": score, "above_ma20": c > ma20, "amount": mean_amt
                })

            ind_stats = []
            for ind, s_list in qualified_by_ind.items():
                if len(s_list) < 3:
                    continue
                avg_ret20 = sum(s["ret20"] for s in s_list) / len(s_list)
                breadth_ma20 = sum(1 for s in s_list if s["above_ma20"]) / len(s_list)
                if avg_ret20 > 0 and breadth_ma20 >= 0.40:
                    ind_score = avg_ret20 * (1.0 + breadth_ma20)
                    ind_stats.append((ind, ind_score, s_list))

            ind_stats.sort(key=lambda x: x[1], reverse=True)
            selected_industries = ind_stats[:top_n_industries]

            chosen_symbols = []
            for ind, i_score, s_list in selected_industries:
                s_list.sort(key=lambda x: x["score"], reverse=True)
                chosen = s_list[:top_n_stocks_per_ind]
                chosen_symbols.extend([s["symbol"] for s in chosen])

            return chosen_symbols

        active_candidates = None
        consecutive_strong_days = 0
        for index, date in enumerate(dates):
            self.notify_progress('replay', index, len(dates))
            day_notional = 0.0
            as_of = prior_date[date]
            if self.streaming:
                stocks.prepare_day(date, as_of)
            dated_industries = industries_on(date)
            rebalance = index % self.config.rebalance_every == 0

            cand_symbols = self.symbols
            if as_of and self.config.dynamic_universe_enabled:
                if rebalance or active_candidates is None:
                    active_candidates = select_dynamic_universe(as_of, dated_industries)
                cand_symbols = active_candidates

            signals = {}
            if as_of:
                for symbol in cand_symbols:
                    info = stocks.get(symbol)
                    if not info:
                        continue
                    signal_bar = info["by_date"].get(as_of, {})
                    name = str(signal_bar.get("name") or info.get("name", "")).upper()
                    risk_warning = (signal_bar["is_st"] if signal_bar.get("is_st") is not None
                                    else "ST" in name or "退" in name or name.startswith("PT"))
                    if (_main_board(symbol) and not risk_warning
                            and eligible_on(symbol, as_of, self.universe_intervals)):
                        signal = stocks.signal(symbol, as_of) if self.streaming else evaluate_momentum(stocks[symbol]["bars"], as_of, self.config)
                        if signal:
                            signals[symbol] = signal
            ranking = sorted(signals, key=lambda s: (-signals[s]["score"], s))
            ranks = {symbol: i + 1 for i, symbol in enumerate(ranking)}
            market_ok = True
            regime = "NORMAL"
            effective_max_positions = self.config.max_positions
            effective_risk = self.config.risk_per_trade
            effective_max_stock_weight = self.config.max_stock_weight

            history = [b for b in benchmark if as_of and b["date"] <= as_of] if as_of else []
            if self.config.regime_sizing_enabled:
                if len(history) >= 120 and as_of:
                    hs_close = float(history[-1]["close"])
                    ma120 = sum(float(b["close"]) for b in history[-120:]) / 120.0
                    ma20 = sum(float(b["close"]) for b in history[-20:]) / 20.0
                    ma60 = sum(float(b["close"]) for b in history[-60:]) / 60.0
                    if hs_close <= ma120:
                        regime = "RISK_OFF"
                        market_ok = False
                        effective_max_positions = 0
                        effective_risk = 0.0
                    elif hs_close <= ma20:
                        regime = "NEUTRAL"
                        market_ok = True
                        effective_max_positions = self.config.regime_neutral_positions
                        effective_risk = self.config.regime_neutral_risk
                        effective_max_stock_weight = self.config.regime_neutral_weight
                    elif ma20 <= ma60:
                        regime = "RISK_ON"
                        market_ok = True
                        effective_max_positions = self.config.regime_riskon_positions
                        effective_risk = self.config.regime_riskon_risk
                        effective_max_stock_weight = self.config.regime_riskon_weight
                    else:
                        cand_breadth = 1.0
                        if cand_symbols:
                            above_120 = 0
                            for s in cand_symbols:
                                s_info = stocks.get(s)
                                if not s_info:
                                    continue
                                s_idx = s_info.get("date_to_idx", {}).get(as_of)
                                if s_idx is not None and s_idx >= 120:
                                    s_ma120 = sum(float(x["close"]) for x in s_info["bars"][s_idx-119:s_idx+1]) / 120.0
                                    s_c = float(s_info["bars"][s_idx]["close"])
                                    if s_c > s_ma120:
                                        above_120 += 1
                            cand_breadth = above_120 / max(1, len(cand_symbols))

                        if (getattr(self.config, "regime_breadth_confirmation", False)
                                and cand_breadth < float(getattr(self.config, "regime_strong_breadth_min", 0.60))):
                            regime = "RISK_ON"
                            market_ok = True
                            effective_max_positions = self.config.regime_riskon_positions
                            effective_risk = self.config.regime_riskon_risk
                            effective_max_stock_weight = self.config.regime_riskon_weight
                        else:
                            regime = "STRONG_RISK_ON"
                            market_ok = True
                            effective_max_positions = self.config.regime_strong_positions
                            effective_risk = self.config.regime_strong_risk
                            effective_max_stock_weight = self.config.regime_strong_weight
                else:
                    market_ok = False
            elif self.config.market_filter:
                market_ok = (len(history) >= 120 and history[-1]["date"] == as_of and float(history[-1]["close"])
                             > sum(float(b["close"]) for b in history[-120:]) / 120)

            if regime == "STRONG_RISK_ON":
                consecutive_strong_days += 1
            else:
                consecutive_strong_days = 0

            if getattr(self.config, "regime_ramp_up_enabled", False) and regime == "STRONG_RISK_ON":
                ramp_days = max(1, int(getattr(self.config, "regime_ramp_up_days", 3)))
                min_mult = float(getattr(self.config, "regime_ramp_min_mult", 0.50))
                if consecutive_strong_days <= 1:
                    ramp_factor = min_mult
                    effective_max_positions = min(effective_max_positions, 2)
                elif consecutive_strong_days < ramp_days:
                    step = (1.0 - min_mult) / max(1, ramp_days - 1)
                    ramp_factor = min_mult + step * (consecutive_strong_days - 1)
                    effective_max_positions = min(effective_max_positions, max(2, self.config.regime_strong_positions - 1))
                else:
                    ramp_factor = 1.0
                effective_risk = effective_risk * ramp_factor
                effective_max_stock_weight = effective_max_stock_weight * ramp_factor

            # Orders only inspect completed bars. Failed/partial exits persist.
            if as_of:
                for symbol, pos in positions.items():
                    signal = stocks.signal(symbol, as_of, holding=True) if self.streaming else holding_signal(stocks[symbol]["bars"], as_of)
                    if signal is None:
                        continue  # Missing evidence cannot authorize a new rank exit.
                    reason = exit_reason(signal["raw_signal_price"], pos["cost_price"],
                                         signal, self.config,
                                         current_adjusted_price=signal["signal_price"])
                    if (reason is None and rebalance and index - pos["buy_index"] - 1 >= self.config.min_holding_days
                            and ranks.get(symbol, math.inf) > effective_max_positions + self.config.rank_buffer):
                        reason = "排名缓冲外轮动信号"
                    if reason:
                        pending_exits.setdefault(symbol, dict(reason=reason, signal_date=as_of))
            consumed = {}
            def execute_exits(intraday=False):
                nonlocal cash, day_notional, total_fees
                for symbol, order in list(pending_exits.items()):
                    if intraday and order.get("trigger_date") != date:
                        continue  # Prior intents already attempted at this open.
                    pos = positions.get(symbol)
                    if not pos:
                        pending_exits.pop(symbol, None)
                        continue
                    if self.strict_t1 and pos["buy_date"] >= date:
                        continue
                    bar = stocks[symbol]["by_date"].get(date)
                    if not bar:
                        rejections.append(dict(date=date, symbol=symbol, side="SELL", reason="missing_daily_bar"))
                        continue
                    bar = execution_bar(symbol, bar)
                    fill_basis = "raw_open"
                    if intraday and order.get("trigger_date") == date:
                        # Only a newly observed low crossing may use a same-day
                        # stop threshold; old unfilled intents execute at next open.
                        stop = order["stop_price"]
                        reference = min(float(bar["raw_open"]), stop)
                        fill_basis = "raw_open_gap" if float(bar["raw_open"]) <= stop else "ohlc_stop_threshold"
                        bar = dict(bar, raw_open=reference)
                    previous = stocks[symbol]["by_date"].get(as_of, {})
                    pre_close = bar.get("raw_pre_close") or previous.get("raw_close") or pos["last_price"]
                    result = match_daily("SELL", pos["shares"], bar, pre_close,
                                         participation_rate=self.config.participation_rate,
                                         slippage_rate=self.slippage_rate, consumed_shares=consumed.get((symbol, "SELL"), 0))
                    if not result.get("success") or not result.get("shares"):
                        rejections.append(dict(date=date, symbol=symbol, side="SELL", reason=result.get("reason")))
                        continue
                    shares, price = int(result["shares"]), float(result["price"])
                    consumed[(symbol, "SELL")] = consumed.get((symbol, "SELL"), 0) + shares
                    amount, fee = shares * price, self._fees("SELL", shares * price)
                    allocated_buy_fees = pos["remaining_buy_fees"] * shares / pos["shares"]
                    buy_amount = shares * pos["buy_price"]
                    pnl = amount - fee - buy_amount - allocated_buy_fees
                    cash += amount - fee
                    day_notional += amount
                    total_fees += fee
                    trades.append(dict(symbol=symbol, name=pos["name"], buy_date=pos["buy_date"],
                                       buy_price=pos["buy_price"], sell_date=date, sell_price=price,
                                       shares=shares, buy_amount=round(buy_amount, 2), sell_amount=round(amount, 2),
                                       net_pnl=round(pnl, 2), pnl_pct=round(pnl / buy_amount * 100, 2),
                                       fees=round(fee + allocated_buy_fees, 2), holding_days=index-pos["buy_index"],
                                       reason=order["reason"], signal_date=order["signal_date"], status=result.get("status"),
                                       execution_phase="intraday_stop" if intraday else "open", fill_basis=fill_basis))
                    fills.append(dict(date=date, symbol=symbol, side="SELL", price=price, shares=shares,
                                      fees=fee, signal_date=order["signal_date"], status=result.get("status"),
                                      execution_phase="intraday_stop" if intraday else "open", fill_basis=fill_basis))
                    pos["remaining_buy_fees"] -= allocated_buy_fees
                    pos["shares"] -= shares
                    if pos["shares"] == 0:
                        positions.pop(symbol)
                        pending_exits.pop(symbol)
                        cooldown[symbol] = index + self.config.cooldown_days

            execute_exits()
            # Scheduled rotation controls rank exits, independently of filling
            # genuinely empty slots. Existing and partially exiting positions
            # still consume slots; intraday proceeds arrive after these buys.
            if (rebalance or self.config.refill_vacancies) and market_ok:
                for symbol in ranking:
                    if len(positions) >= effective_max_positions:
                        break
                    if (symbol in positions or index <= cooldown.get(symbol, -1)
                            or not eligible_on(symbol, date, self.universe_intervals)):
                        continue
                    bar = stocks[symbol]["by_date"].get(date)
                    if not bar:
                        rejections.append(dict(date=date, symbol=symbol, side="BUY", reason="missing_daily_bar"))
                        continue
                    previous = stocks[symbol]["by_date"].get(as_of, {})
                    pre_close = bar.get("raw_pre_close") or previous.get("raw_close")
                    # Only information available at the open. Today's close,
                    # high and low cannot decide whether to initiate a buy.
                    reason = entry_guard_reason(signals[symbol], self.config, pre_close,
                                                bar.get("raw_open"), bar.get("raw_open"),
                                                bar.get("limit_down"))
                    if reason:
                        rejections.append(dict(date=date, symbol=symbol, side="BUY", reason=reason))
                        continue
                    open_values = marks(date, opening=True)
                    equity = cash + sum(open_values.values())
                    if self.config.regime_sizing_enabled:
                        cfg_regime = MomentumConfig(**{**asdict(self.config),
                            "max_positions": effective_max_positions,
                            "risk_per_trade": effective_risk,
                            "max_stock_weight": effective_max_stock_weight})
                        weight = target_weight(cfg_regime, signals[symbol], rank=ranks.get(symbol, 1))
                    else:
                        weight = target_weight(self.config, signals[symbol], rank=ranks.get(symbol, 1))
                    weights = {s: v/equity for s, v in open_values.items()}
                    if not allowed_by_sector(symbol, weight, weights, dated_industries, self.config.max_sector_weight):
                        rejections.append(dict(date=date, symbol=symbol, side="BUY", reason="industry_cap"))
                        continue
                    bar = execution_bar(symbol, bar)
                    estimate = float(bar["raw_open"]) * (1 + self.slippage_rate)
                    budget = min(equity * weight, max(0, cash - self.min_commission))
                    requested = int(budget / (estimate * (1 + self.commission_rate + self.transfer_fee_rate)) / 100) * 100
                    if requested < 100:
                        continue
                    previous = stocks[symbol]["by_date"].get(as_of, {})
                    pre_close = bar.get("raw_pre_close") or previous.get("raw_close")
                    result = match_daily("BUY", requested, bar, pre_close,
                                         participation_rate=self.config.participation_rate,
                                         slippage_rate=self.slippage_rate,
                                         consumed_shares=consumed.get((symbol, "BUY"), 0))
                    if not result.get("success") or not result.get("shares"):
                        rejections.append(dict(date=date, symbol=symbol, side="BUY", reason=result.get("reason")))
                        continue
                    reason = entry_guard_reason(signals[symbol], self.config, pre_close,
                                                bar.get("raw_open"), result["price"],
                                                bar.get("limit_down"))
                    if reason:
                        rejections.append(dict(date=date, symbol=symbol, side="BUY", reason=reason))
                        continue
                    # The pre-fill estimate is only a request size. Apply the
                    # same post-fill mark/fee budgets as the paper broker and
                    # rematch the permitted lot count before committing cash.
                    sector = sector_of(symbol, dated_industries)
                    sector_value = sum(value for held, value in open_values.items()
                                       if sector_of(held, dated_industries) == sector)
                    low, high, permitted = 1, int(result["shares"]) // 100, None
                    while low <= high:
                        lots = (low + high) // 2
                        preview = match_daily("BUY", lots * 100, bar, pre_close,
                                              participation_rate=self.config.participation_rate,
                                              slippage_rate=self.slippage_rate,
                                              consumed_shares=consumed.get((symbol, "BUY"), 0))
                        if preview.get("success") and preview.get("shares"):
                            quantity, price = int(preview["shares"]), float(preview["price"])
                            amount = quantity * price
                            fee = self._fees("BUY", amount)
                            marked = quantity * float(bar["raw_open"])
                            post_equity = equity - amount - fee + marked
                            committed = max(amount + fee, marked)
                            fits = (post_equity > 0 and amount + fee <= cash + 1e-8
                                    and committed <= weight * post_equity + 1e-8
                                    and sector_value + committed <= self.config.max_sector_weight * post_equity + 1e-8)
                        else:
                            fits = False
                        if fits:
                            permitted = preview
                            low = lots + 1
                        else:
                            high = lots - 1
                    if permitted is None:
                        rejections.append(dict(date=date, symbol=symbol, side="BUY", reason="post_fill_risk_budget"))
                        continue
                    result = permitted
                    result["status"] = "FILLED" if result["shares"] == requested else "PARTIALLY_FILLED"
                    shares, price = int(result["shares"]), float(result["price"])
                    amount, fee = shares * price, self._fees("BUY", shares * price)
                    if amount + fee > cash + 1e-8:
                        rejections.append(dict(date=date, symbol=symbol, side="BUY", reason="insufficient_cash"))
                        continue
                    consumed[(symbol, "BUY")] = consumed.get((symbol, "BUY"), 0) + shares
                    cash -= amount + fee
                    day_notional += amount
                    total_fees += fee
                    positions[symbol] = dict(name=stocks[symbol].get("name", symbol), shares=shares,
                                             buy_price=price, cost_price=(amount+fee)/shares,
                                             buy_date=date, buy_index=index, remaining_buy_fees=fee,
                                             last_price=price, peak_price=price,
                                             atr_pct=float((signal.get("atr_pct", 0.035) if signal else 0.035) or 0.035))
                    fills.append(dict(date=date, symbol=symbol, side="BUY", shares=shares, price=price,
                                      fees=fee, signal_date=as_of, status=result.get("status"),
                                      entry_reason="scheduled_rotation" if rebalance else "vacancy_refill"))

            # Run low-crossing stops only after all opening decisions. Their
            # proceeds must never finance an earlier opening buy. A same-day
            # acquisition can trigger an intent, but T+1 delays its execution.
            if self.config.stop_loss_mode == "intraday":
                for symbol, pos in list(positions.items()):
                    bar = stocks[symbol]["by_date"].get(date)
                    if not bar:
                        continue
                    high = float(bar.get("raw_high", bar.get("high", 0)))
                    low = float(bar.get("raw_low", bar.get("low", 0)))
                    pos["peak_price"] = max(pos.get("peak_price", pos["cost_price"]), high)
                    peak_profit = ((pos["peak_price"] / pos["cost_price"]) - 1.0) * 100.0

                    if self.strict_t1 and pos["buy_date"] >= date:
                        hard_stop_price = round(pos["cost_price"] * (1 + self.config.stop_loss_pct / 100), 2)
                        if low <= hard_stop_price:
                            pending_exits.setdefault(symbol, dict(
                                reason="盘中成本止损信号", signal_date=date,
                                trigger_date=date, stop_price=hard_stop_price))
                        continue

                    stop_order = None
                    # 1. 冲高回落动态跟踪止盈 (Trailing Take-Profit)
                    if (getattr(self.config, "trailing_stop_enabled", True)
                            and peak_profit >= float(getattr(self.config, "trailing_stop_activation_pct", 4.0))):
                        trailing_mode = getattr(self.config, "trailing_stop_mode", "fixed_pct")
                        if trailing_mode == "atr":
                            mult = float(getattr(self.config, "atr_trailing_multiplier", 2.0))
                            atr_pct = float(pos.get("atr_pct", 0.035) or 0.035)
                            trailing_drop = mult * atr_pct * 100.0
                        else:
                            trailing_drop = float(getattr(self.config, "trailing_stop_drop_pct", 2.0))
                        trailing_stop_price = round(pos["peak_price"] * (1.0 - trailing_drop / 100.0), 2)
                        if low <= trailing_stop_price and trailing_stop_price > pos["cost_price"]:
                            stop_order = dict(
                                reason=f"盘中动态跟踪止盈信号 ({'ATR模式' if trailing_mode == 'atr' else '固定比例'}，最高浮盈 +{peak_profit:.2f}%，自高点回落锁定)",
                                signal_date=date, trigger_date=date, stop_price=trailing_stop_price)

                    # 2. 保本止损防守 (Breakeven Defense)
                    if (stop_order is None and getattr(self.config, "breakeven_enabled", True)
                            and peak_profit >= float(getattr(self.config, "breakeven_activation_pct", 2.5))):
                        be_buf = float(getattr(self.config, "breakeven_buffer_pct", 0.3))
                        be_price = round(pos["cost_price"] * (1.0 + be_buf / 100.0), 2)
                        if low <= be_price:
                            stop_order = dict(
                                reason=f"盘中保本止损防守信号 (曾最高浮盈 +{peak_profit:.2f}%，保本离场)",
                                signal_date=date, trigger_date=date, stop_price=be_price)

                    # 3. 底线硬止损防守 (Hard Stop Loss)
                    if stop_order is None:
                        hard_stop_price = round(pos["cost_price"] * (1 + self.config.stop_loss_pct / 100), 2)
                        if low <= hard_stop_price:
                            stop_order = dict(
                                reason="盘中成本止损信号", signal_date=date,
                                trigger_date=date, stop_price=hard_stop_price)

                    if stop_order:
                        pending_exits.setdefault(symbol, stop_order)
                execute_exits(intraday=True)

            for symbol, pos in positions.items():
                bar = stocks[symbol]["by_date"].get(date)
                if bar:
                    pos["last_price"] = float(bar["raw_close"])
                    pos["peak_price"] = max(pos.get("peak_price", pos["cost_price"]), float(bar.get("raw_high", bar["high"])))
                    factor = bar.get("adjustment_factor")
                    previous_factor = observed_factors.get(symbol)
                    if factor and previous_factor and abs(float(factor)/previous_factor-1) > .005:
                        warnings.append("possible_corporate_action_while_held: " + symbol + " " + date)
                    if factor:
                        observed_factors[symbol] = float(factor)
            if getattr(self.config, "annual_cash_yield", 0.0) > 0 and cash > 0:
                daily_cash_yield = cash * (float(self.config.annual_cash_yield) / 100.0 / 252.0)
                cash += daily_cash_yield
            market_value = sum(marks(date).values())
            equity = cash + market_value
            industry_values = {}
            for symbol, value in marks(date).items():
                sector = sector_of(symbol, dated_industries)
                industry_values[sector] = industry_values.get(sector, 0) + value
            peak = max(peak, equity)
            bench_return = (float(bench_by_date[date]["close"])/bench_initial-1)*100
            curve.append(dict(date=date, equity=round(equity, 2), cash=round(cash, 2),
                              market_value=round(market_value, 2), pnl=round(equity-self.initial_cash, 2),
                              pnl_pct=round((equity/self.initial_cash-1)*100, 2),
                              benchmark_equity=round(self.initial_cash*(1+bench_return/100), 2),
                              benchmark_return_pct=round(bench_return, 2),
                              drawdown=round(peak-equity, 2), drawdown_pct=round((1-equity/peak)*100, 2),
                              regime=regime,
                              holding_count=len(positions), traded_notional=round(day_notional, 2),
                              turnover=day_notional/(2*equity) if equity > 0 else 0,
                              gross_exposure=market_value/equity if equity > 0 else 0,
                              industry_exposure={s: v/equity if equity > 0 else 0 for s, v in industry_values.items()}))

        max_win = max([t["pnl_pct"] for t in trades]) if trades else 0.0
        strong_regime_points = [p for p in curve if p.get("regime") == "STRONG_RISK_ON"]
        avg_strong_regime_exposure = (statistics.mean(p["gross_exposure"] for p in strong_regime_points)
                                     if strong_regime_points else 0.0)
        win_count = sum(t["net_pnl"] > 0 for t in trades)
        loss_count = len(trades)-win_count
        avg_win = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)/win_count if win_count else 0
        avg_loss = abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] <= 0)/loss_count) if loss_count else 0
        final = curve[-1]["equity"]
        total_return = (final/self.initial_cash-1)*100
        cagr = ((final/self.initial_cash)**(252/len(curve))-1)*100 if final > 0 else -100
        daily = []
        last = self.initial_cash
        for point in curve:
            daily.append(point["equity"]/last-1)
            last = point["equity"]
        stdev = statistics.stdev(daily) if len(daily) > 1 else 0
        sharpe = (statistics.mean(daily)-.02/252)/stdev*math.sqrt(252) if stdev > 1e-9 else 0
        max_dd = max(p["drawdown_pct"] for p in curve)
        sectors = {s for point in curve for s in point["industry_exposure"]}
        sector_max = {s: max(p["industry_exposure"].get(s, 0) for p in curve) for s in sorted(sectors)}
        if any(dates[-1] not in stocks[s]["by_date"] for s in positions):
            warnings.append("terminal_stale_marks: suspended or missing positions retain last raw close; no recovery assumed")
        summary = dict(strategy_name=self.strategy_name, pool_type=self.pool_type,
                       universe_mode="historical_eligibility_research" if self.universe_intervals is not None else "fixed_pool_research_only",
                       symbol_count=len(self.symbols), start_date=self.start_date, end_date=self.end_date,
                       actual_start_date=dates[0], actual_end_date=dates[-1], trading_days=len(curve),
                       initial_cash=self.initial_cash, final_equity=final, total_pnl=round(final-self.initial_cash, 2),
                       total_return_pct=round(total_return, 2), cagr_pct=round(cagr, 2), benchmark_code=self.benchmark_code,
                       benchmark_return_pct=curve[-1]["benchmark_return_pct"],
                       alpha_pct=round(total_return-curve[-1]["benchmark_return_pct"], 2),
                       excess_return_pct=round(total_return-curve[-1]["benchmark_return_pct"], 2),
                       max_drawdown_pct=max_dd, max_drawdown_val=max(p["drawdown"] for p in curve),
                       sharpe_ratio=round(sharpe, 3), calmar_ratio=round(cagr/max_dd, 3) if max_dd else 0,
                       total_trades=len(trades), win_rate=round(win_count/len(trades)*100, 2) if trades else 0,
                       win_count=win_count, loss_count=loss_count, avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
                       profit_loss_ratio=round(avg_win/avg_loss, 2) if avg_loss else 0,
                       avg_holding_days=round(statistics.mean(t["holding_days"] for t in trades), 2) if trades else 0,
                       total_fees=round(total_fees, 2), fill_count=len(fills),
                       avg_gross_exposure=statistics.mean(p["gross_exposure"] for p in curve),
                       max_gross_exposure=max(p["gross_exposure"] for p in curve),
                       max_win_pct=round(max_win, 2),
                       avg_strong_exposure=round(avg_strong_regime_exposure*100, 2),
                       strong_regime_days=len(strong_regime_points),
                       annualized_turnover=statistics.mean(p["turnover"] for p in curve)*252,
                       total_traded_notional=round(sum(p["traded_notional"] for p in curve), 2),
                       turnover_definition="252 * mean(daily gross buy+sell notional / (2 * closing equity))",
                       industry_exposure_max=sector_max,
                       industry_mapping_coverage=round(1-missing_industry/max(1, len(self.symbols)), 4))
        return dict(summary=summary, parameters=asdict(self.config), equity_curve=curve,
                    trades=trades, fills=fills, rejections=rejections,
                    open_positions={s: dict(p) for s, p in positions.items()},
                    warnings=list(dict.fromkeys(warnings)), data_coverage=data_coverage,
                    pending_exits={s: dict(order) for s, order in pending_exits.items()},
                    execution_model="previous_close_signal_next_raw_open; daily_capacity_approximation"
                    + ("; vacancy_refill_next_open" if self.config.refill_vacancies else "")
                    + ("; intraday_stop_ohlc_approximation" if self.config.stop_loss_mode == "intraday" else ""))
