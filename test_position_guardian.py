#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import copy
import datetime as dt
import math
import pytest
from types import SimpleNamespace

from test_account_execution import NOW, clock, no_network
from test_daily_momentum import account, seed_position, market_quote, prepared, SYMBOL, tick
from position_guardian import (
    update_position_monitors,
    compute_live_ma20,
    evaluate_position_realtime_rules,
    _valid_price,
)
from strategy_rules import MomentumConfig
from daily_momentum import DailyMomentumStrategy, BEIJING


def test_valid_price_helper():
    assert _valid_price(10.5) == 10.5
    assert _valid_price("15.2") == 15.2
    assert _valid_price(0) is None
    assert _valid_price(-5) is None
    assert _valid_price(float("nan")) is None
    assert _valid_price(float("inf")) is None
    assert _valid_price(True) is None
    assert _valid_price(None) is None
    assert _valid_price("invalid") is None


def test_update_position_monitors():
    now = dt.datetime(2026, 9, 22, 10, 30, tzinfo=BEIJING)
    positions = {
        "sh600000": {
            "symbol": "sh600000",
            "cost_price": 10.0,
            "total_shares": 1000,
            "available_shares": 1000,
            "buy_lots": [{"date": "2026-09-20", "price": 10.0, "shares": 1000}]
        }
    }
    quotes = {
        "sh600000": {
            "current": 10.5,
            "high": 10.8,
            "low": 9.9,
            "pre_close": 10.0,
            "limit_up": 11.0,
            "time": "20260922103000"
        }
    }
    history_cache = {
        "sh600000": [
            {"date": "2026-09-21", "high": 10.6, "close": 10.2}
        ]
    }
    monitors = {}
    update_position_monitors(positions, quotes, history_cache, monitors, now=now)

    mon = monitors["sh600000"]
    # Peak price should be max(cost 10.0, quote_high 10.8, hist_high 10.6) = 10.8
    assert mon["peak_price"] == 10.8
    assert mon["peak_profit_pct"] == 8.0  # (10.8 / 10.0 - 1) * 100 = +8%
    assert mon["current_profit_pct"] == 5.0  # (10.5 / 10.0 - 1) * 100 = +5%
    # Drawdown from peak: (10.5 / 10.8 - 1) * 100 = -2.78%
    assert mon["drawdown_from_peak_pct"] == -2.78
    assert mon["is_locked_limit_up"] is False

    # Graceful handling of NaN
    bad_quotes = {"sh600000": {"current": float("nan"), "high": float("nan")}}
    update_position_monitors(positions, bad_quotes, history_cache, monitors, now=now)
    assert math.isfinite(monitors["sh600000"]["peak_price"])
    assert math.isfinite(monitors["sh600000"]["current_profit_pct"])


def test_compute_live_ma20():
    bars = [{"date": f"2026-08-{i:02d}", "close": 10.0} for i in range(1, 20)]
    cache = {"sh600000": bars}

    # 19 bars with close 10.0, plus current live price 10.0 -> MA20 = 10.0
    ma20 = compute_live_ma20("sh600000", 10.0, cache)
    assert ma20 == 10.0

    # Current price 12.0 -> (19 * 10 + 12) / 20 = 202 / 20 = 10.1
    ma20_up = compute_live_ma20("sh600000", 12.0, cache)
    assert ma20_up == 10.1

    # Less than 19 bars -> None
    short_cache = {"sh600000": bars[:15]}
    assert compute_live_ma20("sh600000", 10.0, short_cache) is None


def test_trailing_stop_rule():
    config = MomentumConfig(
        trailing_stop_enabled=True,
        trailing_stop_activation_pct=4.0,
        trailing_stop_drop_pct=2.0
    )
    pos = {"cost_price": 10.0, "total_shares": 1000, "available_shares": 1000}
    quote = {"current": 10.3}
    monitor = {
        "peak_price": 10.6,          # +6% peak
        "peak_profit_pct": 6.0,
        "is_locked_limit_up": False
    }
    # Trailing stop line: 10.6 * (1 - 0.02) = 10.388 -> 10.39
    # Current price 10.3 <= 10.39, while 10.3 > 10.0 (still profit)
    reason, info = evaluate_position_realtime_rules(
        "sh600000", pos, quote, {}, config, monitor,
        validated_price=10.3, is_past_buy=True
    )
    assert reason is not None
    assert "盘中动态跟踪止盈信号" in reason
    assert info["status"] == "triggered_trailing_stop"
    assert info["trailing_stop_active"] is True


def test_breakeven_rule():
    config = MomentumConfig(
        breakeven_enabled=True,
        breakeven_activation_pct=2.5,
        breakeven_buffer_pct=0.3
    )
    pos = {"cost_price": 10.0, "total_shares": 1000, "available_shares": 1000}
    quote = {"current": 10.02}
    monitor = {
        "peak_price": 10.3,          # +3.0% peak (>= 2.5% activation)
        "peak_profit_pct": 3.0,
        "is_locked_limit_up": False
    }
    # Breakeven stop line: 10.0 * (1 + 0.003) = 10.03
    # Current price 10.02 <= 10.03
    reason, info = evaluate_position_realtime_rules(
        "sh600000", pos, quote, {}, config, monitor,
        validated_price=10.02, is_past_buy=True
    )
    assert reason is not None
    assert "盘中保本止损防守信号" in reason
    assert info["status"] == "triggered_breakeven"


