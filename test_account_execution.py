"""Offline integration checks for enforced account execution and persistence."""
import copy
import datetime as dt
import json
from types import SimpleNamespace

import pytest
import requests

import sim_trader
from sim_trader import SimAccount
from trade_calendar import BEIJING


NOW = dt.datetime(2026, 9, 22, 10, 0, tzinfo=BEIJING)


def make_quote(now=NOW, **changes):
    q = dict(symbol="sh600000", name="测试银行", time=now.strftime("%Y%m%d%H%M%S"),
             current=10, pre_close=10, volume_lots=10000, fresh=True,
             asks=[dict(price=10.01, volume=100)], bids=[dict(price=9.99, volume=100)])
    q.update(changes)
    return q


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]
    class ClockDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)
    proxy = {name: getattr(dt, name) for name in dir(dt) if not name.startswith("__")}
    proxy["datetime"] = ClockDateTime
    monkeypatch.setattr(sim_trader, "datetime", SimpleNamespace(**proxy))
    return current


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("integration tests must never fetch market data")
    monkeypatch.setattr(requests.Session, "request", forbidden)


@pytest.fixture
def account(tmp_path, clock):
    return SimAccount(initial_cash=100000, save_path=str(tmp_path / "account.json"))


def test_execution_is_enforced_by_default_and_supplied_price_cannot_bypass_quote(account):
    assert not account.ledger_only
    assert not account.execute_buy("sh600000", "测试", 1, 100)["success"]
    account.bind_execution_quotes({"sh600000": make_quote()}, now=NOW)
    result = account.execute_buy("sh600000", "测试", .01, 100)
    assert result["success"] and result["trade"]["price"] == 10.01
    assert result["trade"]["time"] == "2026-09-22 10:00:00"
    assert account.positions["sh600000"]["buy_lots"][0]["date"] == "2026-09-22"


def test_bound_execution_clock_dates_fill_snapshot_and_save(account, clock):
    # The supplied execution clock is authoritative, even with a different
    # machine clock, making replay dates and account persistence consistent.
    clock[0] = NOW + dt.timedelta(days=2)
    account.bind_execution_quotes({"sh600000": make_quote()}, now=NOW)
    result = account.execute_buy("sh600000", "测试", 10, 100)
    assert result["trade"]["time"] == "2026-09-22 10:00:00"
    assert account.equity_history[-1]["time"] == "2026-09-22 10:00:00"
    assert account.daily_equity_history[-1]["time"] == "2026-09-22 10:00:00"
    assert json.loads(account.save_path.read_text())["updated_at"].startswith("2026-09-22T10:00:00")


@pytest.mark.parametrize("changes", [dict(time="20260921100000"), dict(asks=[]),
                                     dict(volume_lots=0), dict(current=float("nan"))])
def test_old_missing_and_invalid_quotes_never_change_account(account, changes):
    account.bind_execution_quotes({"sh600000": make_quote(**changes)}, now=NOW)
    before = account.cash
    result = account.execute_buy("sh600000", "测试", 10, 100)
    assert not result["success"]
    assert account.cash == before and not account.positions and not account.trades


def test_snapshot_capacity_consumption_survives_restart(account):
    q = make_quote()
    account.bind_execution_quotes({"sh600000": q}, now=NOW)
    first = account.execute_buy("sh600000", "测试", 10, 1000)
    assert first["success"] and first["status"] == "PARTIALLY_FILLED"
    assert first["trade"]["shares"] == 500
    assert first["order"]["requested_shares"] == 1000
    raw = json.loads(account.save_path.read_text())
    assert raw["orders"][-1]["status"] == "PARTIALLY_FILLED"
    assert raw["quote_consumption"] and raw["daily_consumption"]
    reloaded = SimAccount(save_path=str(account.save_path))
    reloaded.bind_execution_quotes({"sh600000": q}, now=NOW)
    assert not reloaded.execute_buy("sh600000", "测试", 10, 100)["success"]
    assert len(reloaded.trades) == 1


def test_same_timestamp_book_revision_does_not_reset_consumption(account):
    q = make_quote()
    account.bind_execution_quotes({"sh600000": q}, now=NOW)
    assert account.execute_buy("sh600000", "测试", 10, 500)["success"]
    changed = copy.deepcopy(q)
    changed["asks"][0]["price"] = 10.02
    account.bind_execution_quotes({"sh600000": changed}, now=NOW)
    assert not account.execute_buy("sh600000", "测试", 10, 100)["success"]


