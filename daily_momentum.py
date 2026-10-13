"""Completed daily signals, next-session execution, persistent order intent."""
import datetime as dt
import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path
from dataclasses import asdict

from history_data import HistoricalDataFeed, previous_trading_day, last_completed_trading_day, normalize_symbol_for_tencent
from strategy_rules import (MomentumConfig, evaluate_momentum, holding_signal,
                            exit_reason, target_weight, allowed_by_sector, sector_of)
from trade_calendar import BEIJING, is_trading_day, is_trading_time

log = logging.getLogger(__name__)


def sessions_between(start, end):
    if not start or start >= end:
        return 0
    current = dt.date.fromisoformat(start) + dt.timedelta(days=1)
    last = dt.date.fromisoformat(end)
    count = 0
    while current <= last:
        count += bool(is_trading_day(current))
        current += dt.timedelta(days=1)
    return count


class DailyMomentumStrategy:
    def __init__(self, account, watchlist, name="Momentum_Rotation_Pro", feed=None,
                 industry_map=None, **kwargs):
        self.account, self.name = account, name
        self.watchlist = list(dict.fromkeys(watchlist))
        defaults = {} if name.endswith("Pro") else {"use_ma120": False, "use_high_filter": False}
        self.config = MomentumConfig.from_dict({**defaults, **kwargs})
        self.display_name = "日线动量增强（收盘信号 / 次日撮合）" if name.endswith("Pro") else "基础日线动量（收盘信号 / 次日撮合）"
        self.feed = feed or HistoricalDataFeed()
        self._industry_overrides = dict(industry_map if industry_map is not None else self._load_industries())
        self.industry_map = dict(self._industry_overrides)
        self.state = account.strategy_state.setdefault(name, {})
        self.state.setdefault("cooldown", {})
        self.state.setdefault("pending_exits", {})
        self.state.setdefault("exit_signal_dates", {})
        self.state.setdefault("pending_entries", {})
        self.history_cache = {}
        self.latest_candidates = []
        self.data_status = {"status": "not_loaded", "requested": len(self.watchlist), "loaded": 0}
        self._history_date = None
        self._history_lock = threading.Lock()
        self._history_thread = None
        self._last_attempt = 0
        self._generation = 0
        self._history_revision = 0
        self._published_generation = None
        self._history_valid_until = 0
        self._entry_eligible = set()
        self._benchmark_valid = False

    def _new_risk_data_valid(self):
        return (self._published_generation == self._generation
                and time.monotonic() < self._history_valid_until
                and self.data_status.get("status") in ("ready", "partial"))

    @staticmethod
    def _load_industries():
        path = Path(__file__).resolve().parent / "data" / "industry_map.json"
        if path.exists():
            return json.loads(path.read_text()).get("industries", {})
        return {}

    def set_config(self, updates):
        config = MomentumConfig.from_dict(asdict(self.config), **updates)
        with self._history_lock:
            self.config = config
            # An in-flight loader captured the old configuration. Invalidate its
            # publication and force benchmark preparation on a filter change.
            self._generation += 1
            self._last_attempt = 0
            self.state.pop("planned_revision", None)

    def set_watchlist(self, symbols):
        with self._history_lock:
            self.watchlist = list(dict.fromkeys(normalize_symbol_for_tencent(s) for s in symbols))
            self._generation += 1
            self._last_attempt = 0
            self.state.pop("planned_revision", None)

    @property
    def max_positions(self):
        return self.config.max_positions

    @property
    def max_stock_weight(self):
        return self.config.max_stock_weight

    @property
    def stop_loss_pct(self):
        return self.config.stop_loss_pct

    @stop_loss_pct.setter
    def stop_loss_pct(self, value):
        self.set_config({"stop_loss_pct": value})

    def prepare(self, now=None, synchronous=False):
        """Download outside the account lock; poll/HTTP remain responsive."""
        now = now or dt.datetime.now(BEIJING)
        as_of = last_completed_trading_day(now)
        with self._history_lock:
            symbols = list(dict.fromkeys(self.watchlist + list(self.account.positions)))
            if (self._history_date == as_of and self.data_status.get("status") == "ready"
                    and self._new_risk_data_valid()
                    and set(symbols) <= set(self.history_cache)):
                return
            if self._history_thread and self._history_thread.is_alive():
                return
            if not synchronous and time.monotonic() - self._last_attempt < 300:
                return
            self._last_attempt = time.monotonic()
            self._generation += 1
            generation = self._generation
            config = self.config
            self.data_status = dict(status="loading", as_of=as_of, requested=len(symbols), loaded=0)
        def load():
            try:
                if hasattr(self.feed, "fetch_industry_map"):
                    industries = {**self.feed.fetch_industry_map(), **self._industry_overrides}
                else:
                    industries = dict(self.industry_map)
                start = (dt.date.fromisoformat(as_of) - dt.timedelta(days=400)).isoformat()
                data = self.feed.fetch_universe_klines(symbols, start_date=start, end_date=as_of, max_workers=6)
                usable, entry_eligible, errors, gaps = {}, set(), [], []
                ttl = getattr(self.feed, "CACHE_TTL", dt.timedelta(hours=24)).total_seconds()
                remaining_ttl = ttl
                for sym in symbols:
                    item = data.get(sym, {})
                    bars = item.get("bars", [])
                    meta = item.get("metadata", {})
                    # Retain dated bars for existing-position risk management.
                    # A suspension or pre-IPO gap in the warmup range does not
                    # erase a valid latest close or make 130 real bars unusable.
                    usable[sym] = bars
                    last_valid = bool(bars) and bars[-1].get("date") == as_of and HistoricalDataFeed._has_raw(bars[-1])
                    if (last_valid and meta.get("fresh") is True
                            and meta.get("raw_prices_available", False)
                            and not (item.get("error") or meta.get("error"))):
                        entry_eligible.add(sym)
                        if meta.get("fetched_at"):
                            try:
                                fetched = dt.datetime.fromisoformat(meta["fetched_at"])
                                if fetched.tzinfo is None:
                                    fetched = fetched.replace(tzinfo=dt.timezone.utc)
                                age = max(0, (dt.datetime.now(dt.timezone.utc)-fetched).total_seconds())
                                if age >= ttl:
                                    entry_eligible.discard(sym)
                                    errors.append({"symbol": sym, "error": "缓存已超过有效期"})
                                else:
                                    remaining_ttl = min(remaining_ttl, ttl-age)
                            except (TypeError, ValueError):
                                entry_eligible.discard(sym)
                                errors.append({"symbol": sym, "error": "缓存时间戳无效"})
                    else:
                        errors.append({"symbol": sym, "error": item.get("error") or meta.get("warning") or "历史缺失、过期或缺原始价格"})
                    if meta.get("coverage_complete") is False:
                        gaps.append({"symbol": sym, "coverage": meta.get("coverage", {}), "warning": meta.get("warning", [])})
                benchmark = []
                benchmark_valid = not config.market_filter
                if config.market_filter:
                    benchmark = self.feed.fetch_benchmark_kline(start_date=start, end_date=as_of)
                    benchmeta = getattr(self.feed, "metadata", {}).get("idx_000300", {})
                    benchmark_valid = (benchmeta.get("fresh") is True and not benchmeta.get("error")
                                       and bool(benchmark) and benchmark[-1].get("date") == as_of)
                    if not benchmark_valid:
                        errors.append({"symbol": "_benchmark", "error": "基准行情缺失、过期或未验证"})
                    elif benchmeta.get("fetched_at"):
                        try:
                            fetched = dt.datetime.fromisoformat(benchmeta["fetched_at"])
                            if fetched.tzinfo is None:
                                fetched = fetched.replace(tzinfo=dt.timezone.utc)
                            age = max(0, (dt.datetime.now(dt.timezone.utc)-fetched).total_seconds())
                            benchmark_valid = age < ttl
                            remaining_ttl = min(remaining_ttl, max(0, ttl-age))
                        except (TypeError, ValueError):
                            benchmark_valid = False
                        if not benchmark_valid:
                            errors.append({"symbol": "_benchmark", "error": "基准缓存时间戳无效或已过期"})
                with self._history_lock:
                    if generation != self._generation:
                        return
                    # Include explicit empty entries: coverage never silently claims the full universe.
                    self.history_cache = {sym: usable.get(sym, []) for sym in symbols}
                    self._stock_names = {sym: data.get(sym, {}).get("name", sym) for sym in symbols}
                    self._benchmark = benchmark
                    self._benchmark_valid = benchmark_valid
                    self.industry_map = industries
                    self._entry_eligible = entry_eligible
                    self._history_date = as_of
                    self._published_generation = generation
                    self._history_valid_until = time.monotonic() + remaining_ttl
                    self._history_revision = uuid.uuid4().hex
                    self.data_status = dict(status="ready" if not errors else "partial" if entry_eligible else "failed",
                                            as_of=as_of, requested=len(symbols), loaded=len(entry_eligible),
                                            unavailable=len(errors), errors=errors[:10],
                                            coverage_gaps=gaps[:10], coverage_gap_count=len(gaps),
                                            industry_coverage=sum(s in self.industry_map for s in symbols),
                                            industry_data=getattr(self.feed, "metadata", {}).get("industry_map", {}),
                                            unknown_industry_policy="合并为未知行业，适用同一行业仓位上限")
            except Exception as exc:
                log.exception("日线数据准备失败")
                with self._history_lock:
                    if generation == self._generation:
                        self._entry_eligible = set()
                        self._benchmark_valid = False
                        self._history_date = as_of
                        self._published_generation = generation
                        self._history_valid_until = 0
                        self._history_revision = uuid.uuid4().hex
                        self.data_status = dict(status="failed", as_of=as_of, requested=len(symbols), loaded=0, error=str(exc))
        if synchronous:
            load()
        else:
            self._history_thread = threading.Thread(target=load, name="daily-signal-data", daemon=True)
            self._history_thread.start()

    def _plan(self, as_of, execution_day):
        signals = []
        new_risk_valid = self._new_risk_data_valid()
        for sym, bars in self.history_cache.items():
            if not new_risk_valid or sym not in self.watchlist or sym not in self._entry_eligible:
                continue
            code = sym[-6:]
            if not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
                continue
            name = getattr(self, "_stock_names", {}).get(sym, sym)
            if "ST" in name.upper() or "退" in name or name.upper().startswith("PT"):
                continue
            signal = evaluate_momentum(bars, as_of, self.config)
            if signal and signal.get("raw_signal_price") and signal.get("adjustment_factor"):
                signals.append({"symbol": sym, "code": code, "name": name,
                                "current": signal["raw_signal_price"], **signal})
        signals.sort(key=lambda c: (-c["score"], c["symbol"]))
        ranks = {c["symbol"]: i + 1 for i, c in enumerate(signals)}
        self.latest_candidates = signals[:50]
        rebalance = self.state.get("rebalance_as_of") == as_of or not self.state.get("last_rebalance") or sessions_between(self.state["last_rebalance"], as_of) >= self.config.rebalance_every
        exits = {sym: reason for sym, reason in self.state.get("pending_exits", {}).items()
                 if sym in self.account.positions}
        for sym, pos in self.account.positions.items():
            buy_day = min((lot["date"] for lot in pos.get("buy_lots", [])), default=execution_day)
            if buy_day > as_of:
                continue  # Today's new lot did not exist at yesterday's signal close.
            sig = holding_signal(self.history_cache.get(sym, []), as_of)
            if not sig:
                continue  # Missing history cannot authorize a fabricated exit.
            reason = exit_reason(sig["raw_signal_price"], pos["cost_price"], sig, self.config,
                                 current_adjusted_price=sig["signal_price"])
            held_days = sessions_between(buy_day, as_of)
            if (not reason and new_risk_valid and sym in self._entry_eligible and rebalance
                    and held_days >= self.config.min_holding_days
                    and ranks.get(sym, math.inf) > self.config.max_positions + self.config.rank_buffer):
                reason = "排名跌出缓冲区的轮动信号"
            if reason:
                exits.setdefault(sym, reason)
                self.state["exit_signal_dates"].setdefault(sym, as_of)
        self.state["pending_exits"] = exits
        previous_entries = self.state.get("pending_entries", {}) if self.state.get("planned_as_of") == as_of else {}
        entries = {}
        market_ok = True
        if self.config.market_filter:
            bench = getattr(self, "_benchmark", [])
            market_ok = self._benchmark_valid and len(bench) >= 120 and bench[-1]["date"] == as_of and bench[-1]["close"] > sum(b["close"] for b in bench[-120:]) / 120
        if not market_ok:
            entries = {}
        if rebalance and market_ok:
            for candidate in signals:
                sym = candidate["symbol"]
                old = previous_entries.get(sym)
                # Keep partial fills, but publish the new rank order and current
                # signal. An already held unrelated position is not a new buy.
                if sym in self.account.positions and old is None:
                    continue
                sold = self.state["cooldown"].get(sym)
                if sold and sessions_between(sold, execution_day) <= self.config.cooldown_days:
                    continue
                entries[sym] = dict(signal=candidate,
                                    filled_shares=old.get("filled_shares", 0) if old else 0,
                                    target_shares=old.get("target_shares") if old else None)
        self.state["pending_entries"] = entries
        self.state["planned_as_of"] = as_of
        self.state["planned_revision"] = self._history_revision
        self.state["execution_day"] = execution_day
        if rebalance:
            self.state["last_rebalance"] = as_of
            self.state["rebalance_as_of"] = as_of
        self.account.save()

    def on_tick(self, quotes, now=None):
        now = now or dt.datetime.now(BEIJING)
        self.prepare(now)
        if not is_trading_time(now):
            return
        as_of = previous_trading_day(now.date())
        if self._history_date != as_of:
            return
        execution_day = now.date().isoformat()
        def valid_past_date(value, cutoff):
            try:
                return dt.date.fromisoformat(value).isoformat() <= cutoff
            except (ValueError, TypeError):
                return False
        if self.state.get("last_rebalance") and not valid_past_date(self.state["last_rebalance"], as_of):
            self.state.pop("last_rebalance", None)
            self.state.pop("planned_as_of", None)
        self.state["cooldown"] = {sym: date for sym, date in self.state["cooldown"].items()
                                   if valid_past_date(date, execution_day)}
        self.account.update_t1_available_shares(today_str=execution_day)
        with self._history_lock:
            if self.state.get("planned_as_of") != as_of or self.state.get("planned_revision") != self._history_revision:
                self._plan(as_of, execution_day)
        self.account.bind_execution_quotes(quotes, now=now)
        self.account.participation_rate = self.config.participation_rate
        # Risk exits are retried while there is available depth; partial fills retain intent.
        for sym, reason in list(self.state["pending_exits"].items()):
            pos = self.account.positions.get(sym)
            if not pos:
                self.state["pending_exits"].pop(sym, None)
                self.state["exit_signal_dates"].pop(sym, None)
                continue
            avail = pos.get("available_shares", 0) if self.account.strict_t1 else pos["total_shares"]
            if avail <= 0:
                continue
            q = quotes.get(sym, {})
            signal_date = self.state["exit_signal_dates"].get(sym, as_of)
            def commit_sale(trade, account, symbol=sym):
                if symbol not in account.positions:
                    self.state["cooldown"][symbol] = execution_day
                    self.state["pending_exits"].pop(symbol, None)
                    self.state["exit_signal_dates"].pop(symbol, None)
                    self.state["pending_entries"].pop(symbol, None)
            result = self.account.execute_sell(sym, q.get("current", 0), avail,
                        reason=f"{self.name}: {signal_date}收盘{reason}，随后交易日执行",
                        state_update=commit_sale)
        total = self.account.get_summary()["total_equity"]
        weights = {s: p.get("market_value", 0) / total for s, p in self.account.positions.items()} if total > 0 else {}
        for sym, intent in list(self.state["pending_entries"].items()):
            if not self._new_risk_data_valid() or sym not in self._entry_eligible:
                continue
            if self.config.market_filter and not self._benchmark_valid:
                continue
            if sym in self.state["cooldown"] and sessions_between(self.state["cooldown"][sym], execution_day) <= self.config.cooldown_days:
                continue
            if sym not in self.account.positions and len(self.account.positions) >= self.config.max_positions:
                continue
            # Do not use pending new buys to evade a risk exit which cannot execute.
            if sym in self.state["pending_exits"]:
                continue
            q = quotes.get(sym, {})
            asks = q.get("asks", [])
            if not asks:
                continue
            price = asks[0].get("price", 0)
            if not price or not math.isfinite(price) or price <= 0:
                continue
            weight = target_weight(self.config, intent["signal"])
            if intent["target_shares"] is None:
                intent["target_shares"] = int(total * weight / price / 100) * 100
            remaining = intent["target_shares"] - intent["filled_shares"]
            # Recheck growing position value, cash, risk budget and sector cap on every retry.
            existing = self.account.positions.get(sym, {}).get("market_value", 0)
            remaining = min(remaining, int(max(0, total * weight - existing) / price / 100) * 100,
                            int(self.account.cash / (price * 1.002) / 100) * 100)
            if remaining < 100 or not allowed_by_sector(sym, remaining * price / total, weights,
                                                       self.industry_map, self.config.max_sector_weight):
                continue
            def commit_purchase(trade, account, entry=intent):
                entry["filled_shares"] += trade["shares"]
            result = self.account.execute_buy(sym, q.get("name", sym), price, remaining,
                        reason=f"{self.name}: {as_of}收盘动量排名，次日执行",
                        state_update=commit_purchase, max_position_weight=weight,
                        max_sector_weight=self.config.max_sector_weight,
                        industry_map=self.industry_map)
            if result.get("success"):
                weights[sym] = self.account.positions[sym]["market_value"] / total
        self.account.save()


class MomentumRotationProStrategy(DailyMomentumStrategy):
    def __init__(self, account, watchlist, **kwargs):
        super().__init__(account, watchlist, name="Momentum_Rotation_Pro", **kwargs)


class MomentumRotationStrategy(DailyMomentumStrategy):
    def __init__(self, account, watchlist, **kwargs):
        super().__init__(account, watchlist, name="Momentum_Rotation", **kwargs)
