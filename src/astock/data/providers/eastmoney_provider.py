"""EastMoney daily history adapter with separately verified raw/adjusted prices.

The public endpoint is also used by AKShare/efinance; these are one upstream,
not three independent providers. Network failure must fall through to others.
"""
from datetime import datetime
import requests
from .base import BaseMarketDataProvider
from ..models import DailyBar, normalize_symbol


class EastMoneyProvider(BaseMarketDataProvider):
    def __init__(self, timeout=6.0):
        self._timeout=timeout
        self._session=requests.Session()
        self._session.headers.update({'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'})

    @property
    def name(self):return 'eastmoney'

    def _rows(self,symbol,start,end,adjust):
        sym=normalize_symbol(symbol)
        params={'secid':('1' if sym.startswith('sh') else '0')+'.'+sym[2:],
                'klt':'101','fqt':{'none':0,'qfq':1,'hfq':2}[adjust],
                'beg':(start or '1990-01-01').replace('-',''),
                'end':(end or '2099-12-31').replace('-',''),'lmt':10000,
                'fields1':'f1,f2,f3,f4,f5,f6',
                'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'}
        r=self._session.get('https://push2his.eastmoney.com/api/qt/stock/kline/get',params=params,timeout=self._timeout)
        r.raise_for_status();payload=r.json()
        if payload.get('rc')!=0:raise ValueError('eastmoney_api_rejected')
        data=payload.get('data') or {}
        if data and str(data.get('code'))!=sym[2:]:raise ValueError('eastmoney_symbol_mismatch')
        return {v[0]:v for row in data.get('klines',[]) if len(v:=row.split(','))>=11}

    def fetch_stock_daily(self,symbol,start_date,end_date,adjust_type='qfq'):
        sym=normalize_symbol(symbol)
        raw=self._rows(sym,start_date,end_date,'none')
        adjusted=raw if adjust_type=='none' else self._rows(sym,start_date,end_date,adjust_type)
        if set(raw)!=set(adjusted):raise ValueError('eastmoney_raw_adjusted_dates_mismatch')
        out=[];stamp=datetime.utcnow().isoformat()
        for day in sorted(raw):
            r,a=raw[day],adjusted[day]
            if (start_date and day<start_date) or (end_date and day>end_date):continue
            rc=float(r[2]);ac=float(a[2])
            if rc<=0 or ac<=0:raise ValueError('eastmoney_invalid_price')
            out.append(DailyBar(symbol=sym,trade_date=day,open=float(a[1]),close=ac,
                high=float(a[3]),low=float(a[4]),volume=float(r[5]),amount=float(r[6]),
                source=self.name,adjust_type=adjust_type,updated_at=stamp,is_final=True,
                raw_open=float(r[1]),raw_close=rc,raw_high=float(r[3]),raw_low=float(r[4]),
                raw_pre_close=rc-float(r[9]),adjustment_factor=ac/rc,
                amplitude_pct=float(r[7]),change_pct=float(r[8]),turnover_rate=float(r[10]),
                is_suspended=float(r[5])==0))
        return out

    def fetch_index_daily(self,index_code,start_date,end_date):
        return self.fetch_stock_daily(index_code,start_date,end_date,'none')

    def fetch_stock_list(self):return []
    def fetch_industry_map(self):return {}
    def is_healthy(self):
        try:return bool(self.fetch_index_daily('sh000300','',''))
        except Exception:return False
