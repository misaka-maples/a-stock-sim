"""Offline live-pipeline regression tests for completed daily momentum signals."""
import copy
import datetime as dt
import json
import threading
from unittest.mock import Mock

import pytest

from daily_momentum import MomentumRotationProStrategy, sessions_between
from sim_trader import SimAccount
from trade_calendar import BEIJING, is_trading_day
from test_account_execution import NOW, clock, no_network


SYMBOL = "sh600000"


def completed_bars(as_of, trend=.02, count=130):
    dates, day = [], dt.date.fromisoformat(as_of)
    while len(dates) < count:
        if is_trading_day(day):
            dates.append(day.isoformat())
        day -= dt.timedelta(days=1)
    bars = []
    for i, date in enumerate(reversed(dates)):
        close = round(10 + i * trend, 2)
        bars.append(dict(date=date, open=round(close-.01, 2), close=close,
                         high=round(close+.03, 2), low=round(close-.03, 2),
                         raw_open=round(close-.01, 2), raw_close=close,
                         raw_high=round(close+.03, 2), raw_low=round(close-.03, 2),
                         raw_pre_close=round(close-trend, 2), adjustment_factor=1., volume=10000))
    return bars


class StubFeed:
    def __init__(self, overrides=None, complete=True):
        self.calls = []
        self.overrides = overrides or {}
        self.complete = complete
        self.metadata = {"idx_000300": {"fresh": True}}

    def fetch_universe_klines(self, symbols, start_date=None, end_date=None, **kwargs):
        self.calls.append(dict(symbols=list(symbols), start_date=start_date, end_date=end_date))
        return {s: dict(symbol=s, name="测试股票", bars=copy.deepcopy(
                    self.overrides.get(s, completed_bars(end_date))),
                    metadata=dict(raw_prices_available=True, coverage_complete=self.complete, fresh=True)) for s in symbols}

    def fetch_benchmark_kline(self, start_date=None, end_date=None):
        return completed_bars(end_date)


def market_quote(symbol=SYMBOL, now=NOW, current=12.60, pre_close=12.58, **changes):
    q = dict(symbol=symbol, name="测试股票", code=symbol[-6:], time=now.strftime("%Y%m%d%H%M%S"),
             current=current, pre_close=pre_close, open=current, fresh=True, volume_lots=10000,
             asks=[dict(price=current, volume=100)], bids=[dict(price=current, volume=100)])
    q.update(changes)
    return q


@pytest.fixture
def account(tmp_path, clock):
    return SimAccount(initial_cash=100000, save_path=str(tmp_path / "account.json"))


def prepared(account, symbols=None, feed=None, now=NOW, **config):
    config = {**dict(max_positions=3, max_stock_weight=.2, max_sector_weight=1.,
                    risk_per_trade=0, volatility_target=0, rebalance_every=1,
                    min_holding_days=0), **config}
    s = MomentumRotationProStrategy(account, symbols or [SYMBOL], feed=feed or StubFeed(),
                                    industry_map={}, **config)
    s.prepare(now=now, synchronous=True)
    return s


def seed_position(account, clock, shares=1500, price=12.60, buy_day="2026-09-18"):
    when = dt.datetime.combine(dt.date.fromisoformat(buy_day), dt.time(10), BEIJING)
    account.bind_execution_quotes({}, now=when)
    account.ledger_only = True
    assert account.execute_buy(SYMBOL, "测试股票", price, shares)["success"]
    account.ledger_only = False
    account.update_t1_available_shares(clock[0].date().isoformat())


def tick(strategy, quotes, clock, now=NOW):
    clock[0] = now
    strategy.account.update_market_prices(quotes)
    strategy.account.bind_execution_quotes(quotes, now=now)
    strategy.on_tick(quotes, now=now)


def test_completed_previous_close_signal_executes_next_session(account, clock):
    s = prepared(account)
    assert s.data_status["status"] == "ready"
    assert s.feed.calls[0]["end_date"] == "2026-09-21"
    tick(s, {SYMBOL: market_quote()}, clock)
    assert account.trades[-1]["time"] == "2026-09-22 10:00:00"
    assert "2026-09-21收盘" in account.trades[-1]["reason"]
    assert s.latest_candidates[0]["as_of"] == "2026-09-21"
    assert account.positions[SYMBOL]["available_shares"] == 0