def test_same_day_t1_rejects_sell_then_next_session_unlocks(account, clock):
    q = make_quote()
    account.bind_execution_quotes({"sh600000": q}, now=NOW)
    assert account.execute_buy("sh600000", "测试", 10, 100)["success"]
    assert account.positions["sh600000"]["available_shares"] == 0
    assert not account.execute_sell("sh600000", 10, 100)["success"]
    clock[0] = NOW + dt.timedelta(days=1)
    account.update_t1_available_shares(clock[0].date().isoformat())
    account.bind_execution_quotes({"sh600000": make_quote(clock[0])}, now=clock[0])
    sold = account.execute_sell("sh600000", 1000, 100)
    assert sold["success"] and sold["trade"]["price"] == 9.99
    assert not account.positions


def test_failed_cash_validation_does_not_consume_liquidity(account):
    account.bind_execution_quotes({"sh600000": make_quote()}, now=NOW)
    account.cash = 0
    assert not account.execute_buy("sh600000", "测试", 10, 100)["success"]
    assert not account._quote_consumption and not account._daily_consumption
    account.cash = 100000
    assert account.execute_buy("sh600000", "测试", 10, 500)["success"]


def test_manual_limit_price_is_an_execution_constraint(account):
    account.bind_execution_quotes({"sh600000": make_quote()}, now=NOW)
    assert not account.execute_buy("sh600000", "测试", 10, 100, limit_price=10)["success"]
    assert account.execute_buy("sh600000", "测试", 10, 100, limit_price=10.01)["success"]


def test_daily_participation_caps_across_different_timestamps(account):
    # Both visible book and cumulative day volume initially permit 500 shares.
    q = make_quote(volume_lots=100)
    account.bind_execution_quotes({"sh600000": q}, now=NOW)
    assert account.execute_buy("sh600000", "测试", 10, 500)["success"]
    later = NOW + dt.timedelta(seconds=3)
    account.bind_execution_quotes({"sh600000": make_quote(later, volume_lots=100)}, now=later)
    assert not account.execute_buy("sh600000", "测试", 10, 100)["success"]
    account.bind_execution_quotes({"sh600000": make_quote(later, volume_lots=200)}, now=later)
    assert account.execute_buy("sh600000", "测试", 10, 500)["success"]


def test_risk_maxima_survive_minute_truncation_and_restart(account, clock):
    account.cash = 120000
    account.record_equity_snapshot(force=True)
    clock[0] = NOW + dt.timedelta(minutes=1)
    account.cash = 90000
    account.record_equity_snapshot(force=True)
    assert account.risk_history["max_drawdown"] == 30000
    assert account.risk_history["max_drawdown_pct"] == 25
    # The display's bounded minute curve loses both historical extrema.
    account.equity_history = [dict(time="2026-09-22 10:02:00", equity=110000)] * 1000
    clock[0] = NOW + dt.timedelta(minutes=2)
    account.cash = 110000
    account.record_equity_snapshot(force=True)
    account.save()
    restored = SimAccount(save_path=str(account.save_path))
    summary = restored.get_summary()
    assert len(restored.equity_history) == 1000
    assert summary["peak_equity"] == 120000
    assert summary["max_drawdown"] == 30000
    assert summary["max_drawdown_pct"] == 25


def test_legacy_account_marks_incomplete_risk_history(account):
    raw = json.loads(account.save_path.read_text())
    raw.pop("risk_history", None)
    raw.pop("daily_equity_history", None)
    account.save_path.write_text(json.dumps(raw))
    restored = SimAccount(save_path=str(account.save_path))
    assert restored.get_summary()["risk_history_complete"] is False


def test_legacy_migration_preserves_known_drawdown_and_daily_points(tmp_path, clock):
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps(dict(summary=dict(initial_cash=100000, cash=95000,
                    peak_equity=120000, max_drawdown=30000, max_drawdown_pct=25),
                    positions={}, trades=[], orders=[],
                    equity_history=[dict(time='2026-09-21 15:00:00', equity=90000),
                                    dict(time='2026-09-22 10:00:00', equity=95000)])))
    migrated = SimAccount(save_path=str(path))
    migrated.equity_history = []
    summary = migrated.get_summary()
    assert summary['max_drawdown'] == 30000
    assert summary['max_drawdown_pct'] == 25
    assert not summary['risk_history_complete']
    assert len(migrated.daily_equity_history) == 2


