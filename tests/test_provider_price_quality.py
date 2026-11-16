"""No external requests; verify raw/adjusted dates and real turnover provenance."""
import json
import sys
import time
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from src.astock.data.providers.tencent_provider import TencentProvider
from src.astock.data.providers.sina_provider import SinaProvider
from src.astock.data.providers.baostock_provider import BaoStockProvider
from src.astock.data.providers.akshare_provider import AKShareProvider


def tencent_payload():
    return {'sh600000': {'day': [['2026-11-16','20','22','24','18','200',{},'1.5','30']]}}


def test_tencent_raw_adjusted_prices_and_real_amount():
    p=TencentProvider()
    raw=tencent_payload()['sh600000']
    adj={'qfqday':[['2026-11-16','10','11','12','9','200']]}
    p._request=Mock(side_effect=[raw,adj])
    b=p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16')[0]
    assert (b.close,b.raw_close,b.adjustment_factor)==(11,22,.5)
    assert b.amount==300000 and b.volume==200
    assert p._request.call_args_list[0].args[-1]=='none'
    assert p._request.call_args_list[1].args[-1]=='qfq'


def test_tencent_does_not_invent_amount_or_use_quote_from_another_day():
    p=TencentProvider()
    raw={'day':[['2026-11-16','20','22','24','18','200']],
         'qt':{'sh600000':['2026-11-13','untrusted old quote']}}
    p._request=Mock(return_value=raw)
    b=p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','none')[0]
    assert b.amount==0


def test_tencent_missing_adjusted_dates_rejected():
    p=TencentProvider();p._request=Mock(side_effect=[tencent_payload()['sh600000'],{'qfqday':[]}])
    with pytest.raises(ValueError,match='align'):
        p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16')


def test_tencent_request_routes_raw_to_amount_endpoint_and_has_timeout():
    p=TencentProvider(timeout=2)
    response=Mock();response.json.return_value={'code':0,'data':tencent_payload()}
    p._session.get=Mock(return_value=response)
    p._request('sh600000','2026-11-16','2026-11-16','none')
    args=p._session.get.call_args
    assert 'newfqkline' in args.args[0]
    assert args.kwargs['timeout']==2
    assert args.kwargs['params']['param'].endswith(',640,')


def test_sina_cannot_mislabel_unadjusted_prices_as_qfq():
    p=SinaProvider();p._session.get=Mock()
    with pytest.raises(ValueError,match='unadjusted'):
        p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','qfq')
    p._session.get.assert_not_called()


def test_sina_no_amount_estimate_for_raw_bars():
    p=SinaProvider();response=Mock()
    response.json.return_value=[{'day':'2026-11-16','open':'10','high':'12','low':'9','close':'11','volume':'20000'}]
    p._session.get=Mock(return_value=response)
    assert p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','none')[0].amount==0


class BaoResult:
    error_code='0'
    fields=['date','open','high','low','close','preclose','volume','amount','adjustflag','turn','tradestatus','pctChg','isST']
    def __init__(self,adjust):
        px=[10,12,9,11] if adjust=='2' else [20,24,18,22]
        self.row=['2026-11-16',*map(str,px),'21','20000','300000',adjust,'1.5','1','4.76','0']
        self.i=0
    def next(self):self.i+=1;return self.i==1
    def get_row_data(self):return self.row


def test_baostock_pairs_raw_adjusted_and_keeps_exchange_reference():
    p=BaoStockProvider(isolated=False);p._logged_in=True
    p._bs=SimpleNamespace(query_history_k_data_plus=Mock(side_effect=lambda *a,**kw:BaoResult(kw['adjustflag'])))
    b=p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16')[0]
    assert (b.close,b.raw_close,b.adjustment_factor)==(11,22,.5)
    assert b.raw_pre_close==21 and b.amount==300000 and b.volume==200
    assert [c.kwargs['adjustflag'] for c in p._bs.query_history_k_data_plus.call_args_list]==['3','2']


