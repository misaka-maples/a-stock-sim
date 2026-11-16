"""Exercise server wiring without importing live application singletons."""
import ast
import datetime
import sys
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def load_helpers(scope):
    tree=ast.parse(Path('server.py').read_text())
    names={'_tracked_holding_symbols','_holding_position_indicators','_v3_execution_policies',
           '_v3_industry_screen','worker_step'}
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes}==names
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'server.py','exec'),scope)
    return scope


class HoldingCache(dict):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.pinned=[];self.registered=[]
    def pin(self,symbols):self.pinned.extend(symbols)
    def register_known_symbols(self,symbols):self.registered.extend(symbols)


def setup_polling(monkeypatch):
    now=datetime.datetime(2026,11,17,9,31,tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    held={'balanced':{'sh600261':object()},'aggressive':{'sz000831':object()}}
    history=HoldingCache({s:[{'date':'2026-11-16','close':10,'raw_close':20,'adjustment_factor':.5}]
                          for s in ['sh600261','sz000831']})
    account=SimpleNamespace(positions=held['balanced'],strategy_state={},update_market_prices=Mock(),save_if_due=Mock())
    strategy=SimpleNamespace(prepare=Mock(),preview_candidates=Mock(return_value=[]),latest_candidates=[],
        history_cache=history,_benchmark=[],industry_map={},watchlist=[],_stock_names={},_history_revision=1)
    runtime=SimpleNamespace(ledger=SimpleNamespace(get_positions=lambda acc:held[acc]),step=Mock())
    ctx=SimpleNamespace(lock=threading.RLock(),account=account,strategy=strategy,symbols=[],
        last_quotes={},strategy_active=True,ignore_market_hours=False,trading_runtime=runtime)
    fetched=Mock(side_effect=lambda symbols:{s:{'current':10,'fresh':True} for s in symbols})
    snapshot=SimpleNamespace(signal_asof_date='2026-11-16',market_breadth=.4,sector_map={},
        source_lineage={'coverage_ratio':1})
    calc=Mock(side_effect=lambda bars,asof:{'signal_asof':asof,'atr':.8,'ma20':20,
            'consecutive_below_ma20':0} if bars and bars[-1].get('date')==asof else None)
    monkeypatch.setitem(sys.modules,'src.astock.trade.market_inputs',SimpleNamespace(completed_position_indicators=calc))
    monkeypatch.setitem(sys.modules,'src.astock.trade.execution_policy',SimpleNamespace(
        load_execution_policies=Mock(return_value={'balanced':{'rebalance_every':5},'aggressive':{'rebalance_every':5}})))
    from src.astock.trade.strategy import signal_inputs
    monkeypatch.setattr(signal_inputs,'leading_industries',Mock(return_value={'top_sectors':[],'sector_scores':{}}))
    service=SimpleNamespace(get_bars=Mock(return_value=[]))
    scope=load_helpers({'ctx':ctx,'datetime':datetime,'BEIJING':now.tzinfo,'logger':Mock(),'json':json,'BASE_DIR':Path('.'),
        'MarketFeed':SimpleNamespace(is_trading_time=lambda _:True,fetch_quotes=fetched),
        'is_trading_day':lambda _:True,'sync_execution_settings':Mock(),
        'refresh_quote_freshness':Mock(),'last_completed_trading_day':lambda _:'2026-11-16',
        '_finite_quote':lambda q:q,'data_service':service,
        'data_scheduler':SimpleNamespace(get_latest_snapshot=lambda:snapshot,get_status=lambda:{'last_sync_result':{'status':'SUCCESS'}})})
    return scope,now,ctx,runtime,fetched,calc,service


def test_aggressive_only_holding_receives_quotes_history_and_indicators(monkeypatch):
    scope,now,ctx,runtime,fetched,calc,service=setup_polling(monkeypatch)
    scope['worker_step'](now)
    assert fetched.call_args.args[0]==['sh600261','sz000831']
    assert set(ctx.strategy.history_cache.registered)=={'sh600261','sz000831'}
    assert set(ctx.strategy.history_cache.pinned)=={'sh600261','sz000831'}
    inputs=runtime.step.call_args.kwargs['market_data']['position_indicators']
    assert set(inputs)=={'sh600261','sz000831'}
    assert inputs['sz000831']['signal_asof']=='2026-11-16'
    assert inputs['sz000831']['atr']==.8
    assert runtime.step.call_args.kwargs['market_data']['execution_policy']==runtime.execution_policies
    # The legacy positions view remains the balanced account only.
    assert set(ctx.account.positions)=={'sh600261'}
    service.get_bars.assert_not_called()


def test_cold_aggressive_history_uses_readonly_local_storage(monkeypatch):
    scope,_,ctx,_,_,calc,service=setup_polling(monkeypatch)
    ctx.strategy.history_cache.pop('sz000831')
    stored=SimpleNamespace(to_legacy_dict=lambda:{'date':'2026-11-16','close':10})
    service.get_bars.return_value=[stored]
    result=scope['_holding_position_indicators'](['sz000831'],ctx.strategy,'2026-11-16')
    assert result['sz000831']['signal_asof']=='2026-11-16'
    service.get_bars.assert_called_once_with('sz000831',end_date='2026-11-16',auto_fetch=False)
    assert calc.call_args.args[0]==[{'date':'2026-11-16','close':10}]


def test_missing_history_does_not_fabricate_position_indicators(monkeypatch):
    scope,_,ctx,_,_,_,service=setup_polling(monkeypatch)
    ctx.strategy.history_cache.clear()
    assert scope['_holding_position_indicators'](['sz000831'],ctx.strategy,'2026-11-16')=={}
    service.get_bars.assert_called_once_with('sz000831',end_date='2026-11-16',auto_fetch=False)


def test_legacy_only_account_keeps_original_holding_subscription(monkeypatch):
    scope,_,ctx,_,_,_,_=setup_polling(monkeypatch)
    assert scope['_tracked_holding_symbols'](ctx.account,None)==['sh600261']


def test_profile_configuration_failure_does_not_invent_defaults(monkeypatch):
    scope,_,ctx,runtime,_,_,_=setup_polling(monkeypatch)
    module=sys.modules['src.astock.trade.execution_policy']
    module.load_execution_policies.side_effect=ValueError('invalid execution profile')
    with pytest.raises(ValueError,match='invalid execution profile'):
        scope['_v3_execution_policies'](ctx.strategy,runtime)
    assert not hasattr(runtime,'execution_policies')


def test_validated_profiles_are_cached_until_primary_parameters_change(monkeypatch):
    scope,_,ctx,runtime,_,_,_=setup_polling(monkeypatch)
    ctx.strategy.config=SimpleNamespace(rebalance_every=5,refill_vacancies=False)
    load=sys.modules['src.astock.trade.execution_policy'].load_execution_policies
    scope['_v3_execution_policies'](ctx.strategy,runtime)
    scope['_v3_execution_policies'](ctx.strategy,runtime)
    assert load.call_count==1
    assert load.call_args.kwargs['primary_parameters']['refill_vacancies'] is False
    ctx.strategy.config.rebalance_every=6
    scope['_v3_execution_policies'](ctx.strategy,runtime)
    assert load.call_count==2


def test_actual_completed_indicators_retain_raw_price_basis_and_ignore_future(monkeypatch):
    from src.astock.trade.market_inputs import completed_position_indicators
    scope,_,ctx,_,_,_,service=setup_polling(monkeypatch)
    sys.modules['src.astock.trade.market_inputs'].completed_position_indicators=completed_position_indicators
    end=datetime.date(2026,11,16)
    rows=[{'date':(end-datetime.timedelta(days=29-i)).isoformat(),'close':10,
           'raw_close':20,'high':11,'low':9,'adjustment_factor':.5,'is_final':True}
          for i in range(30)]
    rows.append({'date':'2026-11-17','close':1000,'raw_close':2000,'high':1100,
                 'low':900,'adjustment_factor':.5,'is_final':True})
    ctx.strategy.history_cache['sz000831']=rows
    result=scope['_holding_position_indicators'](['sz000831'],ctx.strategy,'2026-11-16')['sz000831']
    assert result['atr']==pytest.approx(4.)
    assert result['ma20']==pytest.approx(20.)
    assert result['signal_asof']=='2026-11-16'
    assert result['consecutive_below_ma20']==0
    service.get_bars.assert_not_called()


def test_runtime_configure_method_is_called_when_profiles_load(monkeypatch):
    scope,_,ctx,runtime,_,_,_=setup_polling(monkeypatch)
    runtime.configure_execution_policies=Mock(side_effect=lambda p:setattr(runtime,'execution_policies',p))
    result=scope['_v3_execution_policies'](ctx.strategy,runtime)
    runtime.configure_execution_policies.assert_called_once_with(result)


def test_quote_freshness_is_rechecked_after_preparation_before_live_step(monkeypatch):
    scope,now,ctx,runtime,_,_,_=setup_polling(monkeypatch)
    later=now+datetime.timedelta(minutes=3)
    ticks=iter([now,now,later])
    scope['datetime']=SimpleNamespace(datetime=SimpleNamespace(now=lambda _:next(ticks)),time=datetime.time)
    def refresh(at):
        for quote in ctx.last_quotes.values():
            quote['fresh']=(at-now).total_seconds()<=120
    scope['refresh_quote_freshness']=Mock(side_effect=refresh)
    scope['worker_step']()
    assert runtime.step.call_args.kwargs['now']==later
    assert all(not q['fresh'] for q in runtime.step.call_args.kwargs['quotes'].values())
    assert scope['refresh_quote_freshness'].call_args.args==(later,)


def test_preparation_crossing_market_close_does_not_execute_orders(monkeypatch):
    scope,_,ctx,runtime,_,_,_=setup_polling(monkeypatch)
    before=datetime.datetime(2026,11,17,14,59,59,tzinfo=scope['BEIJING'])
    after=before+datetime.timedelta(seconds=2)
    ticks=iter([before,before,after])
    scope['datetime']=SimpleNamespace(datetime=SimpleNamespace(now=lambda _:next(ticks)),time=datetime.time)
    scope['MarketFeed'].is_trading_time=lambda at:at.time()<datetime.time(15)
    scope['worker_step']()
    runtime.step.assert_not_called()
    assert ctx.worker_last_success==after
    assert scope['refresh_quote_freshness'].call_args.args==(after,)


def test_supplied_clock_stays_deterministic_after_preparation(monkeypatch):
    scope,now,_,runtime,_,_,_=setup_polling(monkeypatch)
    scope['datetime']=SimpleNamespace(datetime=SimpleNamespace(now=Mock(side_effect=AssertionError('wall clock used'))),
                                      time=datetime.time)
    scope['worker_step'](now)
    assert runtime.step.call_args.kwargs['now']==now
    assert scope['refresh_quote_freshness'].call_count==2