def test_one_durable_commit_contains_capacity_final_status_and_strategy_intent(account, monkeypatch):
    q = make_quote()
    account.bind_execution_quotes({q['symbol']: q}, now=NOW)
    account.strategy_state['test'] = {'filled': 0}
    actual_save = account.save
    commits = []
    def crash_after_commit():
        actual_save()
        commits.append(json.loads(account.save_path.read_text()))
        raise SystemExit('process death after complete commit')
    monkeypatch.setattr(account, 'save', crash_after_commit)
    def update(trade, broker):
        broker.strategy_state['test']['filled'] += trade['shares']
    with pytest.raises(SystemExit):
        account.execute_buy(q['symbol'], '测试', 10, 1000, state_update=update)
    assert len(commits) == 1
    persisted = commits[0]
    assert persisted['orders'][-1]['status'] == 'PARTIALLY_FILLED'
    assert persisted['orders'][-1]['requested_shares'] == 1000
    assert persisted['strategy_state']['test']['filled'] == 500
    assert persisted['quote_consumption'][0][-1] == 500
    restored = SimAccount(save_path=str(account.save_path))
    restored.bind_execution_quotes({q['symbol']: q}, now=NOW)
    assert not restored.execute_buy(q['symbol'], '测试', 10, 100)['success']
    assert len(restored.trades) == 1


def test_disk_failure_rolls_back_fill_and_intent_and_suppresses_notification(account, monkeypatch):
    q = make_quote()
    account.bind_execution_quotes({q['symbol']: q}, now=NOW)
    state = account.strategy_state.setdefault('test', {'filled': 0})
    account.save()
    before = account.save_path.read_bytes()
    notifications = []
    account.register_trade_callback(lambda trade, broker: notifications.append(trade))
    def failed_replace(*args):
        raise OSError('disk full')
    monkeypatch.setattr(sim_trader.os, 'replace', failed_replace)
    def update(trade, broker):
        state['filled'] += trade['shares']
    result = account.execute_buy(q['symbol'], '测试', 10, 100, state_update=update)
    assert not result['success'] and result['status'] == 'ERROR'
    assert account.cash == 100000 and not account.trades and not account.positions
    assert state is account.strategy_state['test'] and state['filled'] == 0
    assert not account._quote_consumption and not notifications
    assert account.save_path.read_bytes() == before
    assert not account.execute_buy(q['symbol'], '测试', 10, 100)['success']
    with pytest.raises(OSError):
        account.save()


def test_expanding_book_cannot_replenish_same_time_capacity_after_restart(account):
    q = make_quote()
    account.bind_execution_quotes({q['symbol']: q}, now=NOW)
    assert account.execute_buy(q['symbol'], '测试', 10, 500)['success']
    reloaded = SimAccount(save_path=str(account.save_path))
    q['asks'][0]['volume'] = 200
    reloaded.bind_execution_quotes({q['symbol']: q}, now=NOW)
    assert not reloaded.execute_buy(q['symbol'], '测试', 10, 500)['success']
    assert sum(t['shares'] for t in reloaded.trades) == 500


def test_actual_vwap_and_fees_respect_stock_and_industry_budget(account):
    q = make_quote(current=10, asks=[dict(price=10, volume=1), dict(price=11, volume=500)])
    account.bind_execution_quotes({q['symbol']: q}, now=NOW)
    result = account.execute_buy(q['symbol'], '测试', 10, 2000,
                                 max_position_weight=.2, max_sector_weight=.2,
                                 industry_map={q['symbol']: '银行'})
    assert result['success'] and result['trade']['shares'] == 1700
    pos = account.positions[q['symbol']]
    equity = account.get_summary()['total_equity']
    cost = result['trade']['amount'] + result['trade']['fees']
    assert max(cost, pos['market_value']) <= .2 * equity
    assert account.get_summary()['peak_equity'] <= account.initial_cash


def test_full_and_partial_roundtrips_conserve_cent_cost_basis(tmp_path, clock):
    a = SimAccount(initial_cash=1200000, strict_t1=False, ledger_only=True,
                   save_path=str(tmp_path / 'exact_cost.json'))
    assert a.execute_buy('sh600000', '测试', 11, 100000)['success']
    assert a.execute_sell('sh600000', 11, 33333)['success']
    a = SimAccount(save_path=str(a.save_path), strict_t1=False, ledger_only=True)
    summary = a.get_summary()
    assert summary['total_pnl'] == round(summary['realized_pnl'] + summary['unrealized_pnl'], 2)
    assert a.execute_sell('sh600000', 11, 66667)['success']
    summary = a.get_summary()
    assert summary['total_pnl'] == summary['realized_pnl'] == -1122
    assert summary['unrealized_pnl'] == 0


