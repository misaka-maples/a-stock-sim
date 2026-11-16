"""Execute the real API function without importing application singletons."""
import ast
import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def handler(tmp_path,scope,progress,benchmark,clock=None,syncing=False):
    now=clock or datetime.datetime(2026,11,16,17,tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    class Clock:
        @classmethod
        def now(cls,tz=None):return now
    data_service=SimpleNamespace(get_benchmark_bars=Mock(return_value=benchmark))
    state={'data_storage':SimpleNamespace(get_all_sync_progress=lambda:progress,
             mem_cache=SimpleNamespace(get_stats=lambda:{})),
        'data_service':data_service,'ctx':SimpleNamespace(symbols=scope),
        'data_scheduler':SimpleNamespace(sync_symbols=scope,get_status=lambda:{'is_syncing':syncing}),
        'BASE_DIR':tmp_path,'BEIJING':now.tzinfo,
        'datetime':SimpleNamespace(datetime=Clock,time=datetime.time),
        'last_completed_trading_day':lambda _: '2026-11-16'}
    node=next(n for n in ast.parse(Path('server.py').read_text()).body
              if isinstance(n,ast.FunctionDef) and n.name=='get_data_system_status')
    node.decorator_list=[]
    exec(compile(ast.Module(body=[node],type_ignores=[]),'server.py','exec'),state)
    return state['get_data_system_status'],data_service


def bench(day='2026-11-16',count=120):
    end=datetime.date.fromisoformat(day)
    return [SimpleNamespace(trade_date=(end-datetime.timedelta(days=count-1-i)).isoformat()) for i in range(count)]


def test_status_uses_3185_trading_scope_not_history_metadata_or_index(tmp_path):
    scope=[f'sh60{i:04d}' for i in range(3185)]
    progress={s:SimpleNamespace(last_trade_date='2026-11-16') for s in scope[:-1]}
    progress.update({f'old-{i}':SimpleNamespace(last_trade_date='2026-11-16') for i in range(300)})
    progress['sh000300']=SimpleNamespace(last_trade_date='2026-11-16')
    get_status,service=handler(tmp_path,scope,progress,bench())
    result=get_status()
    assert result['total_symbols_tracked']==3185
    assert result['up_to_date_symbols']==3184
    assert result['missing_symbols']==[scope[-1]]
    assert result['coverage_pct']<100
    assert result['status']=='partial'
    service.get_benchmark_bars.assert_called_once_with('sh000300',auto_fetch=False)


def test_current_target_not_ready_before_scheduled_sync_or_with_old_benchmark(tmp_path):
    scope=['sh600000']
    clock=datetime.datetime(2026,11,16,15,10,tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    get_status,_=handler(tmp_path,scope,{},bench('2026-11-13'),clock)
    assert get_status()['status']=='waiting_for_scheduled_sync'
    progress={'sh600000':SimpleNamespace(last_trade_date='2026-11-16')}
    get_status,_=handler(tmp_path,scope,progress,bench('2026-11-13'))
    assert get_status()['status']!='ready'


def test_syncing_does_not_report_ready_even_if_all_files_exist(tmp_path):
    scope=['sh600000'];progress={'sh600000':SimpleNamespace(last_trade_date='2026-11-16')}
    get_status,_=handler(tmp_path,scope,progress,bench(),syncing=True)
    assert get_status()['status']=='updating'


def test_official_nontrading_exception_keeps_raw_coverage_and_is_excluded(tmp_path):
    scope=['sh600000','sh600001']
    progress={'sh600000':SimpleNamespace(last_trade_date='2026-11-16')}
    get_status,service=handler(tmp_path,scope,progress,bench())
    service.get_security_statuses=Mock(return_value={'sh600001':{'status':'SUSPENDED'}})
    result=get_status()
    assert result['status']=='ready_with_exclusions'
    assert result['coverage_pct']==50
    assert result['missing_symbols']==[]
    assert result['nontrading_symbols_count']==1
    assert result['excluded_from_new_buys']==['sh600001']
    assert result['data_complete'] is True
