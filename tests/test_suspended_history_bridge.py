"""Prove a common price basis across provider-specific suspension placeholders."""
import datetime
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.astock.data.models import DailyBar
from src.astock.data.storage import LocalStorageManager
from src.astock.data.service import AStockDataService

SYMBOL='sz000831'


def traded(day,price=10.,factor=1.,source='baostock'):
    return DailyBar(symbol=SYMBOL,trade_date=day,open=(price-.1)*factor,
        high=(price+.2)*factor,low=(price-.2)*factor,close=price*factor,
        raw_open=price-.1,raw_high=price+.2,raw_low=price-.2,raw_close=price,
        volume=1000.,amount=50000000.,source=source,adjust_type='qfq',
        adjustment_factor=factor,is_suspended=False,updated_at=day+'T17:00:00')


def suspended(day):
    return DailyBar(symbol=SYMBOL,trade_date=day,open=10.,high=10.,low=10.,close=10.,
        raw_open=10.,raw_high=10.,raw_low=10.,raw_close=10.,volume=0.,amount=0.,
        source='baostock',adjustment_factor=1.,is_suspended=True,updated_at=day+'T17:00:00')


def histories():
    real=[traded('2026-10-28',9.),traded('2026-10-29',9.5),traded('2026-10-30')]
    old=real+[suspended(d) for d in ['2026-11-02','2026-11-03','2026-11-13']]
    new=[traded(b.trade_date,b.raw_close,.5,'tencent') for b in real]
    new.append(traded('2026-11-16',11.,.5,'tencent'))
    return old,new


def test_verified_three_real_day_overlap_bridges_zero_volume_tail(tmp_path):
    store=LocalStorageManager(tmp_path);old,new=histories()
    store.write_daily_bars(SYMBOL,old)
    before=store._parquet_path(SYMBOL).read_bytes()
    assert store.write_daily_bars(SYMBOL,new)==7
    rows=store.read_daily_bars(SYMBOL,use_cache=False)
    assert rows[-1].trade_date=='2026-11-16'
    by_date={b.trade_date:b for b in rows}
    for b in old[-3:]:
        after=by_date[b.trade_date]
        assert after.is_suspended is True and after.volume==after.amount==0
        assert after.source=='baostock'
        assert after.raw_close==10. and after.close==5. and after.adjustment_factor==.5
    assert before!=store._parquet_path(SYMBOL).read_bytes()


@pytest.mark.parametrize('change,error',[
    ('missing_real','missing_overlap'),('unknown_suspension','suspension_gap'),
    ('positive_volume','suspension_gap'),('positive_amount','suspension_gap'),
    ('flat_raw_changed','suspension_gap'),('factor_changed','suspension_gap'),
    ('untrusted_source','suspension_gap'),('raw_overlap_changed','raw_history_changed'),
    ('adjusted_overlap_changed','inconsistent_adjustment_basis'),
])
def test_unproved_sparse_history_never_changes_store(tmp_path,change,error):
    store=LocalStorageManager(tmp_path);old,new=histories()
    if change=='missing_real':new=new[1:]
    if change=='unknown_suspension':old[-1]=replace(old[-1],is_suspended=False)
    if change=='positive_volume':old[-1]=replace(old[-1],volume=1.)
    if change=='positive_amount':old[-1]=replace(old[-1],amount=1.)
    if change=='flat_raw_changed':old[-1]=replace(old[-1],raw_high=10.1)
    if change=='factor_changed':old[-1]=replace(old[-1],adjustment_factor=.9)
    if change=='untrusted_source':old[-1]=replace(old[-1],source='unknown')
    if change=='raw_overlap_changed':new[0]=replace(new[0],raw_close=new[0].raw_close+.01)
    if change=='adjusted_overlap_changed':new[0]=replace(new[0],open=new[0].open+.1)
    store.write_daily_bars(SYMBOL,old)
    before=store._parquet_path(SYMBOL).read_bytes()
    progress=store.get_sync_progress(SYMBOL)
    with pytest.raises(ValueError,match=error):store.write_daily_bars(SYMBOL,new)
    assert before==store._parquet_path(SYMBOL).read_bytes()
    assert store.get_sync_progress(SYMBOL)==progress


def test_any_missing_actual_trade_after_anchor_is_not_a_suspension_bridge(tmp_path):
    store=LocalStorageManager(tmp_path);old,new=histories()
    old[-1]=traded('2026-11-13',10.)
    store.write_daily_bars(SYMBOL,old)
    with pytest.raises(ValueError,match='missing_overlap'):store.write_daily_bars(SYMBOL,new)


def test_service_widens_only_when_real_overlap_hidden_by_verified_halt(tmp_path):
    store=LocalStorageManager(tmp_path);old,_=histories();store.write_daily_bars(SYMBOL,old)
    svc=AStockDataService(storage=store,providers=SimpleNamespace())
    assert svc._incremental_stock_start(SYMBOL,'2026-11-13',datetime.date(2026,11,16))=='2026-10-27'
    ordinary='sh600000'
    store.write_daily_bars(ordinary,[replace(traded(d),symbol=ordinary) for d in ['2026-11-10','2026-11-11','2026-11-13']])
    assert svc._incremental_stock_start(ordinary,'2026-11-13',datetime.date(2026,11,16))=='2026-11-03'
    assert svc._incremental_stock_start('sh600001','',datetime.date(2026,11,16))=='2025-10-12'


def test_service_narrow_overlap_does_not_read_beyond_ten_days(tmp_path):
    store=LocalStorageManager(tmp_path);ordinary=[traded(d) for d in ['2026-11-10','2026-11-11','2026-11-13']]
    store.write_daily_bars(SYMBOL,ordinary)
    svc=AStockDataService(storage=store,providers=SimpleNamespace())
    original=store.read_daily_bars;store.read_daily_bars=Mock(wraps=original)
    assert svc._incremental_stock_start(SYMBOL,'2026-11-13',datetime.date(2026,11,16))=='2026-11-03'
    assert store.read_daily_bars.call_count==1
    assert store.read_daily_bars.call_args.kwargs['start_date']=='2026-11-03'
