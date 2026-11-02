import datetime as dt
import json
from unittest.mock import patch
import pytest
from compact_bars import CompactBars
from strategy_rules import evaluate_momentum, holding_signal, MomentumConfig
from test_momentum_backtest import fixture_data, engine, set_price
from backtest_jobs import JobStore, protected_time
from streaming_history import DiskList
from backtest_worker import write_result

@pytest.mark.parametrize('intraday',[False,True])
@pytest.mark.parametrize('refill',[False,True])
def test_streaming_full_result_matches_reference(tmp_path,intraday,refill):
    data,dates=fixture_data(210,('sh600096','sz000001','sh600519'))
    # Missing day, dated ST/sector, raw/adjusted divergence, partial fills.
    data['stocks']['sz000001']['bars'].pop(142)
    for i,b in enumerate(data['stocks']['sh600519']['bars']):
        b['industry']='a' if i<150 else 'b'
        b['is_st']=145<=i<=148
        if i>155: b['close']*=1.01;b['adjustment_factor']=1.01
        if i==160:set_price(b,18)
    config=MomentumConfig(max_positions=2,max_sector_weight=1,max_stock_weight=.4,
        risk_per_trade=0,volatility_target=0,rebalance_every=3,min_holding_days=0,
        refill_vacancies=refill,stop_loss_mode='intraday' if intraday else 'daily_close')
    expected=engine(data,dates,end=200,config=config).run()
    actual_engine=engine(data,dates,end=200,config=config,streaming=True,work_dir=tmp_path)
    try: assert actual_engine.run()==expected
    finally:actual_engine.close()

def test_spilled_results_are_lossless(tmp_path):
    data,dates=fixture_data()
    expected=engine(data,dates).run()
    e=engine(data,dates,streaming=True,spill_results=True,work_dir=tmp_path)
    try:
        result=e.run()
        assert isinstance(result['fills'],DiskList)
        target=tmp_path/'out.json';write_result(target,result)
        assert json.loads(target.read_text())==expected
    finally:e.close()

def test_compact_preserves_missing_types_and_signals():
    rows=[{'date':'x','a':None,'b':True,'c':2,'d':3.0,'large':2**60},{'date':'y','b':False,'c':4.5}]
    actual=CompactBars(rows)
    assert [dict(row) for row in actual]==rows
    assert type(actual[0]['c']) is int and type(actual[0]['d']) is float
    data,dates=fixture_data()
    bars=data['stocks']['sh600096']['bars']
    compact=CompactBars(bars,126)
    assert len(compact)==126
    assert evaluate_momentum(bars,dates[-1],MomentumConfig())==evaluate_momentum(compact,dates[-1],MomentumConfig())
    assert holding_signal(bars,dates[-1])==holding_signal(compact,dates[-1])

@pytest.mark.parametrize('stamp,expected', [('2026-11-02T07:59:59+08:00',False),('2026-11-02T08:00:00+08:00',True),('2026-11-02T12:00:00+08:00',True),('2026-11-02T16:59:59+08:00',True),('2026-11-02T17:00:00+08:00',False),('2026-11-07T12:00:00+08:00',False),('2030-01-01T10:00:00+08:00',True)])
def test_schedule_conservative(stamp,expected):
    assert protected_time(dt.datetime.fromisoformat(stamp))==expected

def test_queue_atomic_claim_cancel_recovery(tmp_path):
    store=JobStore(tmp_path)
    args=dict(strategy_name='Momentum_Rotation',symbols=['sh600096'],start_date='2024-01-01',end_date='2026-01-01')
    job=store.submit(args,{})
    with pytest.raises(RuntimeError):store.submit(args,{})
    assert store.claim()['id']==job['id']
    assert store.claim() is None
    store.recover();assert store.get(job['id'])['status']=='queued'
    store.cancel(job['id']);assert store.get(job['id'])['status']=='cancelled'
    with pytest.raises(ValueError):store.submit(dict(args,strategy_name='TurtleBreakout'),{})