def test_symbol_101_receives_history_and_can_rank_first(account, clock):
    symbols = [f"sh{600000 + i}" for i in range(101)]
    last = symbols[-1]
    feed = StubFeed({last: completed_bars("2026-09-21", trend=.08)})
    s = prepared(account, symbols=symbols, feed=feed)
    assert len(feed.calls[0]["symbols"]) == 101
    assert last in s.history_cache and s.data_status["loaded"] == 101
    close = s.history_cache[last][-1]["raw_close"]
    tick(s, {last: market_quote(last, current=close, pre_close=close)}, clock)
    assert s.latest_candidates[0]["symbol"] == last
    assert last in account.positions


def test_current_session_surge_cannot_change_dated_momentum_score(account, clock, tmp_path):
    a = prepared(account)
    other = SimAccount(initial_cash=100000, save_path=str(tmp_path / "other.json"))
    b = prepared(other)
    tick(a, {SYMBOL: market_quote(current=12.6)}, clock)
    tick(b, {SYMBOL: market_quote(current=13.5)}, clock)
    for field in ("as_of", "score", "ret_20", "ret_60", "ma20", "ma60", "ma120"):
        assert a.latest_candidates[0][field] == b.latest_candidates[0][field]


@pytest.mark.parametrize("changes", [dict(asks=[]), dict(time="20260921100000"), dict(volume_lots=0)])
def test_unexecutable_entry_keeps_intent_without_fabricated_trade(account, clock, changes):
    s = prepared(account)
    tick(s, {SYMBOL: market_quote(**changes)}, clock)
    assert not account.trades and not account.positions
    assert SYMBOL in s.state["pending_entries"]
    assert s.state["pending_entries"][SYMBOL]["filled_shares"] == 0


def test_partial_entry_retries_new_snapshot_and_survives_restart(account, clock):
    s = prepared(account)
    quotes = {SYMBOL: market_quote()}
    tick(s, quotes, clock)
    first = s.state["pending_entries"][SYMBOL]["filled_shares"]
    assert first == 500 and account.orders[-1]["status"] == "PARTIALLY_FILLED"
    # An identical snapshot cannot be consumed twice.
    tick(s, quotes, clock)
    assert len(account.trades) == 1
    restored = SimAccount(save_path=str(account.save_path))
    r = prepared(restored)
    assert r.state is restored.strategy_state[r.name]
    assert r.state["pending_entries"][SYMBOL]["filled_shares"] == 500
    later = NOW + dt.timedelta(seconds=3)
    tick(r, {SYMBOL: market_quote(now=later)}, clock, now=later)
    assert r.state["pending_entries"][SYMBOL]["filled_shares"] == 1000
    assert restored.positions[SYMBOL]["available_shares"] == 0
    last = later + dt.timedelta(seconds=3)
    tick(r, {SYMBOL: market_quote(now=last)}, clock, now=last)
    intent = r.state["pending_entries"][SYMBOL]
    assert intent["filled_shares"] == intent["target_shares"] == 1500
    reloaded_again = SimAccount(save_path=str(account.save_path))
    assert reloaded_again.strategy_state[r.name]["pending_entries"][SYMBOL]["filled_shares"] == 1500


def test_previous_close_stop_loss_executes_at_next_session_market_price(account, clock):
    seed_position(account, clock, shares=1500, price=14.)
    s = prepared(account)
    # Yesterday closed at 12.58 and breached the cost stop; today's rebound
    # does not erase the pending daily exit. Actual order price is today's bid.
    tick(s, {SYMBOL: market_quote(current=13.4)}, clock)
    assert account.trades[-1]["side"] == "SELL"
    assert account.trades[-1]["price"] == 13.4
    assert "2026-09-21收盘硬止损信号" in account.trades[-1]["reason"]
    assert account.positions[SYMBOL]["total_shares"] == 1000
    assert SYMBOL not in s.state["cooldown"]
    for seconds in (3, 6):
        later = NOW + dt.timedelta(seconds=seconds)
        tick(s, {SYMBOL: market_quote(now=later, current=13.4)}, clock, now=later)
    assert SYMBOL not in account.positions
    assert s.state["cooldown"][SYMBOL] == "2026-09-22"


def test_intraday_drop_without_completed_close_exit_cannot_trigger_daily_stop(account, clock):
    seed_position(account, clock, price=12.58)
    s = prepared(account)
    tick(s, {SYMBOL: market_quote(current=11.4)}, clock)
    assert [t["side"] for t in account.trades] == ["BUY"]
    assert not s.state["pending_exits"]


