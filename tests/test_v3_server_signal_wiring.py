"""Exercise the actual server polling function without importing application singletons."""
import ast
import datetime
import threading
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from src.astock.trade.strategy import signal_inputs


def polling_fixture(monkeypatch):
    account=SimpleNamespace(positions={},strategy_state={},update_market_prices=Mock(),save_if_due=Mock())
    strategy=SimpleNamespace(prepare=Mock(),preview_candidates=Mock(return_value=[]),
        latest_candidates=[{'symbol':'sh600001','current':12.0,'ret_20':10.,'ret_60':20.}],
        _benchmark=[{'date':'2026-11-13','close':4000.}],history_cache={},
        industry_map={'sh600001':'机械'},watchlist=['sh600001'],_stock_names={},_history_revision=1)
    runtime=SimpleNamespace(step=Mock(),ledger=SimpleNamespace(get_positions=lambda _:{}))
    ctx=SimpleNamespace(lock=threading.RLock(),account=account,strategy=strategy,symbols=[],
        last_quotes={},strategy_active=True,ignore_market_hours=False,trading_runtime=runtime)
    snapshot=SimpleNamespace(snapshot_id='snapshot-1',signal_asof_date='2026-11-13',market_breadth=.4,
                             sector_map=strategy.industry_map,coverage_ratio=1.)
    leading=Mock(return_value={'top_sectors':['机械'],'sector_scores':{'机械':10.}})
    monkeypatch.setattr(signal_inputs,'leading_industries',leading)
    clock=datetime.datetime(2026,11,16,14,tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    scope={'ctx':ctx,'datetime':datetime,'BEIJING':clock.tzinfo,'logger':Mock(),'json':json,'BASE_DIR':Path('.'),
        'data_service':SimpleNamespace(get_bars=Mock(return_value=[])),
        'MarketFeed':SimpleNamespace(is_trading_time=lambda now:True,fetch_quotes=lambda symbols:{}),
        'is_trading_day':lambda now:True,'sync_execution_settings':Mock(),
        'refresh_quote_freshness':Mock(),'last_completed_trading_day':lambda now:'2026-11-13',
        'data_scheduler':SimpleNamespace(get_latest_snapshot=lambda:snapshot,
                                        get_status=lambda:{'last_sync_result':None})}
    names={'worker_step','_tracked_holding_symbols','_holding_position_indicators','_v3_execution_policies',
           '_v3_industry_screen'}
    nodes=[n for n in ast.parse(Path('server.py').read_text()).body
           if isinstance(n,ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes}==names
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'server.py','exec'),scope)
    return scope['worker_step'],clock,ctx,runtime,leading


def test_server_passes_dated_industry_and_history_evidence(monkeypatch):
    poll,clock,ctx,runtime,leading=polling_fixture(monkeypatch)
    poll(clock);poll(clock)
    assert leading.call_count==1  # No full-universe rescan every three seconds.
    data=runtime.step.call_args.kwargs['market_data']
    assert data['industry_map']=={'sh600001':'机械'}
    assert data['top_sectors']==['机械']
    assert data['sector_scores']=={'机械':10.}
    assert data['candidate_bars'] is ctx.strategy.history_cache
    assert data['signal_asof_date']=='2026-11-13'
    assert data['candidates'][0]['current']==12.0
    ctx.strategy._history_revision+=1
    poll(clock)
    assert leading.call_count==1  # Loading an individual history is not a new screen.
    poll.__globals__['data_scheduler'].get_latest_snapshot().snapshot_id='snapshot-2'
    poll(clock)
    assert leading.call_count==2


def test_published_same_day_screen_avoids_full_pool_scan(monkeypatch):
    poll,clock,ctx,runtime,leading=polling_fixture(monkeypatch)
    ctx.strategy.watchlist=[f'sh{i:06d}' for i in range(3185)]
    published={'signal_asof_date':'2026-11-13','top_sectors':['医药'],
               'sector_scores':{'医药':11.},'industry_stats':{}}
    snapshot=poll.__globals__['data_scheduler'].get_latest_snapshot()
    snapshot.source_lineage={'industry_screen':published}
    poll(clock)
    leading.assert_not_called()
    assert runtime.step.call_args.kwargs['market_data']['top_sectors']==['医药']
    assert runtime.step.call_args.kwargs['market_data']['industry_screen_status']=='READY'


def test_missing_or_wrong_day_full_pool_screen_does_not_delay_holding_exits(monkeypatch):
    poll,clock,ctx,runtime,leading=polling_fixture(monkeypatch)
    ctx.strategy.watchlist=[f'sh{i:06d}' for i in range(3185)]
    snapshot=poll.__globals__['data_scheduler'].get_latest_snapshot()
    snapshot.source_lineage={'industry_screen':{'signal_asof_date':'2026-11-12',
        'top_sectors':['机械'],'sector_scores':{'机械':10.}}}
    poll(clock)
    leading.assert_not_called()
    runtime.step.assert_called_once()  # Exits and other independent guards still run.
    data=runtime.step.call_args.kwargs['market_data']
    assert data['top_sectors']==[]
    assert data['sector_scores']=={}
    assert data['industry_screen_status']=='AWAITING_SNAPSHOT_SCREEN'
    # Background publication can supply a screen even if the snapshot ID is stable.
    snapshot.source_lineage['industry_screen']['signal_asof_date']='2026-11-13'
    poll(clock)
    assert runtime.step.call_args.kwargs['market_data']['top_sectors']==['机械']


def test_valid_empty_published_screen_is_not_replaced_by_fallback(monkeypatch):
    poll,clock,_,runtime,leading=polling_fixture(monkeypatch)
    snapshot=poll.__globals__['data_scheduler'].get_latest_snapshot()
    snapshot.source_lineage={'industry_screen':{'signal_asof_date':'2026-11-13',
        'top_sectors':[],'sector_scores':{}}}
    poll(clock)
    leading.assert_not_called()
    assert runtime.step.call_args.kwargs['market_data']['top_sectors']==[]


def test_failed_v3_step_does_not_report_polling_success(monkeypatch):
    poll,clock,ctx,runtime,_=polling_fixture(monkeypatch)
    runtime.step.side_effect=RuntimeError('failed trading step')
    with pytest.raises(RuntimeError,match='failed trading step'):poll(clock)
    assert not hasattr(ctx,'worker_last_success')
