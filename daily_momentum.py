"""Completed daily signals, next-session execution, persistent order intent."""
import datetime as dt
import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from dataclasses import asdict

from history_data import HistoricalDataFeed, previous_trading_day, last_completed_trading_day, normalize_symbol_for_tencent
from strategy_rules import (MomentumConfig, evaluate_momentum, holding_signal,
                            exit_reason, target_weight, allowed_by_sector, sector_of,
                            entry_guard_reason)
from execution import _timestamp
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
    FAILED_RETRY_SECONDS = 900
    FAILED_RETRY_BATCH = 24
    CATCHUP_BATCH = 96
    CATCHUP_SECONDS = 5
    HOLDING_RETRY_SECONDS = 60

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
        self.state.setdefault("entry_rejections", [])
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
        self._history_items = {}
        self._history_errors = {}
        self._next_failed_retry = 0
        self._industry_refresh_needed = False
        self._symbol_retry_due = {}
        self._next_industry_retry = 0
        account_path = getattr(account, "save_path", None)
        self._retry_path = Path(account_path).parent / "history_refresh_retries.json" if account_path else None
        try:
            saved = json.loads(self._retry_path.read_text()) if self._retry_path else {}
            records = saved.get("symbols", {}) if isinstance(saved, dict) else {}
            self._retry_records = {sym: record for sym, record in records.items()
                                   if isinstance(sym, str) and isinstance(record, dict)
                                   and isinstance(record.get("retry_at"), (int, float))
                                   and math.isfinite(record["retry_at"])
                                   and isinstance(record.get("failures", 0), int)
                                   and 0 <= record.get("failures", 0) < 1000000
                                   and isinstance(record.get("total_failures", 0), int)
                                   and 0 <= record.get("total_failures", 0) < 1000000} if isinstance(records, dict) else {}
        except (OSError, ValueError, TypeError):
            self._retry_records = {}
        self._next_nonholding_refresh = 0

    def _new_risk_data_valid(self):
        return (self._published_generation == self._generation
                and time.monotonic() < self._history_valid_until
                and self.data_status.get("status") in ("ready", "partial")
                and set(self.watchlist+list(self.account.positions)) <= set(self.history_cache))

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

    def _history_issue(self, item, as_of):
        bars, meta = item.get("bars", []), item.get("metadata", {})
        count = len({bar.get("date") for bar in bars if bar.get("date", "") <= as_of})
        details = dict(last_date=bars[-1].get("date") if bars else None, actual_bars=count)
        ttl = getattr(self.feed, "CACHE_TTL", dt.timedelta(hours=24)).total_seconds()
        if not bars:
            return ("no_cache" if not meta.get("cache_available", False) else "no_history"), details, ttl
        if bars[-1].get("date") != as_of:
            return "latest_outdated", details, ttl
        if item.get("error") or meta.get("error"):
            return "error", details, ttl
        if not HistoricalDataFeed._has_raw(bars[-1]) or not meta.get("raw_prices_available", False):
            return "raw_missing", details, ttl
        if meta.get("fresh") is not True:
            return ("stale" if meta.get("fresh") is False else "unverified"), details, ttl
        if meta.get("fetched_at"):
            try:
                fetched = dt.datetime.fromisoformat(meta["fetched_at"])
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=dt.timezone.utc)
                age = (dt.datetime.now(dt.timezone.utc)-fetched).total_seconds()
                if age < 0 or age >= ttl:
                    return "stale", details, ttl
                ttl -= age
            except (ValueError, TypeError):
                return "invalid_timestamp", details, ttl
        if count < 125:
            return "short", details, ttl
        return None, details, ttl

    def _publish_history(self, items, symbols, as_of, generation, industries, benchmark,
                         benchmark_valid, benchmark_ttl, repair=False):
        """Publish one coherent snapshot; unavailable names never zero good names."""
        if generation != self._generation:
            return
        eligible, errors, gaps, counts = set(), {}, [], {}
        latest_raw = 0
        remaining = getattr(self.feed, "CACHE_TTL", dt.timedelta(hours=24)).total_seconds()
        for sym in symbols:
            item = items.get(sym, {})
            bars, meta = item.get("bars", []), item.get("metadata", {})
            if bars and bars[-1].get("date") == as_of and HistoricalDataFeed._has_raw(bars[-1]):
                latest_raw += 1
            reason, details, lifetime = self._history_issue(item, as_of)
            if reason:
                errors[sym] = dict(symbol=sym, reason=reason, **details,
                                   error=item.get("error") or meta.get("error") or meta.get("warning"))
                counts[reason] = counts.get(reason, 0)+1
            else:
                eligible.add(sym)
                remaining = min(remaining, lifetime)
            if meta.get("coverage_complete") is False:
                gaps.append(dict(symbol=sym, coverage=meta.get("coverage", {}), warning=meta.get("warning", [])))
        # Rotate only attempted failures to the end so later batches get a turn.
        if repair:
            old_order = [sym for sym in self._history_errors if sym in errors]
            errors = {sym: errors[sym] for sym in old_order} | {sym: error for sym, error in errors.items() if sym not in old_order}
        self._history_items = {sym: dict(name=items.get(sym, {}).get("name", sym),
                                        bars=items.get(sym, {}).get("bars", []),
                                        metadata=items.get(sym, {}).get("metadata", {}),
                                        error=items.get(sym, {}).get("error")) for sym in symbols}
        self.history_cache = {sym: item["bars"] for sym, item in self._history_items.items()}
        self._stock_names = {sym: item["name"] for sym, item in self._history_items.items()}
        self._history_errors = errors
        self._benchmark, self._benchmark_valid = benchmark, benchmark_valid
        self.industry_map = industries
        self._entry_eligible = eligible
        self._history_date, self._published_generation = as_of, generation
        self._history_valid_until = time.monotonic()+min(remaining, benchmark_ttl)
        self._history_revision = uuid.uuid4().hex
        self._next_failed_retry = time.monotonic()+self.FAILED_RETRY_SECONDS
        unavailable = len(errors)
        samples = list(errors.values())[:10]
        if not benchmark_valid and len(samples) < 10:
            samples.append(dict(symbol="_benchmark", reason="benchmark_unavailable", error="基准行情缺失、过期或未验证"))
        scheduling = {key: self.data_status[key] for key in ("update_mode", "deferred_history", "next_full_update_window")
                      if key in self.data_status}
        self.data_status = dict(**scheduling, status="ready" if not unavailable and benchmark_valid else "partial" if eligible else "failed",
                                as_of=as_of, requested=len(symbols), loaded=len(eligible), usable_history=len(eligible),
                                latest_raw_history=latest_raw, retained_history=sum(bool(b) for b in self.history_cache.values()),
                                unavailable=unavailable, unavailable_reason_counts=counts,
                                errors=samples, coverage_gaps=gaps[:10], coverage_gap_count=len(gaps),
                                benchmark_available=benchmark_valid, refreshing=False,
                                next_failed_retry_seconds=self.FAILED_RETRY_SECONDS,
                                industry_coverage=sum(s in self.industry_map for s in symbols),
                                industry_data=getattr(self.feed, "metadata", {}).get("industry_map", {}),
                                unknown_industry_policy="合并为未知行业，适用同一行业仓位上限",
                                entry_rejections=self.state["entry_rejections"][-20:])
        matches = self._ranked_signals(as_of)
        self.latest_candidates = matches[:50]
        self.data_status.update(candidate_count=len(matches), preview_count=len(self.latest_candidates))
        self.data_status["ranking_coverage"] = self._ranking_coverage()
        self.data_status["rotation_ready"] = self._ranking_coverage() >= .95

    def _ranking_coverage(self):
        # Known sufficiently long series are expected to update as a group.
        # Dead/unlisted codes with no history do not prevent a useful quorum.
        expected = {sym for sym, bars in self.history_cache.items()
                    if sym in self.watchlist and len(bars) >= 125}
        return len(expected & self._entry_eligible)/len(expected) if expected else 0.

    def _prioritize_updates(self, symbols):
        symbols = list(dict.fromkeys(symbols))
        held = [sym for sym in self.account.positions if sym in symbols]
        warm = [sym for sym in symbols if sym not in held and self.history_cache.get(sym)]
        cold = [sym for sym in symbols if sym not in held and not self.history_cache.get(sym)]
        return held+warm+cold

    @staticmethod
    def _conservative_update_window(now):
        """Reserve broad catch-up for when completed closing data is available."""
        local = now.astimezone(BEIJING) if now.tzinfo else now.replace(tzinfo=BEIJING)
        return is_trading_day(local.date()) and dt.time(8, 30) <= local.time() < dt.time(16, 30)

    def _select_update_batch(self, symbols, now, catchup=True):
        now_epoch = now.timestamp()
        priority = self._prioritize_updates(symbols)
        held = [sym for sym in priority if sym in self.account.positions]
        others = [sym for sym in priority if sym not in self.account.positions
                  and now_epoch >= self._retry_records.get(sym, {}).get("retry_at", 0)]
        conservative = self._conservative_update_window(now)
        if conservative and time.monotonic() < self._next_nonholding_refresh:
            others = []
        limit = self.FAILED_RETRY_BATCH if conservative or not catchup else self.CATCHUP_BATCH
        local = now.astimezone(BEIJING) if now.tzinfo else now.replace(tzinfo=BEIJING)
        day = local.date()
        if not is_trading_day(day) or local.time() >= dt.time(16, 30):
            day += dt.timedelta(days=1)
        while not is_trading_day(day):
            day += dt.timedelta(days=1)
        self.data_status.update(update_mode="holding_and_missing_only" if conservative else "closing_catchup",
                                deferred_history=max(0, len(priority)-len(held)-min(limit, len(others))),
                                next_full_update_window=dt.datetime.combine(day, dt.time(16, 30), BEIJING).isoformat())
        # Isolate holding updates so a slow nonholding cannot delay publication.
        return held or others[:limit]

    def _record_update_results(self, requested, now, as_of):
        nonheld = False
        for sym in requested:
            if sym in self.account.positions:
                continue
            nonheld = True
            if sym not in self._history_errors:
                self._retry_records.pop(sym, None)
                continue
            issue = self._history_errors[sym]
            previous = self._retry_records.get(sym, {})
            failures = previous.get("failures", 0)+1 if previous.get("as_of") == as_of else 1
            if not self.history_cache.get(sym):
                # Repeatedly empty legacy codes get a weekly retry, never removal.
                total_failures = previous.get("total_failures", 0)+1
                retry_at = now.timestamp()+86400*(7 if total_failures >= 3 else 1)
            elif failures >= 2:
                day = now.astimezone(BEIJING).date()+dt.timedelta(days=1)
                while not is_trading_day(day):
                    day += dt.timedelta(days=1)
                retry_at = dt.datetime.combine(day, dt.time(16, 30), BEIJING).timestamp()
            else:
                retry_at = now.timestamp()+self.FAILED_RETRY_SECONDS
            self._retry_records[sym] = dict(as_of=as_of, failures=failures, retry_at=retry_at,
                                            total_failures=previous.get("total_failures", 0)+1,
                                            reason=issue["reason"])
        if nonheld and self._conservative_update_window(now):
            self._next_nonholding_refresh = time.monotonic()+self.FAILED_RETRY_SECONDS
        if self._retry_path:
            temporary = None
            try:
                # Bound old records while retaining today's failed retired codes.
                records = {s: r for s, r in self._retry_records.items()
                           if r.get("retry_at", 0) >= now.timestamp()-86400}
                with tempfile.NamedTemporaryFile(mode="w", dir=self._retry_path.parent,
                                                 prefix=".history_retries_", delete=False) as handle:
                    temporary = handle.name
                    json.dump(dict(symbols=records), handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._retry_path)
            except OSError:
                log.exception("无法保存历史更新冷却记录")
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)

    def _catchup_needed(self):
        return (len(self._history_errors) >= self.CATCHUP_BATCH
                or any(issue["reason"] in ("latest_outdated", "stale") and self.history_cache.get(sym)
                       for sym, issue in self._history_errors.items()))

    def _source_health(self):
        sources = getattr(self.feed, "_sources", None)
        return sources.status() if sources is not None else {}

    def _source_retry_delay(self):
        health = self._source_health()
        enabled = ["eastmoney", "tencent"]
        if health.get("baostock", {}).get("installed"):
            enabled.append("baostock")
        if health.get("tushare", {}).get("configured"):
            enabled.append("tushare")
        states = health.get("circuits", {})
        if all(states.get(name, {}).get("state") == "open" for name in enabled):
            return max(1, min(states[name]["retry_after_seconds"] for name in enabled))
        return self.CATCHUP_SECONDS

    def _benchmark_evidence(self, bars, as_of):
        meta = getattr(self.feed, "metadata", {}).get("idx_000300", {})
        ttl = getattr(self.feed, "CACHE_TTL", dt.timedelta(hours=24)).total_seconds()
        valid = (bool(bars) and bars[-1].get("date") == as_of and meta.get("fresh") is True
                 and not meta.get("error") and meta.get("raw_prices_available", True)
                 and meta.get("cache_hit", True))
        if valid and meta.get("fetched_at"):
            try:
                fetched = dt.datetime.fromisoformat(meta["fetched_at"])
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=dt.timezone.utc)
                age = (dt.datetime.now(dt.timezone.utc)-fetched).total_seconds()
                valid = 0 <= age < ttl
                if valid:
                    ttl -= age
            except (ValueError, TypeError):
                valid = False
        return valid, ttl

    def _industry_refresh_required(self, symbols):
        if not hasattr(self.feed, "peek_industry_map"):
            return False
        if all(self._industry_overrides.get(sym) for sym in symbols):
            return False
        return getattr(self.feed, "metadata", {}).get("industry_map", {}).get("fresh") is not True

    def prepare(self, now=None, synchronous=False):
        """Reuse valid partial data; repair only failed names without blocking it."""
        now = now or dt.datetime.now(BEIJING)
        as_of = last_completed_trading_day(now)
        with self._history_lock:
            symbols = list(dict.fromkeys(self.watchlist+list(self.account.positions)))
            current = (self._history_date == as_of and self._published_generation == self._generation
                       and time.monotonic() < self._history_valid_until
                       and set(symbols) <= set(self.history_cache))
            if self._history_thread and self._history_thread.is_alive():
                return
            repair = current and not synchronous
            if current and self.data_status.get("status") == "ready" and not self._industry_refresh_needed:
                return
            if repair:
                if time.monotonic() < self._next_failed_retry:
                    return
                retryable = [sym for sym, issue in self._history_errors.items() if issue["reason"] != "short"]
                retryable = [sym for sym in self._prioritize_updates(retryable)
                             if time.monotonic() >= self._symbol_retry_due.get(sym, 0)]
                catchup = self._catchup_needed()
                requested = self._select_update_batch(retryable, now, catchup=catchup)
                retry_benchmark = self.config.market_filter and not self._benchmark_valid
                retry_industry = self._industry_refresh_needed and time.monotonic() >= self._next_industry_retry
                if not requested and not retry_benchmark and not retry_industry:
                    future = [due for sym, due in self._symbol_retry_due.items()
                              if sym in self._history_errors and due > time.monotonic()]
                    self._next_failed_retry = min(future) if future else time.monotonic()+self.FAILED_RETRY_SECONDS
                    return
                generation = self._generation  # Published good names remain authorized.
                self.data_status = dict(self.data_status, refreshing=True,
                                        refresh=dict(mode="failed_symbols", requested=len(requested), status="loading"))
            else:
                context_changed = (self._history_date != as_of or self._published_generation != self._generation
                                   or not set(symbols) <= set(self.history_cache))
                if not synchronous and not context_changed and time.monotonic()-self._last_attempt < 300:
                    return
                self._generation += 1
                self._symbol_retry_due = {}
                generation = self._generation
                requested, retry_benchmark, retry_industry = symbols, self.config.market_filter, False
                self.data_status = dict(status="loading", as_of=as_of, requested=len(symbols), loaded=0,
                                        usable_history=0, refreshing=True, previous_usable_history=len(self._entry_eligible))
            self._last_attempt = time.monotonic()
            config = self.config

        def load(requested=requested, retry_benchmark=retry_benchmark, retry_industry=retry_industry):
            seeded = False
            try:
                industries = dict(self.industry_map)
                start = (dt.date.fromisoformat(as_of)-dt.timedelta(days=400)).isoformat()
                cached = {}
                benchmark = getattr(self, "_benchmark", []) if repair else []
                benchmark_valid = self._benchmark_valid if repair else not config.market_filter
                benchmark_ttl = (max(0, self._history_valid_until-time.monotonic()) if repair else
                                 getattr(self.feed, "CACHE_TTL", dt.timedelta(hours=24)).total_seconds())
                if not repair and hasattr(self.feed, "peek_universe"):
                    # Publish verified disk data before a retired/unknown name
                    # can hold the first batch open for minutes. Holdings first.
                    priority = list(dict.fromkeys(list(self.account.positions)+symbols))
                    cached = self.feed.peek_universe(priority, start_date=start, end_date=as_of)
                    if hasattr(self.feed, "peek_industry_map"):
                        industries = {**self.feed.peek_industry_map(), **self._industry_overrides}
                    retry_industry = self._industry_refresh_required(symbols)
                    if config.market_filter and hasattr(self.feed, "peek_benchmark_kline"):
                        benchmark = self.feed.peek_benchmark_kline(start_date=start, end_date=as_of)
                        benchmark_valid, benchmark_ttl = self._benchmark_evidence(benchmark, as_of)
                    requested = [sym for sym in symbols if not cached.get(sym, {}).get("metadata", {}).get("cache_hit")]
                    retry_benchmark = config.market_filter and not benchmark_valid
                    with self._history_lock:
                        if generation != self._generation:
                            return
                        if cached:
                            self._industry_refresh_needed = retry_industry
                            self._publish_history(cached, symbols, as_of, generation, industries, benchmark,
                                                  benchmark_valid, benchmark_ttl)
                            seeded = True
                            pending = bool(requested or retry_benchmark or retry_industry)
                            self.data_status = dict(self.data_status, refreshing=pending,
                                                    bootstrap=True, refresh=dict(mode="cache_bootstrap",
                                                    requested=len(requested), industry_pending=retry_industry,
                                                    status="loading" if pending else "complete"))
                    # Production publishes bounded batches. Do not wait for
                    # thousands of failing requests before exposing holdings.
                    if not synchronous:
                        requested = self._select_update_batch(requested, now)
                    if seeded and not requested and not retry_benchmark and not retry_industry:
                        with self._history_lock:
                            if generation == self._generation:
                                self.data_status = dict(self.data_status, refreshing=False,
                                                        refresh=dict(mode="cache_bootstrap", requested=0,
                                                                     status="deferred" if self._history_errors else "complete"))
                        return
                if (not repair or retry_industry) and hasattr(self.feed, "fetch_industry_map"):
                    industries = {**self.feed.fetch_industry_map(), **self._industry_overrides}
                    self._next_industry_retry = time.monotonic()+self.FAILED_RETRY_SECONDS
                fetched = self.feed.fetch_universe_klines(requested, start_date=start, end_date=as_of, max_workers=6) if requested else {}
                data = {**cached, **fetched} if cached else fetched
                if retry_benchmark:
                    benchmark = self.feed.fetch_benchmark_kline(start_date=start, end_date=as_of)
                    benchmark_valid, benchmark_ttl = self._benchmark_evidence(benchmark, as_of)
                with self._history_lock:
                    if generation != self._generation:
                        return
                    self._industry_refresh_needed = self._industry_refresh_required(symbols)
                    items = {**self._history_items, **data} if repair else data
                    if repair or seeded:
                        for sym, fetched_item in fetched.items():
                            previous = self._history_items.get(sym, {})
                            if not fetched_item.get("bars") and previous.get("bars"):
                                # Retain dated raw-close evidence for a holding's
                                # risk exit, while rejecting all new risk from
                                # this failed observation. Never copy old fresh.
                                failure = fetched_item.get("error") or "repair_returned_no_bars"
                                meta = dict(fetched_item.get("metadata", {}), fresh=False, error=failure)
                                items[sym] = dict(fetched_item, bars=previous["bars"],
                                                  name=previous.get("name", sym), metadata=meta, error=failure)
                    if repair:
                        # Queue attempted failures after untouched failures.
                        for sym in requested:
                            issue = self._history_errors.pop(sym, None)
                            if issue is not None:
                                self._history_errors[sym] = issue
                    self._publish_history(items, symbols, as_of, generation, industries, benchmark,
                                          benchmark_valid, benchmark_ttl, repair=repair)
                    if not synchronous:
                        now_mono = time.monotonic()
                        self._record_update_results(requested, now, as_of)
                        large = self._catchup_needed()
                        for sym in requested:
                            if sym in self._history_errors:
                                delay = (self.HOLDING_RETRY_SECONDS if sym in self.account.positions
                                         else 300 if self.history_cache.get(sym) else 3600)
                                if large:
                                    self._symbol_retry_due[sym] = now_mono+delay
                            else:
                                self._symbol_retry_due.pop(sym, None)
                        if large:
                            delay = self._source_retry_delay()
                            if requested and not any(sym in self._entry_eligible for sym in requested):
                                delay = max(delay, self.HOLDING_RETRY_SECONDS)
                            if self._conservative_update_window(now):
                                delay = max(delay, self.HOLDING_RETRY_SECONDS if any(
                                    sym in self.account.positions for sym in self._history_errors) else self.FAILED_RETRY_SECONDS)
                            self._next_failed_retry = now_mono+delay
                        self.data_status.update(next_failed_retry_seconds=max(0,round(self._next_failed_retry-now_mono)),
                                                refresh_batch_size=len(requested),
                                                holding_history_ready=all(sym in self._entry_eligible for sym in self.account.positions),
                                                source_health=self._source_health())
            except Exception as exc:
                log.exception("日线数据准备失败")
                with self._history_lock:
                    if generation != self._generation:
                        return
                    self._next_failed_retry = time.monotonic()+self.FAILED_RETRY_SECONDS
                    if repair or seeded:
                        self.data_status = dict(self.data_status, refreshing=False,
                                                refresh=dict(mode="failed_symbols" if repair else "cache_bootstrap", status="failed", error=str(exc)))
                    else:
                        self._entry_eligible = set()
                        self._benchmark_valid = False
                        self._history_date, self._published_generation = as_of, generation
                        self._history_valid_until = 0
                        self._history_revision = uuid.uuid4().hex
                        self.data_status = dict(status="failed", as_of=as_of, requested=len(symbols), loaded=0,
                                                usable_history=0, unavailable=len(symbols), refreshing=False, error=str(exc))
        if synchronous:
            load()
        else:
            self._history_thread = threading.Thread(target=load, name="daily-signal-data", daemon=True)
            self._history_thread.start()

    def _ranked_signals(self, as_of):
        signals = []
        if self._history_date != as_of or not self._new_risk_data_valid():
            return signals
        for sym, bars in self.history_cache.items():
            if sym not in self.watchlist or sym not in self._entry_eligible:
                continue
            code = sym[-6:]
            if not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
                continue
            name = getattr(self, "_stock_names", {}).get(sym, sym)
            risk_flag = bars[-1].get("is_st") if bars else None
            if (risk_flag is True or (risk_flag is not False and "ST" in name.upper())
                    or "退" in name or name.upper().startswith("PT")):
                continue
            signal = evaluate_momentum(bars, as_of, self.config)
            if signal and signal.get("raw_signal_price") and signal.get("adjustment_factor"):
                signals.append(dict(symbol=sym, code=code, name=name,
                                    current=signal["raw_signal_price"], **signal))
        signals.sort(key=lambda candidate: (-candidate["score"], candidate["symbol"]))
        return signals

    def preview_candidates(self, now=None):
        """Read-only top 50; no orders, account writes or plan-state changes."""
        as_of = last_completed_trading_day(now or dt.datetime.now(BEIJING))
        with self._history_lock:
            if self._history_date != as_of or not self._new_risk_data_valid():
                return []
            # Ranking was computed at publication. HTTP polling need not sort
            # the complete universe again; copies protect the cached preview.
            return [dict(candidate) for candidate in self.latest_candidates]

    def _plan(self, as_of, execution_day):
        new_risk_valid = self._new_risk_data_valid() and self._ranking_coverage() >= .95
        signals = self._ranked_signals(as_of)
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
        if rebalance and market_ok and new_risk_valid:
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
                                    target_shares=old.get("target_shares") if old else None,
                                    blocked_date=old.get("blocked_date") if old else None,
                                    blocked_reason=old.get("blocked_reason") if old else None)
        self.state["pending_entries"] = entries
        self.state["planned_as_of"] = as_of
        self.state["planned_revision"] = self._history_revision
        self.state["execution_day"] = execution_day
        if (rebalance and new_risk_valid
                and (not self.config.market_filter or self._benchmark_valid)):
            self.state["last_rebalance"] = as_of
            self.state["rebalance_as_of"] = as_of
        self.account.save()

    def _reject_entry(self, symbol, intent, execution_day, reason):
        # Unsafe prices cancel the rest of this day's intent, including partial
        # top-ups. A recovering quote or loader publication cannot revive it.
        if reason != "entry_unverified_prices":
            intent.update(blocked_date=execution_day, blocked_reason=reason)
        record = dict(symbol=symbol, name=intent["signal"].get("name", symbol),
                      date=execution_day, reason=reason)
        records = self.state["entry_rejections"]
        if record not in records:
            records.append(record)
            del records[:-20]
        self.data_status["entry_rejections"] = [dict(row) for row in records]

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
            if intent.get("blocked_date") == execution_day:
                continue
            if (intent.get("filled_shares", 0) > 0 and intent.get("target_shares") is not None
                    and intent["filled_shares"] >= intent["target_shares"]):
                continue  # Completed buys are not new orders or rejection events.
            if not self._new_risk_data_valid() or self._ranking_coverage() < .95 or sym not in self._entry_eligible:
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
            # A stale/malformed observation cannot permanently cancel a valid
            # plan. The broker independently validates the full book at fill.
            try:
                quote_time, current_time = _timestamp(q.get("time")), _timestamp(now)
                age = (current_time - quote_time).total_seconds()
                if (q.get("fresh") is False or quote_time.date() != current_time.date()
                        or not 0 <= age <= 120 or not is_trading_time(quote_time)):
                    continue
            except (ValueError, TypeError):
                continue
            reason = entry_guard_reason(intent["signal"], self.config, q.get("pre_close"),
                                        q.get("open"), q.get("current"), q.get("limit_down"))
            if reason:
                self._reject_entry(sym, intent, execution_day, reason)
                continue
            asks = q.get("asks", [])
            if not asks:
                continue
            price = asks[0].get("price", 0)
            if not price or not math.isfinite(price) or price <= 0:
                continue
            reason = entry_guard_reason(intent["signal"], self.config, q.get("pre_close"),
                                        q.get("open"), price, q.get("limit_down"))
            if reason:
                self._reject_entry(sym, intent, execution_day, reason)
                continue
            weight = target_weight(self.config, intent["signal"])
            if intent["target_shares"] is None or not intent["filled_shares"]:
                # An initially unaffordable lot must not freeze target=0 for
                # the whole day; recompute until the first actual fill.
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
                        limit_price=(q["pre_close"] * (1+self.config.max_entry_gap_up_pct/100)
                                     if self.config.max_entry_gap_up_pct else None),
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