def test_blocked_daily_exit_and_partial_sale_preserve_order_intent(account, clock):
    seed_position(account, clock, shares=1500, price=14.)
    s = prepared(account)
    tick(s, {SYMBOL: market_quote(bids=[])}, clock)
    assert len(account.trades) == 1 and SYMBOL in s.state["pending_exits"]
    later = NOW + dt.timedelta(seconds=3)
    tick(s, {SYMBOL: market_quote(now=later)}, clock, now=later)
    assert account.trades[-1]["side"] == "SELL"
    assert account.trades[-1]["shares"] == 500
    assert account.positions[SYMBOL]["total_shares"] == 1000
    restored = SimAccount(save_path=str(account.save_path))
    assert SYMBOL in restored.strategy_state[s.name]["pending_exits"]


def test_pending_risk_exit_survives_new_day_with_missing_history(account, clock):
    seed_position(account, clock, shares=1500, price=14.)
    s = prepared(account)
    tick(s, {SYMBOL: market_quote(bids=[])}, clock)
    assert SYMBOL in s.state["pending_exits"]
    tomorrow = NOW + dt.timedelta(days=1)
    # An already authorized risk exit must still be retried when the next
    # nightly download fails; missing data cannot cancel the old order intent.
    s.feed.overrides[SYMBOL] = completed_bars("2026-09-21")
    clock[0] = tomorrow
    s.prepare(now=tomorrow, synchronous=True)
    tick(s, {SYMBOL: market_quote(now=tomorrow)}, clock, now=tomorrow)
    assert account.trades[-1]["side"] == "SELL"
    assert "2026-09-21收盘" in account.trades[-1]["reason"]
    assert SYMBOL in s.state["pending_exits"]  # remaining partial liquidation


@pytest.mark.parametrize("last_rebalance,should_exit", [("2026-09-18", False), ("2026-09-11", True)])
def test_rank_rotation_waits_for_scheduled_rebalance(account, clock, last_rebalance, should_exit):
    seed_position(account, clock, shares=100, price=10.)
    # Flat prices fail the entry ranking but do not breach MA20 or hard stop.
    feed = StubFeed({SYMBOL: completed_bars("2026-09-21", trend=0)})
    s = prepared(account, symbols=[SYMBOL, "sh600001"], feed=feed,
                 max_positions=1, rank_buffer=0, rebalance_every=5)
    s.state["last_rebalance"] = last_rebalance
    tick(s, {SYMBOL: market_quote(current=10, pre_close=10)}, clock)
    assert (account.trades[-1]["side"] == "SELL") is should_exit
    if should_exit:
        assert "排名跌出缓冲区" in account.trades[-1]["reason"]


def test_three_session_cooldown_counts_exchange_sessions(account, clock):
    sold = "2026-09-21"
    # 9/25-9/27 are exchange holidays/weekend, so the fourth session is 9/28.
    assert sessions_between(sold, "2026-09-24") == 3
    assert sessions_between(sold, "2026-09-28") == 4
    s = prepared(account)
    s.state["cooldown"][SYMBOL] = sold
    tick(s, {SYMBOL: market_quote()}, clock)
    assert not account.trades
    fourth = dt.datetime(2026, 9, 28, 10, tzinfo=BEIJING)
    clock[0] = fourth
    s.prepare(now=fourth, synchronous=True)
    tick(s, {SYMBOL: market_quote(now=fourth)}, clock, now=fourth)
    assert account.trades and account.trades[-1]["time"].startswith("2026-09-28")


def test_old_warmup_gap_does_not_discard_usable_completed_signal(account, clock):
    s = prepared(account, feed=StubFeed(complete=False))
    tick(s, {SYMBOL: market_quote()}, clock)
    assert account.trades and s.data_status["loaded"] == 1
    assert s.data_status["coverage_gap_count"] == 1


def test_future_persisted_plan_cannot_suppress_current_session_forever(account, clock):
    account.strategy_state["Momentum_Rotation_Pro"] = dict(
        planned_as_of="2030-01-01", execution_day="2030-01-02", last_rebalance="2030-01-01",
        cooldown={SYMBOL: "2030-01-01"}, pending_exits={}, pending_entries={})
    account.save()
    restored = SimAccount(save_path=str(account.save_path))
    s = prepared(restored)
    tick(s, {SYMBOL: market_quote()}, clock)
    assert s.state["planned_as_of"] == "2026-09-21"
    assert restored.trades, "future cooldown/rebalance dates must be migrated or invalidated"


def test_missing_history_does_not_fabricate_exit_for_existing_position(account, clock):
    seed_position(account, clock, shares=100, price=14.)
    stale = completed_bars("2026-09-18")
    s = prepared(account, feed=StubFeed({SYMBOL: stale}))
    tick(s, {SYMBOL: market_quote()}, clock)
    assert [t["side"] for t in account.trades] == ["BUY"]
    assert not s.state["pending_exits"]


