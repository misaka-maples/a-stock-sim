"""Causal multi-day resumption recovery using isolated Parquet/SQLite ledgers.

Provider fixtures have Tencent's sparse real trading dates; existing suspension
placeholders are verified BaoStock-style rows. No remote services are touched.
"""
import datetime
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.astock.data.models import DailyBar
from src.astock.data.storage import LocalStorageManager
from src.astock.data.service import AStockDataService
from src.astock.data.scheduler import DataScheduler
from src.astock.data.security_status import load_nontrading_statuses
from trade_calendar import is_trading_day

TARGET='sz000831'
CONTROL=[f'sh6000{i:02d}' for i in range(12)]
ASOF='2026-11-16'


def sessions(start,end):
    cursor=datetime.date.fromisoformat(start);stop=datetime.date.fromisoformat(end)
    out=[]
    while cursor<=stop:
        if is_trading_day(cursor):out.append(cursor.isoformat())
        cursor+=datetime.timedelta(days=1)
    return out


def trade(symbol,day,index=0,source='baostock'):
    price=10+index*.01
    return DailyBar(symbol=symbol,trade_date=day,open=price-.01,high=price+.02,
        low=price-.02,close=price,raw_open=price-.01,raw_high=price+.02,
        raw_low=price-.02,raw_close=price,volume=1000.,amount=50000000.,
        adjustment_factor=1.,source=source,adjust_type='qfq',is_final=True,
        is_suspended=False,is_st=False,updated_at=day+'T17:00:00')


def suspension(symbol,day,anchor):
    return DailyBar(symbol=symbol,trade_date=day,open=anchor.close,high=anchor.close,
        low=anchor.close,close=anchor.close,raw_open=anchor.raw_close,
        raw_high=anchor.raw_close,raw_low=anchor.raw_close,raw_close=anchor.raw_close,
        volume=0.,amount=0.,source='baostock',adjust_type='qfq',is_final=True,
        adjustment_factor=anchor.adjustment_factor,is_suspended=True,
        updated_at=day+'T17:00:00')


def evidence(symbol=TARGET,status='SUSPENDED',verified='2026-11-16'):
    return {'symbol':symbol,'status':status,'announcement_date':'2026-11-01',
            'effective_from':'2026-11-02','verified_through':verified,
            'source_url':'https://static.cninfo.com.cn/finalpage/2026-11-01/test-only.PDF',
            'title':'TEST fixture dated suspension evidence',
            'evidence_summary':'Simulated official-format evidence; no external data assertion.'}


class SparseProvider:
    def __init__(self,rows):self.rows=rows;self.calls=[]
    def fetch_stock_daily(self,symbol,start,end,adjust_type='qfq'):
        self.calls.append((symbol,start,end))
        return [replace(b,source='tencent',updated_at=end+'T17:00:00')
                for b in self.rows[symbol] if start<=b.trade_date<=end]


def make_system(tmp_path,monkeypatch,resumes):
    end=max(resumes,'2026-11-20')
    monkeypatch.setattr('src.astock.data.service._last_completed_session',lambda:end)
    project=tmp_path/'project';(project/'configs').mkdir(parents=True)
    path=project/'configs/security_status_evidence.json'
    path.write_text(json.dumps({'version':1,'records':[evidence()]}))
    monkeypatch.setattr('src.astock.data.service.BASE_DIR',project)
    store=LocalStorageManager(tmp_path/'isolated_storage')
    all_dates=sessions('2025-10-01',end)
    seed_dates=[d for d in all_dates if d<='2026-11-13'][-150:]
    rows={}
    for symbol in CONTROL:
        rows[symbol]=[trade(symbol,d,i) for i,d in enumerate(all_dates)]
        store.write_daily_bars(symbol,[b for b in rows[symbol] if b.trade_date in seed_dates])
    target_actual=[trade(TARGET,d,i) for i,d in enumerate(seed_dates) if d<='2026-10-30']
    anchor=target_actual[-1]
    old=target_actual+[suspension(TARGET,d,anchor) for d in seed_dates if '2026-11-02'<=d<='2026-11-13']
    store.write_daily_bars(TARGET,old)
    # Resumption first has a genuine traded bar; suspended sessions have none.
    rows[TARGET]=target_actual+[trade(TARGET,d,len(target_actual)+i)
        for i,d in enumerate(all_dates) if d>=resumes]
    bench=[replace(trade('sh000300',d,i),adjust_type='none') for i,d in enumerate(all_dates)]
    store.write_daily_bars('sh000300',bench,adjust_type='none')
    provider=SparseProvider(rows)
    svc=AStockDataService(storage=store,providers=provider)
    svc.get_industry_map=lambda:{s:'test-sector' for s in CONTROL+[TARGET]}
    callback=Mock()
    scheduler=DataScheduler(svc,on_pipeline_completed=callback,sync_symbols=CONTROL+[TARGET])
    return svc,store,provider,scheduler,path


