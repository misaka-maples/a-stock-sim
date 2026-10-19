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

from history_data import HistoricalDataFeed, previous_trading_day, normalize_symbol_for_tencent
from trade_calendar import is_trading_day
from strategy_rules import (
    MomentumConfig, evaluate_momentum, holding_signal, exit_reason,
    target_weight, allowed_by_sector, sector_of, entry_guard_reason,
)
from execution import match_daily


MOMENTUM_STRATEGIES = {"Momentum_Rotation", "Momentum_Rotation_Pro"}


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
                 **kwargs):
        if strategy_name not in MOMENTUM_STRATEGIES:
            raise ValueError("Unknown momentum strategy")
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

    def run(self):
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
        stocks = self.feed.fetch_universe_klines(
            self.symbols, start_date=warmup_start, end_date=self.end_date)
        warnings = ["daily_open_approximation: signals use previous close; fills use next raw open",
                    "capacity_approximation: daily total volume limits fills, not opening-auction depth",
                    "corporate_actions_not_accounted: cash dividends, splits and delisting recoveries require an event feed"]
        if self.config.stop_loss_mode == "intraday":
            warnings.append("intraday_stop_ohlc_approximation: existing holdings stop at raw open on gaps or threshold minus slippage on low crossing; daily OHLC cannot prove tick order, depth or exact fills; T+1 and unfilled stop intents persist")
        if any(not _main_board(s) for s in self.symbols):
            warnings.append("non_main_board_excluded: ranking and execution both use Shanghai/Shenzhen main-board stocks only")
        warnings.append("historical_risk_status_unverified: dated ST/price-limit metadata takes precedence; current names are only a conservative fallback")
        if self.universe_intervals is None:
            warnings.append("fixed_pool_research_only: historical universe eligibility/survivorship unverified")
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
            info = dict(info, bars=bars, by_date={b["date"]: b for b in bars})
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
                bar = stocks[symbol]["by_date"].get(date, {})
                if "industry" in bar or "sector" in bar:
                    mapping[symbol] = bar.get("industry") or bar.get("sector")
            return mapping

        for index, date in enumerate(dates):
            day_notional = 0.0
            as_of = prior_date[date]
            dated_industries = industries_on(date)
            signals = {}
            rebalance = index % self.config.rebalance_every == 0
            if as_of:
                for symbol in self.symbols:
                    info = stocks[symbol]
                    signal_bar = info["by_date"].get(as_of, {})
                    name = str(signal_bar.get("name") or info.get("name", "")).upper()
                    risk_warning = (signal_bar["is_st"] if signal_bar.get("is_st") is not None
                                    else "ST" in name or "退" in name or name.startswith("PT"))
                    if (_main_board(symbol) and not risk_warning
                            and eligible_on(symbol, as_of, self.universe_intervals)):
                        signal = evaluate_momentum(stocks[symbol]["bars"], as_of, self.config)
                        if signal:
                            signals[symbol] = signal
            ranking = sorted(signals, key=lambda s: (-signals[s]["score"], s))
            ranks = {symbol: i + 1 for i, symbol in enumerate(ranking)}
            market_ok = True
            if self.config.market_filter:
                history = [b for b in benchmark if as_of and b["date"] <= as_of]
                market_ok = (len(history) >= 120 and history[-1]["date"] == as_of and float(history[-1]["close"])
                             > sum(float(b["close"]) for b in history[-120:]) / 120)

            # Orders only inspect completed bars. Failed/partial exits persist.
            if as_of:
                for symbol, pos in positions.items():
                    signal = holding_signal(stocks[symbol]["bars"], as_of)
                    if signal is None:
                        continue  # Missing evidence cannot authorize a new rank exit.
                    reason = exit_reason(signal["raw_signal_price"], pos["cost_price"],
                                         signal, self.config,
                                         current_adjusted_price=signal["signal_price"])
                    if (reason is None and rebalance and index - pos["buy_index"] - 1 >= self.config.min_holding_days
                            and ranks.get(symbol, math.inf) > self.config.max_positions + self.config.rank_buffer):
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
            # Establish positions only at scheduled rotations, after exits.
            if rebalance and market_ok:
                for symbol in ranking:
                    if len(positions) >= self.config.max_positions:
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
                    weight = target_weight(self.config, signals[symbol])
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
                                             last_price=price)
                    fills.append(dict(date=date, symbol=symbol, side="BUY", shares=shares, price=price,
                                      fees=fee, signal_date=as_of, status=result.get("status")))

            # Run low-crossing stops only after all opening decisions. Their
            # proceeds must never finance an earlier opening buy. A same-day
            # acquisition can trigger an intent, but T+1 delays its execution.
            if self.config.stop_loss_mode == "intraday":
                for symbol, pos in positions.items():
                    bar = stocks[symbol]["by_date"].get(date)
                    if not bar:
                        continue
                    stop = pos["cost_price"] * (1 + self.config.stop_loss_pct / 100)
                    if float(bar["raw_low"]) <= stop:
                        pending_exits.setdefault(symbol, dict(
                            reason="盘中成本止损信号", signal_date=date,
                            trigger_date=date, stop_price=stop))
                execute_exits(intraday=True)

            for symbol, pos in positions.items():
                bar = stocks[symbol]["by_date"].get(date)
                if bar:
                    pos["last_price"] = float(bar["raw_close"])
                    factor = bar.get("adjustment_factor")
                    previous_factor = observed_factors.get(symbol)
                    if factor and previous_factor and abs(float(factor)/previous_factor-1) > .005:
                        warnings.append("possible_corporate_action_while_held: " + symbol + " " + date)
                    if factor:
                        observed_factors[symbol] = float(factor)
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
                              holding_count=len(positions), traded_notional=round(day_notional, 2),
                              turnover=day_notional/(2*equity) if equity > 0 else 0,
                              gross_exposure=market_value/equity if equity > 0 else 0,
                              industry_exposure={s: v/equity if equity > 0 else 0 for s, v in industry_values.items()}))

        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        avg_win = sum(t["net_pnl"] for t in wins)/len(wins) if wins else 0
        avg_loss = abs(sum(t["net_pnl"] for t in losses)/len(losses)) if losses else 0
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
                       total_trades=len(trades), win_rate=round(len(wins)/len(trades)*100, 2) if trades else 0,
                       win_count=len(wins), loss_count=len(losses), avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
                       profit_loss_ratio=round(avg_win/avg_loss, 2) if avg_loss else 0,
                       avg_holding_days=round(statistics.mean(t["holding_days"] for t in trades), 2) if trades else 0,
                       total_fees=round(total_fees, 2), fill_count=len(fills),
                       avg_gross_exposure=statistics.mean(p["gross_exposure"] for p in curve),
                       max_gross_exposure=max(p["gross_exposure"] for p in curve),
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
                    + ("; intraday_stop_ohlc_approximation" if self.config.stop_loss_mode == "intraday" else ""))
