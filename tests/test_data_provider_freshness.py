from dataclasses import replace
import pytest
from src.astock.data.models import DailyBar
from src.astock.data.providers.manager import ProviderManager
from src.astock.data.providers.eastmoney_provider import EastMoneyProvider
from src.astock.data.storage import LocalStorageManager


def bar(day='2026-11-16',close=10.,factor=1.,amount=50000000.):
    return DailyBar(symbol='sh600000',trade_date=day,open=close,high=close,low=close,
        close=close,volume=100000.,amount=amount,raw_open=close/factor,
        raw_high=close/factor,raw_low=close/factor,raw_close=close/factor,
        adjustment_factor=factor,source='test',adjust_type='qfq',updated_at=day+'T17:00:00')


class Limiter:
    def execute(self,f):return f()
class Provider:
    def __init__(self,values):self.values=values;self.calls=0
    def fetch_stock_daily(self,*a):self.calls+=1;return self.values

@pytest.mark.parametrize('first',[[],[bar('2026-11-13')],[bar(amount=0.)],[replace(bar(),is_final=False)],
                                 [replace(bar(),adjustment_factor=.5)]])
def test_empty_stale_or_bad_basis_falls_back(first):
    pm=ProviderManager();p1=Provider(first);p2=Provider([bar()])
    pm.providers={'baostock':p1,'tencent':p2};pm.limiters={'baostock':Limiter(),'tencent':Limiter()}
    pm.daily_provider_chain=['baostock','tencent']
    result=pm.fetch_stock_daily('sh600000','2026-11-13','2026-11-16')
    assert result[-1].trade_date=='2026-11-16'
    assert p1.calls==p2.calls==1
    assert pm._data_metrics['baostock']['unusable_responses']==1
    assert pm._data_metrics['tencent']['usable_responses']==1


def test_eastmoney_pairs_raw_adjusted_and_real_turnover():
    p=EastMoneyProvider()
    rows={
        'none':{'2026-11-16':['2026-11-16','20','22','24','18','1000','2200000','30','10','2','1']},
        'qfq':{'2026-11-16':['2026-11-16','10','11','12','9','1000','2200000','30','10','1','1']}}
    p._rows=lambda sym,start,end,adj:rows[adj]
    b=p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16')[0]
    assert b.close==11 and b.raw_close==22 and b.adjustment_factor==.5
    assert b.amount==2200000 and b.volume==1000 and b.raw_pre_close==20


def test_storage_rebases_verified_overlap_without_altering_raw_prices(tmp_path):
    s=LocalStorageManager(tmp_path)
    old=[bar(f'2026-11-{d:02d}') for d in [10,11,12,13]]
    s.write_daily_bars('sh600000',old)
    new=[replace(b,open=5.,high=5.,low=5.,close=5.,adjustment_factor=.5,
                 updated_at='2026-11-16T17:00:00') for b in old[1:]]+[bar(close=5.,factor=.5)]
    s.write_daily_bars('sh600000',new)
    stored=s.read_daily_bars('sh600000')
    assert len(stored)==5
    assert all(b.close==5. and b.raw_close==10. and b.adjustment_factor==.5 for b in stored)


def test_storage_refuses_unverified_increment(tmp_path):
    s=LocalStorageManager(tmp_path)
    s.write_daily_bars('sh600000',[bar('2026-11-13')])
    with pytest.raises(ValueError,match='missing_overlap'):s.write_daily_bars('sh600000',[bar()])
    assert s.read_daily_bars('sh600000')[-1].trade_date=='2026-11-13'


def test_independent_reader_sees_scheduler_atomic_write(tmp_path):
    writer=LocalStorageManager(tmp_path);reader=LocalStorageManager(tmp_path)
    first=bar('2026-11-13')
    writer.write_daily_bars('sh600000',[first])
    assert reader.read_daily_bars('sh600000')[-1].trade_date=='2026-11-13'
    writer.write_daily_bars('sh600000',[first,bar()])
    assert reader.read_daily_bars('sh600000')[-1].trade_date=='2026-11-16'