@pytest.mark.parametrize('resume',['2026-11-17','2026-11-20','2026-12-15'])
def test_suspension_expires_and_resumption_is_automatically_reingested(tmp_path,monkeypatch,resume):
    svc,store,provider,scheduler,path=make_system(tmp_path,monkeypatch,resume)
    first=scheduler.run_daily_pipeline(ASOF)
    assert first['status']=='SUCCESS'
    assert first['nontrading_statuses'][TARGET]['status']=='SUSPENDED'
    assert TARGET in scheduler.get_latest_snapshot().source_lineage['excluded_symbols']
    assert not any(sym==TARGET and end==ASOF for sym,start,end in provider.calls)
    original_snapshot=scheduler.get_latest_snapshot()
    original_flags=[(b.trade_date,b.volume,b.is_suspended) for b in store.read_daily_bars(TARGET)]
    assert svc.get_security_statuses('2026-11-17',[TARGET])=={}
    assert scheduler.should_run('2026-11-17') is True

    if resume>'2026-11-17':
        # A longer-than-verified suspension becomes an explicit retrying gap,
        # rather than a permanent blacklist or a fabricated daily candle.
        for day in ['2026-11-17','2026-11-18','2026-11-19']:
            if day>=resume:break
            assert scheduler.should_run(day) is True
            partial=scheduler.run_daily_pipeline(day)
            assert partial['status']=='PARTIAL'
            assert partial['missing_symbols']==[TARGET]
            assert TARGET in scheduler.get_latest_snapshot().source_lineage['excluded_symbols']
            assert partial['nontrading_statuses']=={}
            assert store.get_sync_progress(TARGET).last_trade_date=='2026-11-13'
            assert not scheduler.should_run(day)  # bounded retry delay, no hot loop
            assert [s for s in CONTROL if store.get_sync_progress(s).last_trade_date!=day]==[]

    assert scheduler.should_run(resume) is True
    resumed=scheduler.run_daily_pipeline(resume)
    assert resumed['status']=='SUCCESS'
    assert resumed['missing_symbols']==[] and resumed['nontrading_statuses']=={}
    assert store.get_sync_progress(TARGET).last_trade_date==resume
    latest=scheduler.get_latest_snapshot()
    assert latest.signal_asof_date==resume
    assert TARGET not in latest.source_lineage['excluded_symbols']
    assert TARGET in original_snapshot.source_lineage['excluded_symbols']  # historical snapshot stays causal
    assert svc.get_market_snapshot(resume).snapshot_id==latest.snapshot_id
    assert svc.get_market_snapshot(ASOF).snapshot_id==original_snapshot.snapshot_id
    assert scheduler.last_sync_date==resume
    assert not scheduler.should_run(resume)  # completed day remains idempotent
    target_calls=[c for c in provider.calls if c[0]==TARGET and c[2]==resume]
    assert target_calls and target_calls[-1][1]<='2026-10-28'
    after={b.trade_date:b for b in store.read_daily_bars(TARGET)}
    for day,volume,flag in original_flags:
        if flag:
            assert after[day].is_suspended is True and after[day].volume==volume==0
    assert all(store.get_sync_progress(s).last_trade_date==resume for s in CONTROL)
    # Neither the provider nor the date evidence is modified as a blacklist.
    assert json.loads(path.read_text())['records'][0]['verified_through']=='2026-11-16'


def test_security_status_is_read_fresh_and_expired_halt_is_not_cached(tmp_path):
    path=tmp_path/'evidence.json'
    path.write_text(json.dumps({'version':1,'records':[evidence()]}))
    assert TARGET in load_nontrading_statuses(path,'2026-11-16',[TARGET])
    assert TARGET not in load_nontrading_statuses(path,'2026-11-17',[TARGET])
    path.write_text(json.dumps({'version':1,'records':[evidence(verified='2026-11-19')]}))
    assert TARGET in load_nontrading_statuses(path,'2026-11-19',[TARGET])
    assert TARGET not in load_nontrading_statuses(path,'2026-11-20',[TARGET])


def test_definitive_delisting_is_not_confused_with_temporary_suspension(tmp_path):
    path=tmp_path/'evidence.json'
    path.write_text(json.dumps({'version':1,'records':[evidence(status='DELISTED')]}))
    assert load_nontrading_statuses(path,'2026-11-20',[TARGET])[TARGET]['status']=='DELISTED'


def status_file(path,records):
    path.write_text(json.dumps({'version':1,'records':records}))


def resume_record(effective='2026-11-17',announced='2026-11-17'):
    return {**evidence(status='RESUMED',verified=effective),
            'effective_from':effective,'announcement_date':announced,
            'title':'TEST fixture effective resumption evidence'}