def test_same_day_history_recovery_rechecks_an_existing_stop_loss(account, clock):
    seed_position(account, clock, shares=100, price=14.)
    feed = StubFeed({SYMBOL: completed_bars("2026-09-18", trend=0)}, complete=False)
    s = prepared(account, feed=feed)
    tick(s, {SYMBOL: market_quote(current=10., pre_close=10.)}, clock)
    assert s.state["planned_as_of"] == "2026-09-21"
    assert not s.state["pending_exits"]
    first_revision = s.state["planned_revision"]
    assert [t["side"] for t in account.trades] == ["BUY"]

    # Recovery changes the data revision, rather than the signal/execution day.
    feed.complete = True
    feed.overrides[SYMBOL] = completed_bars("2026-09-21", trend=0)
    recovered_at = NOW + dt.timedelta(minutes=1)
    s.prepare(now=recovered_at, synchronous=True)
    assert s._history_revision != first_revision
    tick(s, {SYMBOL: market_quote(now=recovered_at, current=10., pre_close=10.)},
         clock, now=recovered_at)
    assert s.state["planned_as_of"] == "2026-09-21"
    assert s.state["planned_revision"] == s._history_revision
    assert account.trades[-1]["side"] == "SELL"
    assert "2026-09-21收盘硬止损" in account.trades[-1]["reason"]
    assert SYMBOL not in account.positions


@pytest.mark.parametrize("fresh,error", [(False, None), (True, "provider_failed"), (None, None)])
def test_stale_or_failed_or_unverified_cache_cannot_authorize_new_entries(account, clock, fresh, error):
    class UnverifiedFeed(StubFeed):
        def fetch_universe_klines(self, *args, **kwargs):
            output = super().fetch_universe_klines(*args, **kwargs)
            for item in output.values():
                item["metadata"]["fresh"] = fresh
                item["error"] = error
            return output
    s = prepared(account, feed=UnverifiedFeed())
    tick(s, {SYMBOL: market_quote()}, clock)
    assert not account.trades and not s.state["pending_entries"]
    assert s.data_status["loaded"] == 0 and s.data_status["errors"]
    assert s.history_cache[SYMBOL], "diagnostic/risk history must survive entry rejection"


def test_short_raw_only_history_can_authorize_hard_stop_but_not_ma_or_new_entry(account, clock):
    seed_position(account, clock, shares=100, price=14.)
    bars = [dict(date="2026-09-21", raw_close=10.)]
    s = prepared(account, feed=StubFeed({SYMBOL: bars}, complete=False))
    tick(s, {SYMBOL: market_quote(current=10., pre_close=10.)}, clock)
    assert account.trades[-1]["side"] == "SELL"
    assert "硬止损" in account.trades[-1]["reason"]
    assert not s.state["pending_entries"] and not account.positions
    from strategy_rules import holding_signal, exit_reason
    signal = holding_signal(bars, "2026-09-21")
    assert signal["ma20"] is None
    assert exit_reason(10., 9., signal, s.config) is None


def test_ready_cache_prepares_new_manual_holding_outside_watchlist(account, clock):
    s = prepared(account)
    outside = "sh600001"
    account.ledger_only = True
    assert account.execute_buy(outside, "人工持仓", 10., 100)["success"]
    account.ledger_only = False
    s.prepare(now=NOW, synchronous=True)
    assert outside in s.feed.calls[-1]["symbols"]
    assert outside in s.history_cache


def test_enabling_market_filter_fetches_benchmark_without_waiting_a_day(account, clock):
    feed = StubFeed()
    feed.fetch_benchmark_kline = Mock(return_value=completed_bars("2026-09-21"))
    s = prepared(account, feed=feed)
    s.set_config({"market_filter": True})
    s.prepare(now=NOW, synchronous=True)
    assert feed.fetch_benchmark_kline.call_count == 1
    tick(s, {SYMBOL: market_quote()}, clock)
    assert account.trades


