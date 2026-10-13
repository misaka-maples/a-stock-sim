"""Offline authenticated server timing, configuration and failure regression tests."""
import copy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import server
import test_server as server_tests

NOW = server_tests.NOW


@pytest.fixture
def api():
    case = server_tests.TestServerAPI(methodName="test_get_status")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()
        case.doCleanups()


def test_paused_hot_participation_applies_to_manual_orders(api):
    api.client.post("/api/strategy/toggle", json={"active": False})
    result = api.client.post("/api/strategy/config", json={"participation_rate": .02})
    assert result.status_code == 200
    assert api.ctx.account.participation_rate == .02
    filled = api.order(shares=500).json()
    assert filled["success"] and filled["trade"]["shares"] == 200
    assert not api.ctx.strategy_active


def test_switch_and_reset_sync_broker_participation(api):
    result = api.client.post("/api/strategy/switch", json=dict(
        strategy_id="Momentum_Rotation", participation_rate=.01))
    assert result.status_code == 200 and api.ctx.account.participation_rate == .01
    reset = api.client.post("/api/account/reset", json={"cash": 100000.})
    assert reset.status_code == 200 and reset.json()["success"]
    assert api.ctx.account.participation_rate == .01
    assert api.ctx.strategy.state is api.ctx.account.strategy_state[api.ctx.strategy.name]


def test_interval_watchlist_are_persisted_and_restored_at_startup(api):
    result = api.client.post("/api/strategy/config", json=dict(
        symbols=["600519", "sh600519", "sz000001"], interval=7.5, participation_rate=.02))
    assert result.status_code == 200
    stored = json.loads(server.STRATEGY_CONFIG_PATH.read_text())
    assert stored["symbols"] == ["sh600519", "sz000001"] and stored["interval"] == 7.5
    with patch.object(server.sys, "argv", ["server", "--account-file", str(api.root / "boot.json")]), \
         patch.object(server.StockUniverse, "load_universe", side_effect=AssertionError("must use saved watchlist")), \
         patch.object(server.uvicorn, "run"):
        server.main()
    assert api.ctx.symbols == stored["symbols"] and api.ctx.interval == 7.5
    assert api.ctx.account.participation_rate == .02


@pytest.mark.parametrize("interval", [0, .5, 61, "nan", "inf", "-inf"])
def test_invalid_interval_never_mutates_configuration(api, interval):
    api.client.post("/api/strategy/config", json={"interval": 3})
    before = server.STRATEGY_CONFIG_PATH.read_text()
    result = api.client.post("/api/strategy/config", json={"interval": interval})
    assert result.status_code == 422
    assert api.ctx.interval == 3 and server.STRATEGY_CONFIG_PATH.read_text() == before


def test_configuration_write_failure_preserves_memory_and_file(api):
    api.client.post("/api/strategy/config", json={"interval": 3, "participation_rate": .05})
    before = server.STRATEGY_CONFIG_PATH.read_text()
    with patch.object(server, "persist_settings", side_effect=OSError("mock disk full")):
        with pytest.raises(OSError):
            api.client.post("/api/strategy/config", json={"interval": 8, "participation_rate": .01})
    assert api.ctx.interval == 3 and api.ctx.strategy.config.participation_rate == .05
    assert api.ctx.account.participation_rate == .05
    assert server.STRATEGY_CONFIG_PATH.read_text() == before


def test_reset_write_failure_keeps_old_account(api):
    api.order(shares=100)
    old = api.ctx.account
    before = old.save_path.read_text()
    with patch.object(server, "SimAccount", side_effect=OSError("mock disk full")):
        result = api.client.post("/api/account/reset", json={"cash": 100000.})
    assert result.status_code == 503
    assert api.ctx.account is old and old.save_path.read_text() == before
    assert not list(api.root.glob("*.reset.*"))


def test_manual_nonfinite_quote_is_rejected_without_mutating_account(api):
    api.quotes["sz002594"]["current"] = float("inf")
    before = api.ctx.account.cash
    result = api.order().json()
    assert not result["success"] and "非有限" in result["reason"]
    assert api.ctx.account.cash == before and not api.ctx.account.trades


