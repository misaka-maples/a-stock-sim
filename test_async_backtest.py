from unittest.mock import patch
from pathlib import Path
import pytest
import server
import backtest_worker as worker
from backtest_jobs import JobStore
from test_server import TestServerAPI

@pytest.fixture
def api():
    case=TestServerAPI(methodName='test_get_status');case.setUp()
    try:
        with patch.object(server,'BACKTEST_ASYNC',True),patch.object(server,'_job_store',JobStore(case.root/'jobs')):
            yield case
    finally:case.doCleanups()

def test_http_submit_snapshots_arguments_without_running(api):
    with patch.object(server,'run_backtest',side_effect=AssertionError('must not run in web process')):
        response=api.client.post('/api/backtest/run',json={'pool_type':'current_watchlist','use_current_config':True})
    assert response.status_code==202,response.text
    job=response.json()['job'];before=server.backtest_store().get(job['id'],private=True)
    api.ctx.symbols.clear()
    assert before['arguments']['symbols']
    assert server.backtest_store().get(job['id'],private=True)['arguments']==before['arguments']
    assert api.client.post('/api/backtest/run',json={}).status_code==409
    assert api.client.get('/api/backtest/jobs/'+job['id']+'/result').status_code==409
    assert api.client.post('/api/backtest/jobs/'+job['id']+'/cancel').json()['job']['status']=='cancelled'
    assert api.client.get('/api/trades?limit=-1').status_code==422
    assert api.client.get('/api/orders?limit=10000').status_code==422

def test_legacy_engine_cannot_bypass_queue_memory_limits(api):
    response=api.client.post('/api/backtest/run',json={'strategy_name':'TurtleBreakout'})
    assert response.status_code==400,response.text

def test_scheduler_terminates_child_at_protected_window(tmp_path,monkeypatch):
    store=JobStore(tmp_path)
    job=store.submit(dict(strategy_name='Momentum_Rotation',symbols=['sh600096'],start_date='2024-01-01',end_date='2026-01-01'),{})
    monkeypatch.setattr(worker,'STOP',False)
    monkeypatch.setattr(worker,'live_ready',lambda:True)
    monkeypatch.setattr(worker,'host_headroom',lambda:True)
    calls=iter([False,True]);monkeypatch.setattr(worker,'protected_time',lambda:next(calls))
    killed=[];monkeypatch.setattr(worker.os,'killpg',lambda pid,sig:killed.append((pid,sig)))
    class Child:
        pid=123;returncode=-15
        def poll(self):return None if not worker.STOP else -15
        def wait(self,timeout=None):worker.STOP=True;return -15
    monkeypatch.setattr(worker.subprocess,'Popen',lambda *a,**k:Child())
    worker.supervise(store)
    assert killed and store.get(job['id'])['status']=='queued'
    assert store.get(job['id'])['error']=='deferred_for_live_strategy'

def test_strategy_refresh_or_test_mode_prevents_launch(monkeypatch):
    import json
    class Response:
        def __init__(self,data):self.data=json.dumps(data).encode()
        def read(self,*args):return self.data
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(Path,'read_text',lambda *a,**k:'{"api_token":"test"}')
    for value in [dict(worker_healthy=False),dict(worker_healthy=True,ignore_market_hours=True),dict(worker_healthy=True,data_quality={'refreshing':True}),dict(worker_healthy=True,is_trading_time=True)]:
        monkeypatch.setattr(worker.urllib.request,'urlopen',lambda *a,**k:Response(value))
        assert not worker.live_ready()