def test_configuration_change_invalidates_inflight_publication(account, clock):
    started, release = threading.Event(), threading.Event()
    class BlockingFeed(StubFeed):
        def fetch_universe_klines(self, *args, **kwargs):
            if not self.calls:
                started.set()
                assert release.wait(3)
            return super().fetch_universe_klines(*args, **kwargs)
    feed = BlockingFeed()
    feed.fetch_benchmark_kline = Mock(return_value=completed_bars("2026-09-21"))
    s = MomentumRotationProStrategy(account, [SYMBOL], feed=feed, industry_map={})
    s.prepare(now=NOW)
    assert started.wait(1)
    try:
        s.set_config({"market_filter": True})
        s.set_watchlist(["SH600001", "600001"])
    finally:
        release.set()
        s._history_thread.join(3)
    assert not s._history_thread.is_alive()
    assert s._history_date is None and s._published_generation is None
    s.prepare(now=NOW, synchronous=True)
    assert set(s.history_cache) == {"sh600001"}
    assert feed.fetch_benchmark_kline.call_count == 1
    assert s._published_generation == s._generation


def test_revised_same_day_rank_updates_partial_intent_signal_and_order(account, clock):
    other = "sh600001"
    feed = StubFeed({other: completed_bars("2026-09-18")})
    s = prepared(account, symbols=[SYMBOL, other], feed=feed)
    tick(s, {SYMBOL: market_quote()}, clock)
    assert s.state["pending_entries"][SYMBOL]["filled_shares"] == 500
    old_signal = s.state["pending_entries"][SYMBOL]["signal"]["score"]
    feed.overrides[SYMBOL] = completed_bars("2026-09-21", trend=.01)
    feed.overrides[other] = completed_bars("2026-09-21", trend=.04)
    s.prepare(now=NOW, synchronous=True)
    with s._history_lock:
        s._plan("2026-09-21", "2026-09-22")
    assert list(s.state["pending_entries"]) == [other, SYMBOL]
    partial = s.state["pending_entries"][SYMBOL]
    assert partial["filled_shares"] == 500
    assert partial["signal"]["score"] != old_signal
    assert partial["target_shares"] == 1500


def test_trade_callbacks_see_atomically_committed_partial_intent_and_cooldown(account, clock):
    s = prepared(account)
    observed = []
    def on_buy(trade, acc):
        stored = json.loads(acc.save_path.read_text())["strategy_state"][s.name]
        observed.append(stored["pending_entries"][SYMBOL]["filled_shares"])
    account.register_trade_callback(on_buy)
    tick(s, {SYMBOL: market_quote()}, clock)
    assert observed == [500]
    account.trade_callbacks.clear()
    # Yesterday's close now breaches the cost stop; clear the full partial
    # position and ensure callback readers already see its cooldown.
    account.positions[SYMBOL]["cost_price"] = 14.
    s.state.pop("planned_as_of", None)
    def on_sell(trade, acc):
        stored = json.loads(acc.save_path.read_text())["strategy_state"][s.name]
        observed.append(stored["cooldown"][SYMBOL])
        assert SYMBOL not in stored["pending_exits"]
    account.register_trade_callback(on_sell)
    later = NOW + dt.timedelta(seconds=3)
    # The holding existed before the signal close in this fixture.
    account.positions[SYMBOL]["buy_lots"][0]["date"] = "2026-09-18"
    tick(s, {SYMBOL: market_quote(now=later)}, clock, now=later)
    assert observed == [500, "2026-09-22"]


def test_provider_industry_refresh_does_not_get_overridden_by_previous_auto_value(account):
    feed = StubFeed()
    feed.fetch_industry_map = Mock(side_effect=[{SYMBOL: "原行业"}, {SYMBOL: "新行业"}])
    s = prepared(account, feed=feed)
    s.prepare(now=NOW+dt.timedelta(days=1), synchronous=True)
    assert s.industry_map[SYMBOL] == "新行业"


def test_ready_cache_expiry_triggers_reload_and_uses_source_age(account):
    import time
    class AgedFeed(StubFeed):
        def fetch_universe_klines(self, *args, **kwargs):
            output = super().fetch_universe_klines(*args, **kwargs)
            for item in output.values():
                item["metadata"]["fetched_at"] = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=23)).isoformat()
            return output
    feed = AgedFeed()
    s = prepared(account, feed=feed)
    assert 3500 < s._history_valid_until-time.monotonic() <= 3600
    s._history_valid_until = time.monotonic()-1
    s.prepare(now=NOW, synchronous=True)
    assert len(feed.calls) == 2


def test_global_fetch_failure_next_day_preserves_pending_risk_exit(account, clock):
    seed_position(account, clock, shares=100, price=14.)
    s = prepared(account)
    tick(s, {SYMBOL: market_quote(bids=[])}, clock)
    assert SYMBOL in s.state["pending_exits"]
    s.feed.fetch_universe_klines = Mock(side_effect=RuntimeError("offline provider outage"))
    tomorrow = NOW+dt.timedelta(days=1)
    s.prepare(now=tomorrow, synchronous=True)
    assert s.data_status["status"] == "failed"
    tick(s, {SYMBOL: market_quote(now=tomorrow)}, clock, now=tomorrow)
    assert account.trades[-1]["side"] == "SELL"
    assert not account.positions and not s.state["pending_entries"]