def test_unknown_risk_flag_roundtrips_without_becoming_false(tmp_path):
    b=replace(bar(),is_st=None)
    assert DailyBar.from_dict(b.to_dict()).is_st is None
    s=LocalStorageManager(tmp_path)
    s.write_daily_bars('sh600000',[b])
    assert s.read_daily_bars('sh600000')[0].is_st is None
    assert s.read_daily_bars('sh600000')[0].to_legacy_dict()['is_st'] is None


def test_unknown_provider_flag_does_not_erase_known_dated_st(tmp_path):
    s=LocalStorageManager(tmp_path)
    known=replace(bar(),is_st=True)
    s.write_daily_bars('sh600000',[known])
    unknown=replace(bar(),is_st=None,updated_at='2026-11-16T18:00:00')
    s.write_daily_bars('sh600000',[unknown])
    assert s.read_daily_bars('sh600000')[0].is_st is True


def test_partial_sync_has_a_completion_timestamp(tmp_path):
    store=LocalStorageManager(tmp_path)
    store.create_sync_task('partial','INCREMENTAL','2026-11-16',2)
    store.update_sync_task('partial',1,1,'PARTIAL')
    task=store.get_latest_tasks()[0]
    assert task.status=='PARTIAL' and task.finished_at is not None


def test_integer_lot_rounding_does_not_block_price_verified_increment(tmp_path):
    store=LocalStorageManager(tmp_path)
    old=[replace(bar(f'2026-11-{day:02d}'),volume=3677.55) for day in [11,12,13]]
    store.write_daily_bars('sh600000',old)
    new=[replace(b,volume=3678.,source='tencent',updated_at='2026-11-16T17:00:00') for b in old]+[bar()]
    store.write_daily_bars('sh600000',new)
    actual=store.read_daily_bars('sh600000')
    assert actual[-1].trade_date=='2026-11-16'
    assert actual[0].volume==3677.55


def test_volume_rounding_tolerance_does_not_relax_price_checks(tmp_path):
    store=LocalStorageManager(tmp_path)
    old=[replace(bar(f'2026-11-{day:02d}'),volume=3677.55) for day in [11,12,13]]
    store.write_daily_bars('sh600000',old)
    new=[replace(b,raw_close=9.,volume=3678.,source='tencent') for b in old]+[bar()]
    with pytest.raises(ValueError,match='raw_history_changed'):store.write_daily_bars('sh600000',new)


def test_price_basis_admission_failure_reaches_independent_backup(tmp_path):
    store=LocalStorageManager(tmp_path)
    history=[bar(f'2026-11-{day:02d}') for day in [11,12,13]]
    store.write_daily_bars('sh600000',history)
    pm=ProviderManager()
    first=Provider([bar()])  # Fresh target date, but no verifiable overlap.
    backup=Provider(history+[bar()])
    pm.providers={'primary':first,'backup':backup}
    pm.limiters={'primary':Limiter(),'backup':Limiter()}
    pm.daily_provider_chain=['primary','backup']
    accepted=pm.fetch_stock_daily_admitted('sh600000','2026-11-03','2026-11-16',
        accept=lambda rows:store.write_daily_bars('sh600000',rows))
    assert first.calls==backup.calls==1
    assert accepted and store.read_daily_bars('sh600000')[-1].trade_date=='2026-11-16'
    assert pm._data_metrics['primary']['unusable_responses']==1
    assert pm._data_metrics['backup']['usable_responses']==1


def test_increment_requires_the_cached_boundary_not_only_distant_overlap(tmp_path):
    store=LocalStorageManager(tmp_path)
    old=[bar(f'2026-11-{day:02d}') for day in [3,4,5,11,12,13]]
    store.write_daily_bars('sh600000',old)
    with pytest.raises(ValueError,match='missing_overlap'):
        store.write_daily_bars('sh600000',old[:3]+[bar()])
