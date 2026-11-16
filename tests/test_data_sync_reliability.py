"""Daily ingestion must prove current coverage and retry incomplete runs."""
import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.astock.data.models import DailyBar
from src.astock.data.storage import LocalStorageManager
from src.astock.data.service import AStockDataService, _last_completed_session as completed_session
from src.astock.data.scheduler import DataScheduler
from src.astock.domain.enums import DataQuality, MarketRegime


@pytest.fixture(autouse=True)
def settled_clock(monkeypatch):
    monkeypatch.setattr('src.astock.data.service._last_completed_session',lambda:'2026-11-16')


def bar(symbol,day,source='test',final=True):
    return DailyBar(symbol=symbol,trade_date=day,open=10,high=11,low=9,close=10,
        volume=1000,amount=40000000,source=source,adjust_type='qfq',is_final=final,
        updated_at='2026-11-16T16:31:00')


def service(tmp_path,responses):
    storage=LocalStorageManager(tmp_path)
    providers=SimpleNamespace(fetch_stock_daily=Mock(side_effect=responses))
    svc=AStockDataService(storage=storage,providers=providers)
    end=datetime.date(2026,11,16)
    bench=[bar('sh000300',(end-datetime.timedelta(days=149-i)).isoformat()) for i in range(150)]
    storage.write_daily_bars('sh000300',bench,adjust_type='none')
    return svc,providers


def test_incremental_catches_gap_and_counts_only_current_final_bar(tmp_path):
    svc,providers=service(tmp_path,[[bar('sh600000','2026-11-10'),bar('sh600000','2026-11-11'),bar('sh600000','2026-11-12'),bar('sh600000','2026-11-13'),bar('sh600000','2026-11-16')]])
    svc.storage.write_daily_bars('sh600000',[bar('sh600000','2026-11-10'),bar('sh600000','2026-11-11'),bar('sh600000','2026-11-12')])
    result=svc.sync_daily_increment('2026-11-16',symbols=['sh600000'])
    assert providers.fetch_stock_daily.call_args.args[1:]==('2026-11-02','2026-11-16')
    assert result['status']=='COMPLETED'
    assert result['covered_symbols']==1 and result['coverage']==1
    assert [b.trade_date for b in svc.storage.read_daily_bars('sh600000')]==['2026-11-10','2026-11-11','2026-11-12','2026-11-13','2026-11-16']


@pytest.mark.parametrize('rows,error',[([], 'empty_or_nonfinal_provider_response'),
    ([bar('sh600000','2026-11-10'),bar('sh600000','2026-11-11'),bar('sh600000','2026-11-12'),bar('sh600000','2026-11-13')], 'target_day_missing'),
    ([bar('sh600000','2026-11-16',final=False)],'empty_or_nonfinal_provider_response')])
def test_empty_stale_or_nonfinal_data_never_marks_success(tmp_path,rows,error):
    svc,_=service(tmp_path,[rows])
    svc.storage.write_daily_bars('sh600000',[bar('sh600000','2026-11-10'),bar('sh600000','2026-11-11'),bar('sh600000','2026-11-12')])
    result=svc.sync_daily_increment('2026-11-16',symbols=['sh600000'])
    progress=svc.storage.get_sync_progress('sh600000')
    assert result['status']=='PARTIAL' and result['fail_count']==1
    assert result['coverage']==0
    assert progress.last_trade_date<'2026-11-16'
    assert progress.last_error==error and progress.failed_attempts==1


def test_explicit_scope_does_not_query_historical_codes_and_new_name_gets_warmup(tmp_path):
    svc,providers=service(tmp_path,[[bar('sh600000','2026-11-16')]])
    svc.get_universe_symbols=Mock(return_value=['sh600000','sh600001'])
    result=svc.sync_daily_increment('2026-11-16',symbols=['sh600000'])
    assert result['total_symbols']==1
    assert providers.fetch_stock_daily.call_count==1
    args=providers.fetch_stock_daily.call_args.args
    assert args[0]=='sh600000' and args[1]=='2025-10-12'
    svc.get_universe_symbols.assert_not_called()


def test_benchmark_missing_is_a_failed_acceptance_not_swallowed(tmp_path):
    svc,_=service(tmp_path,[])
    svc.storage.read_daily_bars=Mock(return_value=[])
    svc.get_benchmark_bars=Mock(side_effect=RuntimeError('provider down'))
    result=svc.sync_daily_increment('2026-11-16',symbols=['sh600000'],max_workers=1)
    assert not result['benchmark_valid']
    assert result['benchmark_error']=='RuntimeError'
    assert result['status']=='PARTIAL'


