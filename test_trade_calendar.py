#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试：A股交易日历与节假日休市判定 (test_trade_calendar.py)
"""

import datetime
from unittest.mock import patch
import pytest

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


def test_notifier_skips_on_holiday():
    """验证收盘战报在十一假期及周末自动跳过推送，仅在开盘日或强制模式下推送"""
    mgr = NotificationManager()
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