def test_baostock_blocked_sdk_process_is_killed_on_deadline(monkeypatch):
    import src.astock.data.providers.baostock_provider as module
    real_popen=subprocess.Popen
    created=[]
    def sleeping_worker(*args,**kwargs):
        process=real_popen([sys.executable,'-u','-c','import time; time.sleep(20)'],**kwargs)
        created.append(process);return process
    monkeypatch.setattr(module.subprocess,'Popen',sleeping_worker)
    p=BaoStockProvider(timeout=.1)
    t=time.monotonic()
    with pytest.raises(TimeoutError):p._worker_request('is_healthy',[])
    assert time.monotonic()-t<3
    assert p._process is None and created[0].poll() is not None


def test_akshare_pairs_raw_and_adjusted_and_real_amount(monkeypatch):
    import pandas as pd
    raw=pd.DataFrame([{'日期':'2026-11-16','开盘':20,'最高':24,'最低':18,'收盘':22,'成交量':200,'成交额':300000}])
    adj=raw.copy()
    for col in ['开盘','最高','最低','收盘']:adj[col]/=2
    fn=Mock(side_effect=[raw,adj]);monkeypatch.setitem(sys.modules,'akshare',SimpleNamespace(stock_zh_a_hist=fn))
    b=AKShareProvider(isolated=False).fetch_stock_daily('sh600000','2026-11-16','2026-11-16')[0]
    assert (b.close,b.raw_close,b.adjustment_factor)==(11,22,.5)
    assert b.amount==300000 and b.volume==200
    assert [x.kwargs['adjust'] for x in fn.call_args_list]==['','qfq']


def test_akshare_blocked_library_has_process_deadline(monkeypatch):
    import src.astock.data.providers.akshare_provider as module
    def timed_out(*args,**kwargs):
        assert kwargs['timeout']==.1
        raise subprocess.TimeoutExpired(args[0],.1)
    monkeypatch.setattr(module.subprocess,'run',timed_out)
    with pytest.raises(TimeoutError,match='deadline'):
        AKShareProvider(timeout=.1).fetch_stock_daily('sh600000','2026-11-16','2026-11-16')


def test_isolated_sdk_workers_return_paired_prices_and_reuse_bao_session(tmp_path,monkeypatch):
    import os
    # These fake SDK modules execute in fresh interpreters, not mocked threads.
    (tmp_path/'baostock.py').write_text('''
from types import SimpleNamespace
def login():return SimpleNamespace(error_code="0")
def logout():pass
class Result:
    error_code="0"
    fields="date,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST".split(",")
    def __init__(self,flag):self.flag=flag;self.count=0
    def next(self):self.count+=1;return self.count==1
    def get_row_data(self):
        px=[10,12,9,11] if self.flag=="2" else [20,24,18,22]
        return ["2026-11-16",*map(str,px),"21","20000","300000",self.flag,"1.5","1","4.76","0"]
def query_history_k_data_plus(*args,**kwargs):return Result(kwargs["adjustflag"])
''')
    (tmp_path/'akshare.py').write_text('''
import pandas as pd
def stock_zh_a_hist(**kwargs):
    multiplier=.5 if kwargs["adjust"]=="qfq" else 1.
    return pd.DataFrame([{"日期":"2026-11-16","开盘":20*multiplier,"最高":24*multiplier,"最低":18*multiplier,"收盘":22*multiplier,"成交量":200,"成交额":300000}])
''')
    monkeypatch.setenv('PYTHONPATH',str(tmp_path)+os.pathsep+os.environ.get('PYTHONPATH',''))
    bao=BaoStockProvider(timeout=5)
    try:
        b=bao.fetch_stock_daily('sh600000','2026-11-16','2026-11-16')[0]
        pid=bao._process.pid
        b2=bao.fetch_stock_daily('sh600000','2026-11-16','2026-11-16')[0]
        assert bao._process.pid==pid
        assert (b.close,b.raw_close,b.adjustment_factor)==(11,22,.5)
        assert b2==b or b2.raw_close==b.raw_close
    finally:bao.close()
    ak=AKShareProvider(timeout=5)
    b=ak.fetch_stock_daily('sh600000','2026-11-16','2026-11-16')[0]
    assert (b.close,b.raw_close,b.adjustment_factor)==(11,22,.5)
    assert b.amount==300000


