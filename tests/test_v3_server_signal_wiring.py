"""Exercise the actual server polling function without importing application singletons."""
import ast
import datetime
import threading
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
    runtime=SimpleNamespace(step=Mock())
    ctx=SimpleNamespace(lock=threading.RLock(),account=account,strategy=strategy,symbols=[],
        last_quotes={},strategy_active=True,ignore_market_hours=False,trading_runtime=runtime)
    snapshot=SimpleNamespace(signal_asof_date='2026-11-13',market_breadth=.4,
                             sector_map=strategy.industry_map,coverage_ratio=1.)
    leading=Mock(return_value={'top_sectors':['机械'],'sector_scores':{'机械':10.}})
    monkeypatch.setattr(signal_inputs,'leading_industries',leading)
    clock=datetime.datetime(2026,11,16,14,tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    scope={'ctx':ctx,'datetime':datetime,'BEIJING':clock.tzinfo,'logger':Mock(),
        'MarketFeed':SimpleNamespace(is_trading_time=lambda now:True,fetch_quotes=lambda symbols:{}),
        'is_trading_day':lambda now:True,'sync_execution_settings':Mock(),
        'refresh_quote_freshness':Mock(),'last_completed_trading_day':lambda now:'2026-11-13',
        'data_scheduler':SimpleNamespace(get_latest_snapshot=lambda:snapshot,
                                        get_status=lambda:{'last_sync_result':None})}
    node=next(n for n in ast.parse(Path('server.py').read_text()).body
              if isinstance(n,ast.FunctionDef) and n.name=='worker_step')
    exec(compile(ast.Module(body=[node],type_ignores=[]),'server.py','exec'),scope)
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
    assert leading.call_count==2


def test_failed_v3_step_does_not_report_polling_success(monkeypatch):
    poll,clock,ctx,runtime,_=polling_fixture(monkeypatch)
    runtime.step.side_effect=RuntimeError('failed trading step')
    with pytest.raises(RuntimeError,match='failed trading step'):poll(clock)
    assert not hasattr(ctx,'worker_last_success')
