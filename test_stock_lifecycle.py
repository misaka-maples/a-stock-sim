"""Tests for stock lifecycle and confirmed delisting handling."""
import datetime as dt
import json
from pathlib import Path

import pytest

from stock_lifecycle import (
    confirmed_delisted_symbols,
    filter_active_symbols,
    record_delisted,
    _validated,
    _path,
)
from sim_trader import SimAccount
from daily_momentum import MomentumRotationProStrategy


def test_validated_row_rules():
    today = dt.date(2026, 11, 2)
    valid = {"code": "sh.600001", "code_name": "邯郸钢铁", "outDate": "2009-12-29", "status": "0", "type": "1"}
    res = _validated(valid, today=today)
    assert res is not None
    assert res["code"] == "sh600001"
    assert res["name"] == "邯郸钢铁"
    assert res["out_date"] == "2009-12-29"

    # Future delisting date
    future = {"code": "sh.600001", "outDate": "2026-12-01", "status": "0", "type": "1"}
    assert _validated(future, today=today) is None

    # Active status (status != '0')
    active = {"code": "sh.600001", "outDate": "2009-12-29", "status": "1", "type": "1"}
    assert _validated(active, today=today) is None

    # Non-stock type (type != '1')
    index_type = {"code": "sh.600001", "outDate": "2009-12-29", "status": "0", "type": "2"}
    assert _validated(index_type, today=today) is None

    # Invalid code format
    bad_code = {"code": "unknown123", "outDate": "2009-12-29", "status": "0", "type": "1"}
    assert _validated(bad_code, today=today) is None


def test_record_and_read_delisted(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    rows = [
        {"code": "sh.600001", "code_name": "邯郸钢铁", "outDate": "2009-12-29", "status": "0", "type": "1"},
        {"code": "sz.000832", "code_name": "龙涤股份", "outDate": "2006-06-01", "status": "0", "type": "1"},
        {"code": "sh.600036", "code_name": "招商银行", "outDate": "", "status": "1", "type": "1"},  # Active, skipped
    ]
    confirmed = record_delisted(rows, base_dir=tmp_path)
    assert confirmed == {"sh600001", "sz000832"}

    read_back = confirmed_delisted_symbols(base_dir=tmp_path)
    assert read_back == {"sh600001", "sz000832"}

    # Merge new row
    new_rows = [
        {"code": "sz.003043", "code_name": "华亚智能", "outDate": "2025-10-01", "status": "0", "type": "1"}
    ]
    record_delisted(new_rows, base_dir=tmp_path)
    merged = confirmed_delisted_symbols(base_dir=tmp_path)
    assert merged == {"sh600001", "sz000832", "sz003043"}


def test_filter_active_symbols(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = [
        {"code": "sh.600001", "code_name": "邯郸钢铁", "outDate": "2009-12-29", "status": "0", "type": "1"},
        {"code": "sz.000832", "code_name": "龙涤股份", "outDate": "2006-06-01", "status": "0", "type": "1"},
    ]
    record_delisted(rows, base_dir=tmp_path)

    symbols = ["sh600036", "sh600001", "sz000832", "sz000001"]
    # Without positions
    active = filter_active_symbols(symbols, base_dir=tmp_path)
    assert active == ["sh600036", "sz000001"]

    # With position in sh600001 (retained for risk management)
    positions = {"sh600001": {"shares": 100}}
    active_with_held = filter_active_symbols(symbols, positions=positions, base_dir=tmp_path)
    assert active_with_held == ["sh600036", "sh600001", "sz000001"]


def test_daily_momentum_excludes_delisted_stocks(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = [
        {"code": "sh.600001", "code_name": "邯郸钢铁", "outDate": "2009-12-29", "status": "0", "type": "1"},
    ]
    record_delisted(rows, base_dir=tmp_path)

    # Schedule with one_off_remaining containing the delisted stock
    schedule_file = tmp_path / "history_update_schedule.json"
    schedule_file.write_text(json.dumps({
        "after_close_only": True,
        "one_off_remaining": ["sh600036", "sh600001"]
    }))

    account_file = tmp_path / "sim_account.json"
    account = SimAccount(initial_cash=100000, save_path=str(account_file))

    strategy = MomentumRotationProStrategy(
        account=account,
        watchlist=["sh600036", "sh600001", "sz000001"],
        feed=None
    )

    # Watchlist should have excluded sh600001
    assert "sh600001" not in strategy.watchlist
    assert set(strategy.watchlist) == {"sh600036", "sz000001"}

    # One off remaining should not contain sh600001
    assert "sh600001" not in strategy._one_off_remaining
    assert strategy._one_off_remaining == {"sh600036"}


def test_daily_momentum_retains_held_delisted_position_for_risk(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = [
        {"code": "sh.600001", "code_name": "邯郸钢铁", "outDate": "2009-12-29", "status": "0", "type": "1"},
    ]
    record_delisted(rows, base_dir=tmp_path)

    account_file = tmp_path / "sim_account.json"
    account = SimAccount(initial_cash=100000, save_path=str(account_file))
    account.positions["sh600001"] = {
        "shares": 1000, "available_shares": 1000, "cost_price": 5.0,
        "buy_lots": [{"date": "2026-10-01", "shares": 1000, "price": 5.0}]
    }

    strategy = MomentumRotationProStrategy(
        account=account,
        watchlist=["sh600036", "sh600001"],
        feed=None
    )

    # Since sh600001 is held, it remains in watchlist for tracking
    assert "sh600001" in strategy.watchlist
