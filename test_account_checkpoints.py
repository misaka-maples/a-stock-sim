"""Periodic checkpoints never weaken order durability or hide storage faults."""
import json

import pytest

import sim_trader
from sim_trader import AccountPersistenceError, SimAccount


@pytest.fixture
def account(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(sim_trader.time, "monotonic", lambda: clock[0])
    broker = SimAccount(save_path=str(tmp_path / "account.json"), ledger_only=True)
    return broker, clock


def test_periodic_checkpoints_coalesce_before_serialization(account, monkeypatch):
    broker, clock = account
    original = sim_trader.json.dumps
    calls = []
    def serialize(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(sim_trader.json, "dumps", serialize)
    for tick in range(3, 30, 3):
        clock[0] = 100 + tick
        assert broker.save_if_due() is False
    assert calls == []
    clock[0] = 130
    assert broker.save_if_due() is True
    assert len(calls) == 1


def test_trade_and_explicit_saves_bypass_checkpoint_delay(account):
    broker, clock = account
    clock[0] += 1
    assert broker.execute_buy("sh600000", "测试", 10, 100)["success"]
    persisted = json.loads(broker.save_path.read_text())
    assert len(persisted["trades"]) == len(persisted["orders"]) == 1
    assert persisted["summary"]["cash"] == broker.cash
    broker.strategy_state["checkpoint"] = "explicit"
    broker.save()
    assert json.loads(broker.save_path.read_text())["strategy_state"]["checkpoint"] == "explicit"
    assert broker.save_if_due() is False


def test_failed_checkpoint_does_not_advance_clock_or_hide_error(account, monkeypatch):
    broker, clock = account
    clock[0] = 130
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(sim_trader.os, "replace", fail)
    with pytest.raises(AccountPersistenceError) as failure:
        broker.save_if_due()
    assert not failure.value.committed
    assert broker._last_durable_save == 100
    clock[0] = 101  # Even a recent success cannot suppress a latched failure.
    with pytest.raises(AccountPersistenceError):
        broker.save_if_due()
    assert not broker.execute_buy("sh600000", "测试", 10, 100)["success"]


def test_directory_sync_failure_does_not_advance_checkpoint_clock(account, monkeypatch):
    broker, clock = account
    clock[0] = 130
    original = sim_trader.os.fsync
    calls = []
    def fail_directory(descriptor):
        calls.append(descriptor)
        if len(calls) == 2:
            raise OSError("directory sync failed")
        original(descriptor)
    monkeypatch.setattr(sim_trader.os, "fsync", fail_directory)
    with pytest.raises(AccountPersistenceError) as failure:
        broker.save_if_due()
    assert failure.value.committed
    assert broker._last_durable_save == 100


def test_loaded_account_gets_first_checkpoint_immediately(account):
    broker, _ = account
    restored = SimAccount(save_path=str(broker.save_path), ledger_only=True)
    assert restored.save_if_due() is True


def test_engine_once_flushes_deferred_checkpoint(account):
    broker, _ = account
    class Strategy:
        account = broker
    engine = sim_trader.PaperTradingEngine(Strategy(), [])
    def step():
        broker.strategy_state["pending"] = True
        assert broker.save_if_due() is False
    engine.step = step
    engine.run(once=True)
    assert json.loads(broker.save_path.read_text())["strategy_state"]["pending"] is True