def test_expired_previous_plan_cannot_buy_while_async_reload_is_inflight(account, clock):
    import time
    started, release = threading.Event(), threading.Event()
    class BlockingReload(StubFeed):
        def fetch_universe_klines(self, *args, **kwargs):
            if self.calls:
                started.set()
                assert release.wait(3)
            return super().fetch_universe_klines(*args, **kwargs)
    s = prepared(account, feed=BlockingReload())
    tick(s, {SYMBOL: market_quote()}, clock)
    assert len(account.trades) == 1
    s._history_valid_until = time.monotonic()-1
    s._last_attempt = 0
    later = NOW+dt.timedelta(seconds=3)
    try:
        tick(s, {SYMBOL: market_quote(now=later)}, clock, now=later)
        assert started.wait(1)
        assert len(account.trades) == 1
    finally:
        release.set()
        s._history_thread.join(3)


def test_market_filter_cannot_use_expired_benchmark_fallback(account, clock):
    feed = StubFeed()
    feed.metadata["idx_000300"] = {"fresh": False, "error": "provider_failed"}
    s = prepared(account, feed=feed, market_filter=True)
    tick(s, {SYMBOL: market_quote()}, clock)
    assert not account.trades and not s.state["pending_entries"]
    assert any(error["symbol"] == "_benchmark" for error in s.data_status["errors"])


def test_valid_partial_snapshot_is_reused_without_five_minute_full_reload(account, clock, monkeypatch):
    current = [1000.]
    monkeypatch.setattr("daily_momentum.time.monotonic", lambda: current[0])
    feed = StubFeed({"sh600001": []})
    s = prepared(account, symbols=[SYMBOL, "sh600001"], feed=feed)
    generation = s._generation
    current[0] += 301
    s.prepare(now=NOW)
    assert len(feed.calls) == 1
    assert s._generation == generation and s._new_risk_data_valid()
    assert s.data_status["loaded"] == s.data_status["usable_history"] == 1
    assert s.data_status["unavailable_reason_counts"] == {"no_cache": 1}


def test_failed_symbol_repair_preserves_available_snapshot_and_counts_inflight(account, clock):
    started, release = threading.Event(), threading.Event()
    unavailable = "sh600001"
    class BlockingRepair(StubFeed):
        def fetch_universe_klines(self, symbols, **kwargs):
            if self.calls:
                assert symbols == [unavailable], "a repair must not redownload good names"
                started.set()
                assert release.wait(3)
            return super().fetch_universe_klines(symbols, **kwargs)
    feed = BlockingRepair({unavailable: []})
    s = prepared(account, symbols=[SYMBOL, unavailable], feed=feed)
    generation = s._generation
    s._next_failed_retry = 0
    feed.overrides[unavailable] = completed_bars("2026-09-21", trend=.04)
    try:
        s.prepare(now=NOW)
        assert started.wait(1)
        assert s.data_status["refreshing"] and s.data_status["usable_history"] == 1
        assert s.data_status["status"] == "partial" and s._new_risk_data_valid()
        assert s._generation == generation
        assert s.preview_candidates(NOW)
    finally:
        release.set()
        s._history_thread.join(3)
    assert s.data_status["usable_history"] == 2 and s.data_status["status"] == "ready"
    assert s._generation == generation
    assert s.preview_candidates(NOW)[0]["symbol"] == unavailable
    assert not s.state.get("planned_as_of") and not account.trades


def test_failed_repair_timer_starts_after_completion_not_before_slow_download(account, monkeypatch):
    current = [1000.]
    monkeypatch.setattr("daily_momentum.time.monotonic", lambda: current[0])
    class SlowRepair(StubFeed):
        def fetch_universe_klines(self, *args, **kwargs):
            if self.calls:
                current[0] += 401
            return super().fetch_universe_klines(*args, **kwargs)
    feed = SlowRepair({"sh600001": []})
    s = prepared(account, symbols=[SYMBOL, "sh600001"], feed=feed)
    current[0] = s._next_failed_retry+1
    s.prepare(now=NOW)
    s._history_thread.join(3)
    assert len(feed.calls) == 2
    assert s._next_failed_retry == current[0]+s.FAILED_RETRY_SECONDS
    s.prepare(now=NOW)
    assert len(feed.calls) == 2 and s._new_risk_data_valid()