def test_tencent_adjusted_request_avoids_exclusive_end_date():
    p=TencentProvider(timeout=2)
    response=Mock();response.json.return_value={'code':0,'data':{'sh600000':{'qfqday':[]}}}
    p._session.get=Mock(return_value=response)
    p._request('sh600000','2026-11-03','2026-11-16','qfq')
    assert p._session.get.call_args.kwargs['params']['param']=='sh600000,day,2026-11-03,,640,qfq'
    data={'qfqday':[['2026-11-13','1','1','1','1','100'],['2026-11-16','1','1','1','1','100'],['2026-11-17','1','1','1','1','100']]}
    assert list(p._rows(data,'qfq','2026-11-13','2026-11-16'))==['2026-11-13','2026-11-16']


def test_tencent_uses_current_official_endpoint_for_both_price_bases():
    p=TencentProvider();p._session.get=Mock()
    p._session.get.return_value.json.return_value={'code':0,'data':{}}
    for basis in ['none','qfq']:
        p._request('sh600000','2026-11-03','2026-11-16',basis)
        call=p._session.get.call_args
        assert call.args[0]=='https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
        assert call.kwargs['params']['param'].split(',')[3]==''


def test_tencent_documented_unadjusted_day_alias_in_qfq_response():
    p=TencentProvider()
    raw=tencent_payload()['sh600000']
    p._request=Mock(side_effect=[raw,{'day':[r[:] for r in raw['day']]}])
    b=p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','qfq')[0]
    assert b.adjust_type=='qfq' and b.adjustment_factor==1
    assert b.raw_close==b.close==22 and b.amount==300000
    assert p._request.call_args.args[-1]=='qfq'


def test_tencent_present_but_empty_qfq_series_cannot_fallback_to_day():
    p=TencentProvider();raw=tencent_payload()['sh600000']
    p._request=Mock(side_effect=[raw,{'qfqday':[],'day':raw['day']}])
    with pytest.raises(ValueError,match='align'):
        p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','qfq')


def test_tencent_day_alias_requires_independent_raw_ohlc_agreement():
    p=TencentProvider();raw=tencent_payload()['sh600000']
    different=[r[:] for r in raw['day']];different[0][2]='21'
    p._request=Mock(side_effect=[raw,{'day':different}])
    with pytest.raises(ValueError,match='disagrees'):
        p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','qfq')


def with_dated_quote(raw,name,stamp):
    q=['']*40;q[1]=name;q[2]='600000';q[30]=stamp
    return {**raw,'qt':{'sh600000':q}}


@pytest.mark.parametrize('name,expected',[('*ST测试',True),('ST测试',True),('普通股票',False)])
def test_tencent_same_day_quote_sets_dated_st_status(name,expected):
    p=TencentProvider();raw=with_dated_quote(tencent_payload()['sh600000'],name,'20261116150000')
    p._request=Mock(return_value=raw)
    b=p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','none')[0]
    assert b.is_st is expected


def test_tencent_future_quote_name_does_not_rewrite_historical_st_status():
    p=TencentProvider();raw=with_dated_quote(tencent_payload()['sh600000'],'*ST测试','20261117150000')
    p._request=Mock(return_value=raw)
    b=p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','none')[0]
    assert b.is_st is None


@pytest.mark.parametrize('quote',[None,{}, {'sh600000':['too-short']}])
def test_tencent_missing_st_evidence_remains_unknown(quote):
    p=TencentProvider();raw={**tencent_payload()['sh600000'],'qt':quote}
    p._request=Mock(return_value=raw)
    b=p.fetch_stock_daily('sh600000','2026-11-16','2026-11-16','none')[0]
    assert b.is_st is None


def test_tencent_recovers_history_when_explicit_start_returns_only_today():
    p=TencentProvider()
    one={'day':[['2026-11-16','10','10','10','10','1000','0','1','5000']]}
    history={'day':[['2026-11-03','10','10','10','10','1000','0','1','5000'],
                    ['2026-11-13','10','10','10','10','1000','0','1','5000'],
                    ['2026-11-16','10','10','10','10','1000','0','1','5000']]}
    p._request=Mock(side_effect=[one,history,history])
    rows=p.fetch_stock_daily('sh600000','2026-11-03','2026-11-16')
    assert len(rows)==3
    assert p._request.call_args_list[1].args[1:3]==('','')
    assert p._request.call_args_list[2].args[1]==''
