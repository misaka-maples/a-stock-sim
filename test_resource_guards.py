"""Offline resource-admission and cleanup regressions."""
import threading
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import server
import runtime_resources as resources

@pytest.fixture(autouse=True)
def synchronous_compatibility(monkeypatch):
    monkeypatch.setattr(server, "BACKTEST_ASYNC", False)



def test_get_and_post_share_nonblocking_guard():
    entered, finish = threading.Event(), threading.Event()
    def backtest(req):
        entered.set()
        assert finish.wait(5)
        return {'success': True}
    with patch.object(server, 'memory_snapshot', return_value={'backtest_allowed': True}), \
         patch.object(server, '_run_backtest_request', side_effect=backtest), \
         patch.object(server, 'release_unused_memory') as cleanup:
        first = threading.Thread(target=server.run_backtest_get)
        first.start()
        try:
            assert entered.wait(5)
            with pytest.raises(HTTPException) as error:
                server.run_backtest_endpoint(server.BacktestRequest())
            assert error.value.status_code == 409
            cleanup.assert_not_called()
        finally:
            finish.set()
            first.join(5)
        assert not first.is_alive()
        assert not server.backtest_lock.locked()
        cleanup.assert_called_once()


def test_low_memory_rejection_does_not_execute_and_unlocks():
    with patch.object(server, 'memory_snapshot', return_value={'backtest_allowed': False}), \
         patch.object(server, '_run_backtest_request') as run, \
         patch.object(server, 'release_unused_memory'):
        with pytest.raises(HTTPException) as error:
            server.run_backtest_get()
        assert error.value.status_code == 503
        assert error.value.headers['Retry-After'] == '60'
        run.assert_not_called()
        assert not server.backtest_lock.locked()


def test_cleanup_failure_cannot_mask_result_or_keep_lock():
    with patch.object(server, 'memory_snapshot', return_value={'backtest_allowed': True}), \
         patch.object(server, '_run_backtest_request', return_value={'success': True}), \
         patch.object(server, 'release_unused_memory', side_effect=RuntimeError('cleanup')):
        assert server.run_backtest_get() == {'success': True}
        assert not server.backtest_lock.locked()


def test_execution_exception_always_unlocks():
    with patch.object(server, 'memory_snapshot', return_value={'backtest_allowed': True}), \
         patch.object(server, '_run_backtest_request', side_effect=HTTPException(503, 'provider busy')), \
         patch.object(server, 'release_unused_memory') as cleanup:
        with pytest.raises(HTTPException) as error:
            server.run_backtest_get()
        assert error.value.status_code == 503
        cleanup.assert_called_once()
        assert not server.backtest_lock.locked()


def test_memory_uses_tightest_ancestor_and_host(tmp_path):
    proc, root = tmp_path / 'proc', tmp_path / 'cgroup'
    (proc / 'self').mkdir(parents=True)
    child = root / 'slice/service'
    child.mkdir(parents=True)
    (proc / 'self/status').write_text('VmRSS: 1024 kB\n')
    (proc / 'meminfo').write_text('MemAvailable: 8388608 kB\n')
    (proc / 'self/cgroup').write_text('0::/slice/service\n')
    for node, limit, used in [(child, 10, 3), (child.parent, 12, 8)]:
        (node / 'memory.max').write_text(str(limit * resources.GIB))
        (node / 'memory.current').write_text(str(used * resources.GIB))
    snapshot = resources.memory_snapshot(proc, root)
    assert snapshot['rss_bytes'] == 1024 ** 2
    assert snapshot['effective_available_bytes'] == 4 * resources.GIB
    assert not snapshot['backtest_allowed'] and snapshot['memory_healthy']
    (proc / 'meminfo').write_text('MemAvailable: 262144 kB\n')
    assert not resources.memory_snapshot(proc, root)['memory_healthy']


def test_missing_memory_probe_fails_closed(tmp_path):
    snapshot = resources.memory_snapshot(tmp_path, tmp_path)
    assert not snapshot['backtest_allowed']
    assert snapshot['effective_available_bytes'] is None


def test_http_rejections_and_health_are_visible():
    import test_server as server_tests
    case = server_tests.TestServerAPI(methodName='test_get_status')
    case.setUp()
    try:
        server.backtest_lock.acquire()
        try:
            assert case.client.post('/api/backtest/run', json={}).status_code == 409
            assert case.client.get('/api/backtest/run').status_code == 409
        finally:
            server.backtest_lock.release()
        with patch.object(server, 'memory_snapshot', return_value={
                'backtest_allowed': False, 'memory_healthy': False, 'effective_available_bytes': 0}):
            assert case.client.post('/api/backtest/run', json={}).status_code == 503
            health = case.client.get('/health')
            assert health.status_code == 503
            assert health.json()['resources']['effective_available_bytes'] == 0
    finally:
        case.tearDown()
        case.doCleanups()