def test_premarket_preview_is_read_only_and_reports_all_signals_beyond_top_50(account):
    import copy
    symbols = ["sh"+str(600000+i) for i in range(55)]
    s = prepared(account, symbols=symbols)
    before_state, before_file = copy.deepcopy(account.strategy_state), account.save_path.read_bytes()
    premarket = NOW.replace(hour=8, minute=53)
    preview = s.preview_candidates(premarket)
    assert len(preview) == 50
    assert s.data_status["candidate_count"] == 55 and s.data_status["preview_count"] == 50
    assert account.strategy_state == before_state and account.save_path.read_bytes() == before_file
    assert not account.trades and not account.orders
    preview[0]["score"] = -999
    assert s.preview_candidates(premarket)[0]["score"] != -999


def test_short_history_is_retained_for_risk_but_not_counted_as_entry_usable(account):
    feed = StubFeed({SYMBOL: completed_bars("2026-09-21", count=124)})
    s = prepared(account, feed=feed)
    assert s.data_status["latest_raw_history"] == 1
    assert s.data_status["retained_history"] == 1
    assert s.data_status["usable_history"] == 0
    assert s.data_status["unavailable_reason_counts"] == {"short": 1}
    assert not s.preview_candidates(NOW) and s.history_cache[SYMBOL]


def test_failed_repair_is_bounded_and_rotates_names(account):
    symbols = ["sh"+str(600000+i) for i in range(30)]
    feed = StubFeed({sym: [] for sym in symbols[1:]})
    s = prepared(account, symbols=symbols, feed=feed)
    s._next_failed_retry = 0
    s.prepare(now=NOW)
    s._history_thread.join(3)
    assert len(feed.calls[-1]["symbols"]) == 24
    first = feed.calls[-1]["symbols"]
    s._next_failed_retry = 0
    s.prepare(now=NOW)
    s._history_thread.join(3)
    assert feed.calls[-1]["symbols"][0] == symbols[25]
    assert feed.calls[-1]["symbols"] != first
    assert s.data_status["usable_history"] == 1 and s._new_risk_data_valid()


def test_empty_failed_symbol_repair_preserves_raw_holding_stop_without_new_risk(account, clock):
    seed_position(account, clock, shares=100, price=14.)
    other = "sh600001"
    class StaleHoldingFeed(StubFeed):
        def fetch_universe_klines(self, *args, **kwargs):
            output = super().fetch_universe_klines(*args, **kwargs)
            if SYMBOL in output:
                output[SYMBOL]["metadata"]["fresh"] = False
            return output
    feed = StaleHoldingFeed()
    s = prepared(account, symbols=[SYMBOL, other], feed=feed)
    feed.overrides[SYMBOL] = []
    s._next_failed_retry = 0
    s.prepare(now=NOW)
    s._history_thread.join(3)
    assert s.history_cache[SYMBOL] and SYMBOL not in s._entry_eligible
    assert not s._history_items[SYMBOL]["metadata"]["fresh"]
    assert s._history_items[SYMBOL]["error"] == "repair_returned_no_bars"
    tick(s, {SYMBOL: market_quote()}, clock)
    assert account.trades[-1]["side"] == "SELL" and "硬止损" in account.trades[-1]["reason"]
    assert SYMBOL not in account.positions


def test_future_benchmark_fetch_timestamp_cannot_authorize_market_filter(account, clock):
    feed = StubFeed()
    feed.metadata["idx_000300"] = dict(fresh=True, fetched_at=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).isoformat())
    s = prepared(account, feed=feed, market_filter=True)
    assert not s._benchmark_valid
    tick(s, {SYMBOL: market_quote()}, clock)
    assert not account.trades


def cache_bootstrap_feed(tmp_path, stale=False):
    from history_data import HistoricalDataFeed
    feed = HistoricalDataFeed(tmp_path / "bootstrap_cache")
    feed.fetch_industry_map = Mock(return_value={})
    start = (dt.date(2026, 9, 21)-dt.timedelta(days=400)).isoformat()
    feed._download = Mock(return_value=("测试股份", completed_bars("2026-09-21"), "offline", []))
    feed.fetch_stock_kline(SYMBOL, start, "2026-09-21", use_cache=False)
    if stale:
        path = feed._get_cache_path(SYMBOL)
        item = json.loads(path.read_text())
        item["metadata"]["fetched_at"] = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=2)).isoformat()
        path.write_text(json.dumps(item))
    return feed


