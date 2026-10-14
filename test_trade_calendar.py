#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试：A股交易日历与节假日休市判定 (test_trade_calendar.py)
"""

import datetime
import json
from unittest.mock import patch
import pytest

import notifier
import trade_calendar

from trade_calendar import (
    is_trading_day,
    get_holiday_reason,
    get_market_status,
    is_trading_time,
    get_next_trading_day,
    get_previous_trading_day,
    to_date,
    _load_custom_config,
)
from notifier import NotificationManager
from sim_trader import MarketFeed


@pytest.fixture(autouse=True)
def isolated_calendar_config(monkeypatch, tmp_path):
    """本地测试只读写临时配置，不加载用户的交易日或通知设置。"""
    path = tmp_path / "holidays.json"
    monkeypatch.setattr(trade_calendar, "HOLIDAYS_CONFIG_FILE", path)
    return path


@pytest.fixture
def isolated_notifier(monkeypatch, tmp_path):
    monkeypatch.setattr(notifier, "NOTIFICATION_CONFIG_PATH", tmp_path / "notifications.json")
    monkeypatch.setattr(NotificationManager, "_instance", None)
    manager = NotificationManager()
    with patch.object(manager, "dispatch_message"):
        yield manager
    manager.executor.shutdown(wait=True)


@pytest.mark.parametrize("day", [
    "2023-01-03", "2023-05-04", "2023-05-05",
    "2024-01-02", "2024-01-03", "2025-01-02", "2025-01-03",
])
def test_official_normal_weekdays_do_not_use_generic_holidays(day):
    assert get_holiday_reason(day) is None
    assert is_trading_day(day)
    assert MarketFeed.is_trading_day(day)
    assert is_trading_time(datetime.datetime.fromisoformat(day + " 10:00:00"))


@pytest.mark.parametrize("day", ["2024-01-02", "2024-01-03", "2025-01-02", "2025-01-03"])
def test_daily_execution_accepts_official_january_trading_days(day):
    from execution import match_daily
    bar = {"symbol": "sh600000", "date": day, "raw_open": 10,
           "raw_close": 10.1, "raw_high": 10.2, "raw_low": 9.8,
           "volume": 10000}
    result = match_daily("BUY", 100, bar, pre_close=10)
    assert result["success"], result
    assert result["status"] == "FILLED"


def test_2023_official_holidays_and_adjacent_sessions():
    for day in range(21, 30):
        assert not is_trading_day(f"2023-01-{day:02d}")
    assert not is_trading_day("2023-01-02")
    assert get_next_trading_day("2023-01-20") == datetime.date(2023, 1, 30)
    assert get_previous_trading_day("2023-01-30") == datetime.date(2023, 1, 20)
    assert get_next_trading_day("2022-12-30") == datetime.date(2023, 1, 3)
    assert get_previous_trading_day("2024-01-02") == datetime.date(2023, 12, 29)
    assert not is_trading_day("2023-04-05")
    assert is_trading_day("2023-04-04")
    assert is_trading_day("2023-04-06")
    assert not is_trading_day("2023-06-22")
    assert not is_trading_day("2023-06-23")
    assert get_next_trading_day("2023-06-21") == datetime.date(2023, 6, 26)
    assert get_next_trading_day("2023-09-28") == datetime.date(2023, 10, 9)


@pytest.mark.parametrize("year, expected", [(2023, 242), (2024, 242), (2025, 243)])
def test_complete_official_year_session_counts(year, expected):
    start = datetime.date(year, 1, 1)
    finish = datetime.date(year + 1, 1, 1)
    days = (finish - start).days
    assert sum(is_trading_day(start + datetime.timedelta(days=i)) for i in range(days)) == expected


def test_custom_trading_day_cannot_open_weekend(isolated_calendar_config):
    isolated_calendar_config.write_text(json.dumps({
        "trading_days": ["2026-10-10", "2026-10-11", "2026-10-01"],
    }), encoding="utf-8")
    assert not is_trading_day("2026-10-10")
    assert not is_trading_day("2026-10-11")
    assert is_trading_day("2026-10-01")


@pytest.mark.parametrize("year", [2027, 2028, 2029, 2030])
def test_unconfigured_future_calendar_is_not_a_prediction(year):
    assert year not in trade_calendar._OFFICIAL_YEARS
    # 普通工作日也不能猜测开市，撮合与时段判断必须阻断。
    day = datetime.date(year, 1, 15)
    while day.weekday() >= 5:
        day += datetime.timedelta(days=1)
    assert "年度交易日历未配置" in get_holiday_reason(day)
    assert not is_trading_day(day)
    assert not is_trading_time(datetime.datetime.combine(day, datetime.time(10)))


def test_missing_calendar_search_is_bounded():
    with pytest.raises(ValueError, match="更新年度交易日历"):
        get_next_trading_day("2026-12-31")
    with pytest.raises(ValueError, match="更新年度交易日历"):
        get_previous_trading_day("2029-01-01")


def test_explicitly_verified_custom_year_can_be_imported(isolated_calendar_config):
    isolated_calendar_config.write_text(json.dumps({
        "calendar_years": [2027], "holidays": ["2027-01-01", "2027-01-04"],
    }), encoding="utf-8")
    assert not is_trading_day("2027-01-01")
    assert not is_trading_day("2027-01-02")
    assert not is_trading_day("2027-01-04")
    assert is_trading_day("2027-01-05")
    assert get_next_trading_day("2026-12-31") == datetime.date(2027, 1, 5)


def test_uncovered_past_calendar_is_explicitly_unverified():
    assert "年度日历未核验" in get_holiday_reason("2022-05-04")


def test_timezone_aware_market_time_is_beijing_time():
    now_utc = datetime.datetime(2026, 10, 8, 2, 0, tzinfo=datetime.timezone.utc)
    assert get_market_status(now_utc) == "早盘交易中"
    assert is_trading_time(now_utc)


def test_weekend_detection():
    """验证周末（周六、周日）全天休市"""
    # 2026-10-10 是周六，2026-10-11 是周日
    sat = datetime.date(2026, 10, 10)
    sun = datetime.date(2026, 10, 11)
    mon = datetime.date(2026, 10, 12)

    assert not is_trading_day(sat)
    assert not is_trading_day(sun)
    assert is_trading_day(mon)

    assert "周末休市" in get_holiday_reason(sat)
    assert "周末休市" in get_holiday_reason(sun)
    assert get_holiday_reason(mon) is None

    # 验证 MarketFeed 方法一致性
    assert not MarketFeed.is_trading_day(sat)
    assert not MarketFeed.is_trading_day(sun)
    assert MarketFeed.is_trading_day(mon)


def test_national_day_holiday_2026():
    """验证 2026 年十一国庆节假期 (10月1日~10月7日) 严格休市，10月8日正常开盘"""
    # 2026-10-01 (周四) 至 2026-10-07 (周三) 全部为休市日
    for day in range(1, 8):
        date_str = f"2026-10-{day:02d}"
        assert not is_trading_day(date_str), f"{date_str} 必须为休市日"
        assert not MarketFeed.is_trading_day(date_str)
        reason = get_holiday_reason(date_str)
        assert reason is not None

        # 检查市场状态
        dt_1500 = datetime.datetime.fromisoformat(f"{date_str} 15:00:00")
        status = get_market_status(dt_1500)
        assert "休市中" in status
        assert not is_trading_time(dt_1500)

    # 2026-10-08 (周四) 节后首日正常开盘
    assert is_trading_day("2026-10-08")
    assert MarketFeed.is_trading_day("2026-10-08")
    assert get_holiday_reason("2026-10-08") is None

    # 验证前/后交易日跳转
    assert get_next_trading_day("2026-09-30").isoformat() == "2026-10-08"
    assert get_previous_trading_day("2026-10-08").isoformat() == "2026-09-30"


def test_other_official_holidays():
    """验证其他法定节假日（元旦、春节、清明、五一、端午、中秋）"""
    # 2026 春节 (2026-02-15 至 2026-02-23)
    assert not is_trading_day("2026-02-16")
    assert not is_trading_day("2026-02-18")
    assert "春节" in get_holiday_reason("2026-02-18")

    # 2026 清明 (2026-04-04 至 2026-04-06)
    assert not is_trading_day("2026-04-06")

    # 2026 五一劳动节 (2026-05-01 至 2026-05-05)
    assert not is_trading_day("2026-05-01")
    assert not is_trading_day("2026-05-04")

    # 2026 端午 (2026-06-19 至 2026-06-21)
    assert not is_trading_day("2026-06-19")

    # 2026 中秋 (2026-09-25 至 2026-09-27)
    assert not is_trading_day("2026-09-25")


def test_market_time_slots_on_trading_day():
    """验证交易日各时段状态划分"""
    test_date = "2026-10-08"
    # 盘前 09:10
    dt_pre = datetime.datetime.fromisoformat(f"{test_date} 09:10:00")
    assert get_market_status(dt_pre) == "未开盘（盘前）"
    assert not is_trading_time(dt_pre)

    # 开盘集合竞价 09:20
    dt_call_auction = datetime.datetime.fromisoformat(f"{test_date} 09:20:00")
    assert get_market_status(dt_call_auction) == "开盘集合竞价"
    assert not is_trading_time(dt_call_auction)

    # 早盘交易 09:45
    dt_morning = datetime.datetime.fromisoformat(f"{test_date} 09:45:00")
    assert get_market_status(dt_morning) == "早盘交易中"
    assert is_trading_time(dt_morning)

    # 午间休市 12:00
    dt_lunch = datetime.datetime.fromisoformat(f"{test_date} 12:00:00")
    assert get_market_status(dt_lunch) == "午间休市"
    assert not is_trading_time(dt_lunch)

    # 午盘交易 14:00
    dt_afternoon = datetime.datetime.fromisoformat(f"{test_date} 14:00:00")
    assert get_market_status(dt_afternoon) == "午盘交易中"
    assert is_trading_time(dt_afternoon)

    # 尾盘集合竞价 14:58
    dt_close_auction = datetime.datetime.fromisoformat(f"{test_date} 14:58:00")
    assert get_market_status(dt_close_auction) == "尾盘集合竞价"
    assert not is_trading_time(dt_close_auction)

    # 收盘 15:05
    dt_closed = datetime.datetime.fromisoformat(f"{test_date} 15:05:00")
    assert get_market_status(dt_closed) == "已收盘"
    assert not is_trading_time(dt_closed)


def test_notifier_skips_on_holiday(isolated_notifier):
    """验证收盘战报在十一假期及周末自动跳过推送，仅在开盘日或强制模式下推送"""
    mgr = isolated_notifier
    mgr.config["enabled"] = True
    mgr.config["notify_on_daily_summary"] = True

    dummy_summary = {
        "total_equity": 100000.0,
        "today_pnl": 500.0,
        "today_pnl_pct": 0.5,
        "total_pnl": 500.0,
        "total_pnl_pct": 0.5,
        "cash": 50000.0,
        "market_value": 50000.0,
        "win_rate": 60.0,
        "profit_loss_ratio": 2.0
    }
    dummy_positions = {}

    with patch.object(mgr, "async_dispatch") as mock_dispatch:
        # 1. 十一长假期间 (例如 2026-10-02)，未开启 force -> 应跳过推送，不调用 async_dispatch
        mgr.notify_daily_summary(dummy_summary, dummy_positions, force=False, date_str="2026-10-02")
        mock_dispatch.assert_not_called()

        # 2. 周末 (例如 2026-10-10 周六)，未开启 force -> 应跳过推送
        mgr.notify_daily_summary(dummy_summary, dummy_positions, force=False, date_str="2026-10-10")
        mock_dispatch.assert_not_called()

        # 3. 节假日但启用 force=True (手动强制/测试触发) -> 应正常调用 async_dispatch
        mgr.notify_daily_summary(dummy_summary, dummy_positions, force=True, date_str="2026-10-02")
        mock_dispatch.assert_called_once()

    with patch.object(mgr, "async_dispatch") as mock_dispatch:
        # 4. 开盘交易日 (例如 2026-10-08 节后首个交易日) -> 正常调用推送
        mgr.notify_daily_summary(dummy_summary, dummy_positions, force=False, date_str="2026-10-08")
        mock_dispatch.assert_called_once()