def fake_service():
    end=datetime.date(2026,11,16)
    snap=SimpleNamespace(benchmark_bars=[SimpleNamespace(date=(end-datetime.timedelta(days=119-i)).isoformat()) for i in range(120)],
        market_regime=MarketRegime.RISK_OFF,market_breadth=.3,quality=DataQuality.VALID,universe_version='v3')
    svc=SimpleNamespace(sync_daily_increment=Mock(return_value={'status':'PARTIAL','fail_count':1,'coverage':.5}),
        get_universe_symbols=Mock(return_value=['sh600000','sh600001']),compute_market_snapshot=Mock(return_value=snap))
    return svc,snap


def test_partial_run_retries_with_bounded_backoff_and_does_not_publish(monkeypatch):
    clock=[1000.]
    monkeypatch.setattr('src.astock.data.scheduler.time.monotonic',lambda:clock[0])
    svc,snap=fake_service();callback=Mock()
    scheduler=DataScheduler(svc,on_pipeline_completed=callback,sync_symbols=['sh600000'])
    for delay in [300,900,1800,1800]:
        result=scheduler.run_daily_pipeline('2026-11-16')
        assert result['status']=='PARTIAL'
        assert scheduler.last_sync_date is None
        assert scheduler.last_attempt_date=='2026-11-16'
        assert scheduler.get_latest_snapshot() is None
        assert not scheduler.should_run('2026-11-16')
        assert scheduler.get_status()['next_retry_seconds']==delay
        clock[0]+=delay
        assert scheduler.should_run('2026-11-16')
    callback.assert_not_called()
    assert svc.sync_daily_increment.call_args.kwargs=={'symbols':['sh600000']}
    assert svc.compute_market_snapshot.call_args.kwargs=={'watchlist':['sh600000']}
    # Successful retry publishes once and ends retries for this date.
    svc.sync_daily_increment.return_value={'status':'COMPLETED','fail_count':0,'coverage':1}
    result=scheduler.run_daily_pipeline('2026-11-16')
    assert result['status']=='SUCCESS'
    assert scheduler.last_sync_date=='2026-11-16'
    assert scheduler.get_latest_snapshot() is snap
    callback.assert_called_once_with('2026-11-16',snap)
    assert not scheduler.should_run('2026-11-16')
    assert scheduler.should_run('2026-11-17')


def test_pipeline_exception_retries_and_preserves_previous_accepted_snapshot(monkeypatch):
    clock=[10.]
    monkeypatch.setattr('src.astock.data.scheduler.time.monotonic',lambda:clock[0])
    svc,snap=fake_service();scheduler=DataScheduler(svc)
    scheduler.latest_snapshot=snap;scheduler.last_sync_date='2026-11-13'
    svc.sync_daily_increment.side_effect=RuntimeError('unreachable')
    result=scheduler.run_daily_pipeline('2026-11-16')
    assert result['status']=='FAILED'
    assert scheduler.last_sync_date=='2026-11-13'
    assert scheduler.get_latest_snapshot() is snap
    assert not scheduler.should_run('2026-11-16')
    clock[0]+=300
    assert scheduler.should_run('2026-11-16')


def test_existing_short_benchmark_fetches_warmup_not_just_one_more_bar(tmp_path):
    storage=LocalStorageManager(tmp_path)
    storage.write_daily_bars('sh000300',[bar('sh000300','2026-11-13')],adjust_type='none')
    end=datetime.date(2026,11,16)
    full=[bar('sh000300',(end-datetime.timedelta(days=149-i)).isoformat()) for i in range(150)]
    providers=SimpleNamespace(fetch_index_daily=Mock(return_value=full))
    svc=AStockDataService(storage=storage,providers=providers)
    result=svc.get_benchmark_bars('000300',start_date='2025-10-12',end_date='2026-11-16')
    assert len(result)>=120
    assert providers.fetch_index_daily.call_args.args==('sh000300','2025-10-12','2026-11-16')


def test_daily_sync_rejects_unsettled_target_before_any_fetch(tmp_path,monkeypatch):
    svc,providers=service(tmp_path,[])
    monkeypatch.setattr('src.astock.data.service._last_completed_session',lambda:'2026-11-13')
    with pytest.raises(ValueError,match='target_session_not_completed'):
        svc.sync_daily_increment('2026-11-16',symbols=['sh600000'])
    providers.fetch_stock_daily.assert_not_called()


def test_daily_sync_rejects_nontrading_day_before_any_fetch(tmp_path):
    svc,providers=service(tmp_path,[])
    with pytest.raises(ValueError,match='target_date_not_a_trading_session'):
        svc.sync_daily_increment('2026-11-15',symbols=['sh600000'])
    providers.fetch_stock_daily.assert_not_called()


def test_complete_daily_bar_clock_uses_beijing_cutoff():
    from trade_calendar import BEIJING
    assert completed_session(datetime.datetime(2026,11,16,15,4,tzinfo=BEIJING))=='2026-11-13'
    assert completed_session(datetime.datetime(2026,11,16,15,5,tzinfo=BEIJING))=='2026-11-16'
    assert completed_session(datetime.datetime(2026,11,15,18,tzinfo=BEIJING))=='2026-11-13'
