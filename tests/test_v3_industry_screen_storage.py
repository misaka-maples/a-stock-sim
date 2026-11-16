"""Fast Arrow-backed screening must equal the existing dated industry rule."""
import datetime
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pyarrow.parquet as pq
import pytest

from src.astock.data.models import DailyBar,StockInfo
from src.astock.data.storage import LocalStorageManager
from src.astock.data.service import AStockDataService
from src.astock.trade.strategy.signal_inputs import leading_industries

DAY='2026-11-16'


def history(symbol,n=150,kind='up',amount=50000000.):
    end=datetime.date.fromisoformat(DAY)
    rows=[]
    for i in range(n):
        price=10+i*.02 if kind=='up' else 20-i*.02
        day=(end-datetime.timedelta(days=n-1-i)).isoformat()
        rows.append(DailyBar(symbol=symbol,trade_date=day,open=price,high=price+.1,
            low=price-.1,close=price,raw_open=price,raw_high=price+.1,raw_low=price-.1,
            raw_close=price,adjustment_factor=1.,volume=1000.,amount=amount,
            source='test',is_final=True,updated_at=day+'T17:00:00'))
    return rows


def system(tmp_path,groups=3,per_group=4):
    store=LocalStorageManager(tmp_path/'storage')
    svc=AStockDataService(storage=store,providers=SimpleNamespace(),offline_mode=True)
    all_rows={};mapping={};names={}
    for group in range(groups):
        for number in range(per_group):
            symbol=f'sh60{group:02d}{number:02d}'
            rows=history(symbol,kind='down' if group==groups-1 else 'up')
            store.write_daily_bars(symbol,rows)
            all_rows[symbol]=[b.to_legacy_dict() for b in rows]
            mapping[symbol]=f'industry-{group}';names[symbol]=symbol
    svc._industry_map_cache=mapping
    svc._stock_names_cache=names
    return svc,all_rows,mapping,names


def test_arrow_projection_is_identical_to_original_rule_without_dataclass_reader(tmp_path):
    svc,rows,mapping,names=system(tmp_path)
    expected=leading_industries(DAY,rows,mapping,symbols=list(rows),names=names)
    svc.storage.read_daily_bars=Mock(side_effect=AssertionError('No dataclass/pandas row reader permitted'))
    actual=svc.compute_industry_screen(DAY,list(rows))
    assert actual==expected
    assert actual['top_sectors']==['industry-0','industry-1']
    svc.storage.read_daily_bars.assert_not_called()


def test_projection_only_reads_necessary_columns_and_caches_results(tmp_path,monkeypatch):
    svc,rows,_,_=system(tmp_path)
    original=pq.read_table;calls=[]
    def traced(*args,**kwargs):
        calls.append(kwargs);return original(*args,**kwargs)
    monkeypatch.setattr(pq,'read_table',traced)
    first=svc.compute_industry_screen(DAY,list(rows))
    first['top_sectors'].append('caller mutation')
    second=svc.compute_industry_screen(DAY,list(rows))
    assert len(calls)==len(rows)
    assert 'caller mutation' not in second['top_sectors']
    assert all(set(c['columns'])=={'trade_date','close','volume','amount','is_st','is_suspended'} for c in calls)
    assert all(c['use_threads'] is False for c in calls)


def test_progress_update_invalidates_cached_screen(tmp_path,monkeypatch):
    svc,rows,_,_=system(tmp_path)
    original=pq.read_table;reader=Mock(wraps=original)
    monkeypatch.setattr(pq,'read_table',reader)
    svc.compute_industry_screen(DAY,list(rows));assert reader.call_count==len(rows)
    symbol=next(iter(rows));progress=svc.storage.get_sync_progress(symbol)
    svc.storage.update_sync_progress(symbol,progress.last_trade_date,progress.total_bars,
        first_trade_date=progress.first_trade_date,last_source='test',last_error='new check')
    svc.compute_industry_screen(DAY,list(rows))
    assert reader.call_count==2*len(rows)


def test_scope_and_industry_mapping_change_invalidate_cached_screen(tmp_path):
    svc,rows,mapping,_=system(tmp_path)
    old=svc.compute_industry_screen(DAY,list(rows))
    for symbol in rows:
        if mapping[symbol]=='industry-0':svc._industry_map_cache[symbol]='renamed-industry'
    new=svc.compute_industry_screen(DAY,list(rows))
    assert 'renamed-industry' in new['top_sectors'] and new!=old
    one=svc.compute_industry_screen(DAY,[next(iter(rows))])
    assert one['top_sectors']==[]  # One winning stock cannot manufacture an industry.


def test_future_rows_are_removed_before_tail_and_short_stale_or_bad_inputs_are_excluded(tmp_path):
    svc,rows,mapping,names=system(tmp_path,groups=2,per_group=6)
    symbols=list(rows)
    # One industry still has 3 valid members; the other was already declining.
    bad_st,suspended,illiquid=symbols[:3]
    for symbol,field,value in [(bad_st,'is_st',True),(suspended,'is_suspended',True),
                               (illiquid,'amount',1.)]:
        old=svc.storage.read_daily_bars(symbol)
        modified=[replace(b,**{field:value}) for b in old]
        svc.storage.write_daily_bars(symbol,modified)
        rows[symbol]=[b.to_legacy_dict() for b in modified]
    stale=symbols[6];old=svc.storage.read_daily_bars(stale)[:-1]
    # Replace a test-only Parquet with stale rows, then update its progress.
    svc.storage._parquet_path(stale).unlink();svc.storage.write_daily_bars(stale,old)
    rows[stale]=[b.to_legacy_dict() for b in old]
    short=symbols[7];old=svc.storage.read_daily_bars(short)[-125:]
    svc.storage._parquet_path(short).unlink();svc.storage.write_daily_bars(short,old)
    rows[short]=[b.to_legacy_dict() for b in old]
    future_symbol=symbols[3];old=svc.storage.read_daily_bars(future_symbol)
    future=[replace(old[-1],trade_date=(datetime.date.fromisoformat(DAY)+datetime.timedelta(days=i)).isoformat(),
                    open=100000.,high=100000.,low=100000.,close=100000.,
                    raw_open=100000.,raw_high=100000.,raw_low=100000.,raw_close=100000.) for i in range(1,150)]
    svc.storage.write_daily_bars(future_symbol,old[-3:]+future)
    rows[future_symbol]+=[b.to_legacy_dict() for b in future]
    expected=leading_industries(DAY,rows,mapping,symbols=symbols,names=names)
    assert svc.compute_industry_screen(DAY,symbols)==expected
    assert expected['industry_stats']['industry-0']['stock_count']==3


def test_snapshot_contains_the_shared_precomputed_industry_screen(tmp_path):
    svc,rows,_,_=system(tmp_path)
    bench=[replace(b,symbol='sh000300',adjust_type='none') for b in history('sh000300')]
    svc.storage.write_daily_bars('sh000300',bench,adjust_type='none')
    snap=svc.compute_market_snapshot(DAY,watchlist=list(rows))
    assert snap.source_lineage['industry_screen']==svc.compute_industry_screen(DAY,list(rows))
    assert snap.source_lineage['industry_screen']['signal_asof_date']==DAY