def test_startup_cache_bootstrap_publishes_candidates_before_blocked_provider_finishes(account, tmp_path):
    import copy
    missing = "sh600001"
    feed = cache_bootstrap_feed(tmp_path)
    started, release = threading.Event(), threading.Event()
    def blocked(symbols, **kwargs):
        assert symbols == [missing], "verified cached names should not be redownloaded"
        started.set()
        assert release.wait(3)
        return {missing: dict(bars=[], metadata=dict(fresh=False), error="offline_timeout")}
    feed.fetch_universe_klines = Mock(side_effect=blocked)
    s = MomentumRotationProStrategy(account, [SYMBOL, missing], feed=feed, industry_map={})
    state, stored = copy.deepcopy(account.strategy_state), account.save_path.read_bytes()
    premarket = NOW.replace(hour=8, minute=53)
    try:
        s.prepare(now=premarket)
        assert started.wait(1)
        assert s._history_thread.is_alive()
        assert s.data_status["bootstrap"] and s.data_status["refreshing"]
        assert s.data_status["usable_history"] == 1 and s.data_status["status"] == "partial"
        assert s.preview_candidates(premarket)[0]["symbol"] == SYMBOL
        assert account.strategy_state == state and account.save_path.read_bytes() == stored
        assert not account.trades and not account.orders
    finally:
        release.set()
        s._history_thread.join(3)
    assert s.data_status["usable_history"] == 1 and s.preview_candidates(premarket)


def test_stale_startup_cache_is_retained_but_cannot_seed_entry_candidates(account, tmp_path):
    feed = cache_bootstrap_feed(tmp_path, stale=True)
    started, release = threading.Event(), threading.Event()
    def blocked(symbols, **kwargs):
        started.set()
        assert release.wait(3)
        return {SYMBOL: dict(bars=[], metadata=dict(fresh=False), error="offline_timeout")}
    feed.fetch_universe_klines = Mock(side_effect=blocked)
    s = MomentumRotationProStrategy(account, [SYMBOL], feed=feed, industry_map={})
    try:
        s.prepare(now=NOW)
        assert started.wait(1)
        assert s.history_cache[SYMBOL] and s.data_status["usable_history"] == 0
        assert not s.preview_candidates(NOW) and not s._new_risk_data_valid()
    finally:
        release.set()
        s._history_thread.join(3)
    assert s.history_cache[SYMBOL] and not s._entry_eligible


def test_empty_initial_provider_result_preserves_cached_holding_risk_evidence(account, clock, tmp_path):
    seed_position(account, clock, shares=100, price=14.)
    feed = cache_bootstrap_feed(tmp_path, stale=True)
    feed.fetch_universe_klines = Mock(return_value={SYMBOL: dict(bars=[], metadata=dict(fresh=False), error="offline_timeout")})
    s = prepared(account, feed=feed)
    assert s.history_cache[SYMBOL] and SYMBOL not in s._entry_eligible
    assert s._history_items[SYMBOL]["metadata"]["fresh"] is False
    tick(s, {SYMBOL: market_quote()}, clock)
    assert account.trades[-1]["side"] == "SELL" and "硬止损" in account.trades[-1]["reason"]
    assert SYMBOL not in account.positions


def test_all_history_cached_with_missing_industry_refreshes_only_industry_in_background(account, tmp_path):
    feed = cache_bootstrap_feed(tmp_path)
    started, release = threading.Event(), threading.Event()
    def industry_refresh():
        started.set()
        assert release.wait(3)
        feed.metadata["industry_map"] = dict(fresh=True, source="offline_test")
        return {SYMBOL: "金融"}
    feed.fetch_industry_map = Mock(side_effect=industry_refresh)
    feed.fetch_universe_klines = Mock(side_effect=AssertionError("all history already verified in cache"))
    s = MomentumRotationProStrategy(account, [SYMBOL], feed=feed, industry_map={})
    premarket = NOW.replace(hour=8, minute=53)
    try:
        s.prepare(now=premarket)
        assert started.wait(1)
        assert s.data_status["usable_history"] == 1 and s.data_status["refreshing"]
        assert s._new_risk_data_valid() and s.preview_candidates(premarket)
    finally:
        release.set()
        s._history_thread.join(3)
    assert s.industry_map[SYMBOL] == "金融"
    assert not s._industry_refresh_needed
    feed.fetch_industry_map.assert_called_once()
    feed.fetch_universe_klines.assert_not_called()
    s.prepare(now=premarket)
    feed.fetch_industry_map.assert_called_once()