def test_finite_quote_outside_exchange_limits_cannot_change_marks(api):
    api.order(shares=200)
    api.ctx.strategy.prepare = MagicMock()
    api.ctx.strategy_active = False
    api.quotes["sz002594"]["current"] = 1e100
    before = api.ctx.account.positions["sz002594"]["last_price"]
    server.worker_step(now=NOW)
    assert api.ctx.account.positions["sz002594"]["last_price"] == before
    assert api.ctx.invalid_quote_count == 1


def test_offhours_refreshes_freshness_without_retaining_true_flag(api):
    later = NOW.replace(hour=12, minute=0)
    api.ctx.strategy.prepare = MagicMock()
    api.ctx.strategy.on_tick = MagicMock()
    api.fetch.reset_mock()
    server.worker_step(now=later)
    assert api.ctx.last_quotes and all(not q["fresh"] for q in api.ctx.last_quotes.values())
    api.fetch.assert_not_called()
    api.ctx.strategy.on_tick.assert_not_called()


def test_invalid_quote_does_not_poison_existing_marks(api):
    api.order(shares=200)
    api.ctx.strategy.prepare = MagicMock()
    api.ctx.strategy.latest_candidates = []
    api.ctx.strategy_active = False
    api.quotes["sz002594"]["current"] = float("inf")
    before = api.ctx.account.positions["sz002594"]["last_price"]
    server.worker_step(now=NOW)
    assert api.ctx.account.positions["sz002594"]["last_price"] == before
    assert api.ctx.invalid_quote_count == 1
    assert api.ctx.last_quotes["sz002594"]["fresh"] is False
    json.dumps(api.ctx.account.get_summary(), allow_nan=False)


def test_final_close_fetch_precedes_archive_and_notification(api):
    api.order(shares=200)
    api.ctx.strategy.prepare = MagicMock()
    api.ctx.strategy.latest_candidates = []
    api.ctx.strategy_active = False
    close = NOW.replace(hour=15, minute=0, second=3)
    stale = copy.deepcopy(api.quotes["sz002594"])
    stale["time"] = "20260922145600"
    final = copy.deepcopy(stale)
    final.update(time="20260922150000", current=10.8)
    api.quotes["sz002594"] = stale
    recorded = []
    def record(**kwargs):
        recorded.append(api.ctx.account.positions["sz002594"]["last_price"])
    with patch.object(server.SnapshotManager, "update_daily_snapshot", side_effect=record) as archive, \
         patch.object(server.NotificationManager, "notify_daily_summary") as notify:
        server.worker_step(now=close)
        assert api.ctx.closing_report_pending and not archive.called and not notify.called
        api.quotes["sz002594"] = final
        server.worker_step(now=close + dt.timedelta(seconds=3))
        assert not api.ctx.closing_report_pending and recorded == [10.8]
        assert archive.call_count == notify.call_count == 1
        server.worker_step(now=close + dt.timedelta(seconds=6))
        assert archive.call_count == notify.call_count == 1


def test_opening_boundary_sleep_is_short_and_interval_damage_does_not_kill_worker(api):
    assert server.worker_wait_seconds(NOW.replace(hour=9, minute=29, second=55)) == 5
    assert server.worker_wait_seconds(NOW.replace(hour=12, minute=59, second=58)) == 2
    api.ctx.interval = float("inf")
    assert server.worker_wait_seconds(NOW) == 3


def test_storage_lock_blocks_auto_trades_and_marks_health_degraded(api):
    api.ctx.worker_thread = SimpleNamespace(is_alive=lambda: True)
    api.ctx.worker_started = True
    api.ctx.running = True
    api.ctx.strategy.prepare = MagicMock()
    api.ctx.strategy.on_tick = MagicMock()
    api.ctx.account._persistence_error = "mock disk full"
    server.worker_step(now=NOW)
    api.ctx.strategy.on_tick.assert_not_called()
    assert api.ctx.strategy_active
    assert api.ctx.worker_last_error == "mock disk full"
    assert not api.client.get("/api/status").json()["worker_healthy"]
    assert api.client.get("/health").status_code == 503


def test_stuck_live_thread_has_degraded_health(api):
    api.ctx.worker_thread = SimpleNamespace(is_alive=lambda: True)
    api.ctx.worker_started = True
    api.ctx.running = True
    api.ctx.worker_last_success = NOW - dt.timedelta(minutes=5)
    assert api.client.get("/api/status").json()["worker_healthy"] is False
    assert api.client.get("/health").status_code == 503