def test_non_t1_partial_sell_keeps_lot_counts_for_later_strict_t1(tmp_path, clock):
    a = SimAccount(initial_cash=100000, strict_t1=False, ledger_only=True,
                   save_path=str(tmp_path / 'lots.json'))
    clock[0] = NOW - dt.timedelta(days=1)
    assert a.execute_buy('sh600000', '测试', 10, 500)['success']
    clock[0] = NOW
    assert a.execute_sell('sh600000', 10, 400)['success']
    assert a.execute_buy('sh600000', '测试', 10, 400)['success']
    pos = a.positions['sh600000']
    assert sum(lot['shares'] for lot in pos['buy_lots']) == pos['total_shares'] == 500
    strict = SimAccount(save_path=str(a.save_path), strict_t1=True)
    assert strict.positions['sh600000']['available_shares'] == 100


@pytest.mark.parametrize('invalid', [float('inf'), float('nan'), -1, 'bad'])
def test_bad_mark_cannot_pollute_equity_or_persistent_drawdown(account, invalid):
    account.ledger_only = True
    assert account.execute_buy('sh600000', '测试', 10, 100)['success']
    before = copy.deepcopy(account.risk_history)
    account.update_market_prices({'sh600000': make_quote(current=invalid)})
    account.record_equity_snapshot(force=True)
    assert account.positions['sh600000']['last_price'] == 10
    assert account.risk_history == before
    account.save()
    json.dumps(account.get_summary(), allow_nan=False)


def test_loading_corrupt_cash_fails_closed_and_preserves_file(account):
    raw = json.loads(account.save_path.read_text())
    raw['summary']['cash'] = float('inf')
    account.save_path.write_text(json.dumps(raw))
    before = account.save_path.read_bytes()
    with pytest.raises(ValueError, match='无法安全加载'):
        SimAccount(save_path=str(account.save_path))
    assert account.save_path.read_bytes() == before


def test_legacy_cost_migration_preserves_cash_and_original_pnl(tmp_path, clock):
    a = SimAccount(initial_cash=1200000, strict_t1=False, ledger_only=True,
                   save_path=str(tmp_path / 'legacy.json'))
    assert a.execute_buy('sh600000', '测试', 11, 100000)['success']
    assert a.execute_sell('sh600000', 11, 100000)['success']
    data = json.loads(a.save_path.read_text())
    data['trades'][-1]['realized_pnl'] = -1126
    a.save_path.write_text(json.dumps(data))
    migrated = SimAccount(save_path=str(a.save_path))
    assert migrated.cash == a.cash
    assert migrated.get_summary()['realized_pnl'] == -1122
    assert migrated.trades[-1]['legacy_realized_pnl'] == -1126
    assert migrated.get_summary()['accounting_history_complete']


def test_manual_broker_unlocks_next_session_without_polling(account):
    q = make_quote()
    account.bind_execution_quotes({q['symbol']: q}, now=NOW)
    assert account.execute_buy(q['symbol'], '测试', 10, 100)['success']
    next_day = NOW + dt.timedelta(days=1)
    account.bind_execution_quotes({q['symbol']: make_quote(next_day)}, now=next_day)
    assert account.positions[q['symbol']]['available_shares'] == 0
    assert account.execute_sell(q['symbol'], 10, 100)['success']


def test_legacy_non_t1_corrupted_lots_are_rebuilt_from_complete_ledger(tmp_path, clock):
    a = SimAccount(initial_cash=100000, strict_t1=False, ledger_only=True,
                   save_path=str(tmp_path / 'legacy_lots.json'))
    clock[0] = NOW - dt.timedelta(days=1)
    assert a.execute_buy('sh600000', '测试', 10, 500)['success']
    clock[0] = NOW
    assert a.execute_sell('sh600000', 10, 400)['success']
    assert a.execute_buy('sh600000', '测试', 10, 400)['success']
    raw = json.loads(a.save_path.read_text())
    raw['positions']['sh600000']['buy_lots'][0]['shares'] = 500
    a.save_path.write_text(json.dumps(raw))
    strict = SimAccount(save_path=str(a.save_path), strict_t1=True)
    position = strict.positions['sh600000']
    assert position['available_shares'] == 100
    assert sum(lot['shares'] for lot in position['buy_lots']) == position['total_shares'] == 500
    assert position['lot_history_repaired']
    assert strict.cash == a.cash and len(strict.trades) == len(a.trades)