@pytest.mark.parametrize('reverse',[False,True])
def test_effective_same_day_resumption_overrides_older_still_valid_halt(tmp_path,reverse):
    path=tmp_path/'evidence.json'
    rows=[evidence(verified='2026-11-20'),resume_record()]
    status_file(path,list(reversed(rows)) if reverse else rows)
    assert load_nontrading_statuses(path,'2026-11-16',[TARGET])[TARGET]['status']=='SUSPENDED'
    assert TARGET not in load_nontrading_statuses(path,'2026-11-17',[TARGET])
    assert TARGET not in load_nontrading_statuses(path,'2026-11-20',[TARGET])


@pytest.mark.parametrize('effective,announced',[
    ('2026-11-18','2026-11-17'),('2026-11-17','2026-11-18')])
def test_future_effective_or_undisclosed_resumption_never_enters_history(tmp_path,effective,announced):
    path=tmp_path/'evidence.json'
    status_file(path,[evidence(verified='2026-11-20'),resume_record(effective,announced)])
    assert load_nontrading_statuses(path,'2026-11-17',[TARGET])[TARGET]['status']=='SUSPENDED'
    assert TARGET not in load_nontrading_statuses(path,'2026-11-18',[TARGET])
    # Requery an earlier as_of after the later query: no persistent mutation/cache.
    assert load_nontrading_statuses(path,'2026-11-16',[TARGET])[TARGET]['status']=='SUSPENDED'


@pytest.mark.parametrize('reverse',[False,True])
def test_resumption_cannot_cancel_definitive_delisting(tmp_path,reverse):
    path=tmp_path/'evidence.json'
    rows=[evidence(status='DELISTED'),resume_record()]
    status_file(path,list(reversed(rows)) if reverse else rows)
    assert load_nontrading_statuses(path,'2026-11-20',[TARGET])[TARGET]['status']=='DELISTED'


@pytest.mark.parametrize('reverse',[False,True])
def test_new_suspension_after_resumption_is_still_recognized(tmp_path,reverse):
    path=tmp_path/'evidence.json'
    newer_halt={**evidence(verified='2026-11-20'),'effective_from':'2026-11-18',
                'announcement_date':'2026-11-17','title':'TEST fixture second suspension'}
    rows=[evidence(verified='2026-11-20'),resume_record(),newer_halt]
    status_file(path,list(reversed(rows)) if reverse else rows)
    assert TARGET not in load_nontrading_statuses(path,'2026-11-17',[TARGET])
    assert load_nontrading_statuses(path,'2026-11-18',[TARGET])[TARGET]['title']=='TEST fixture second suspension'


def test_same_session_retry_replaces_excluded_snapshot_after_data_arrives(tmp_path,monkeypatch):
    svc,store,provider,scheduler,path=make_system(tmp_path,monkeypatch,'2026-11-17')
    scheduler.run_daily_pipeline(ASOF)
    genuine=provider.rows[TARGET]
    provider.rows[TARGET]=[b for b in genuine if b.trade_date!='2026-11-17']
    pending=scheduler.run_daily_pipeline('2026-11-17')
    assert pending['status']=='PARTIAL' and pending['missing_symbols']==[TARGET]
    old_snapshot=scheduler.get_latest_snapshot()
    assert TARGET in old_snapshot.source_lineage['excluded_symbols']
    assert svc.get_market_snapshot('2026-11-17').snapshot_id==old_snapshot.snapshot_id
    provider.rows[TARGET]=genuine
    scheduler._next_retry_at=0.0  # Simulate expiration of the bounded retry delay.
    assert scheduler.should_run('2026-11-17')
    accepted=scheduler.run_daily_pipeline('2026-11-17')
    assert accepted['status']=='SUCCESS'
    new_snapshot=scheduler.get_latest_snapshot()
    assert new_snapshot.snapshot_id!=old_snapshot.snapshot_id
    assert TARGET not in new_snapshot.source_lineage['excluded_symbols']
    assert TARGET in old_snapshot.source_lineage['excluded_symbols']
    assert svc.get_market_snapshot('2026-11-17').snapshot_id==new_snapshot.snapshot_id
    assert store.get_sync_progress(TARGET).last_trade_date=='2026-11-17'


def test_tied_evidence_metadata_is_deterministic_across_file_order(tmp_path):
    path=tmp_path/'evidence.json'
    first=evidence(verified='2026-11-20')
    second={**first,'evidence_summary':'TEST fixture alternative same-day verification'}
    status_file(path,[first,second]);a=load_nontrading_statuses(path,'2026-11-17',[TARGET])
    status_file(path,[second,first]);b=load_nontrading_statuses(path,'2026-11-17',[TARGET])
    assert a==b