def test_ma20_breakdown_rule():
    config = MomentumConfig(realtime_ma20_exit=True)
    pos = {"cost_price": 10.0, "total_shares": 1000, "available_shares": 1000}
    # Live MA20 will be 10.0
    bars = [{"date": f"2026-08-{i:02d}", "close": 10.0} for i in range(1, 20)]
    cache = {"sh600000": bars}

    # Current price 9.88 < 10.0 * 0.99 = 9.90
    quote = {"current": 9.88}
    monitor = {"peak_price": 10.1, "peak_profit_pct": 1.0, "is_locked_limit_up": False}

    reason, info = evaluate_position_realtime_rules(
        "sh600000", pos, quote, cache, config, monitor,
        validated_price=9.88, is_past_buy=True
    )
    assert reason is not None
    assert "盘中跌破MA20避险信号" in reason
    assert info["status"] == "triggered_ma20_breakdown"


def test_limit_up_protection():
    config = MomentumConfig(protect_limit_up=True)
    pos = {"cost_price": 10.0, "total_shares": 1000, "available_shares": 1000}
    quote = {"current": 11.0, "limit_up": 11.0}
    monitor = {
        "peak_price": 11.0,
        "peak_profit_pct": 10.0,
        "is_locked_limit_up": True
    }
    reason, info = evaluate_position_realtime_rules(
        "sh600000", pos, quote, {}, config, monitor,
        validated_price=11.0, is_past_buy=True
    )
    # Even if trailing stop conditions might be checked, limit-up overrides and protects
    assert reason is None
    assert info["status"] == "limit_up_locked"
    assert "封死涨停板" in info["summary"]


def test_t1_lock_protection():
    config = MomentumConfig(
        trailing_stop_enabled=True,
        trailing_stop_activation_pct=2.0,
        trailing_stop_drop_pct=1.0
    )
    pos = {"cost_price": 10.0, "total_shares": 1000, "available_shares": 0}
    quote = {"current": 10.1}
    monitor = {
        "peak_price": 10.5,
        "peak_profit_pct": 5.0,
        "is_locked_limit_up": False
    }
    # Bought today: is_past_buy = False
    reason, info = evaluate_position_realtime_rules(
        "sh600000", pos, quote, {}, config, monitor,
        validated_price=10.1, is_past_buy=False
    )
    assert reason is None
    assert info["status"] == "t1_locked"

    # But hard stop loss (<= -6%) ALWAYS triggers even for T+1 holdings
    reason_hs, info_hs = evaluate_position_realtime_rules(
        "sh600000", pos, {"current": 9.3}, {}, config, monitor,
        validated_price=9.3, is_past_buy=False
    )
    assert reason_hs == "盘中硬止损信号"
    assert info_hs["status"] == "triggered_hard_stop"


def test_strategy_trailing_stop_intraday_execution(account, clock):
    seed_position(account, clock, shares=300, price=12.50, buy_day="2026-09-21")
    s = prepared(account, stop_loss_mode="intraday", trailing_stop_enabled=True,
                 trailing_stop_activation_pct=4.0, trailing_stop_drop_pct=2.0)

    # 1. 10:00 - Price rallies to 13.50 (+7.96% profit, higher than historical bars ~12.61)
    t1 = dt.datetime(2026, 9, 22, 10, 0, tzinfo=BEIJING)
    tick(s, {SYMBOL: market_quote(now=t1, current=13.50, high=13.50, pre_close=12.58)}, clock, now=t1)
    assert SYMBOL in account.positions
    assert s.state["position_monitors"][SYMBOL]["peak_price"] == 13.50

    # 2. 10:30 - Price drops to 13.10 (drop = (13.1/13.5 - 1)*100 = -2.96% > 2%, still profit)
    t2 = dt.datetime(2026, 9, 22, 10, 30, tzinfo=BEIJING)
    tick(s, {SYMBOL: market_quote(now=t2, current=13.10, high=13.50, pre_close=12.58)}, clock, now=t2)

    # Sell executed immediately via trailing stop
    assert SYMBOL not in account.positions
    last_trade = account.trades[-1]
    assert last_trade["symbol"] == SYMBOL
    assert last_trade["side"].lower() == "sell"
    assert "盘中动态跟踪止盈信号" in last_trade["reason"]
    assert "实时触发执行" in last_trade["reason"]


def test_strategy_risk_status_shows_guardian_metrics(account, clock):
    seed_position(account, clock, shares=300, price=12.50, buy_day="2026-09-21")
    s = prepared(account, stop_loss_mode="intraday", trailing_stop_enabled=True)

    t1 = dt.datetime(2026, 9, 22, 10, 0, tzinfo=BEIJING)
    quotes = {SYMBOL: market_quote(now=t1, current=13.50, high=13.50, pre_close=12.58)}

    state_before = copy.deepcopy(s.state)
    status_rows = s.risk_status(quotes, now=t1)
    state_after = copy.deepcopy(s.state)

    # Risk status must be read-only!
    assert state_before == state_after

    assert len(status_rows) == 1
    row = status_rows[0]
    assert row["symbol"] == SYMBOL
    assert row["current"] == 13.50
    assert row["peak_price"] == 13.50
    assert row["peak_profit_pct"] == pytest.approx(7.96, abs=0.1)
    assert row["trailing_stop_active"] is True
    assert "guard_info" in row

