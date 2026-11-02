"""Offline resource/parity harness: no live account writes or provider requests."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import resource
import socket
import time

from compact_bars import CompactBars
from history_data import HistoricalDataFeed
from momentum_backtest import MomentumBacktestEngine
from strategy_rules import MomentumConfig, evaluate_momentum, holding_signal
from streaming_history import CacheOnlyFeed
from trade_calendar import is_trading_day

# Hard fail if any accidental provider route tries to use the network.
def no_network(*args,**kwargs):raise RuntimeError('benchmark_network_forbidden')
socket.socket.connect=no_network

class SyntheticFeed:
    def __init__(self, symbols, years=4):
        self.symbols=symbols
        self.dates=[];d=dt.date(2022,1,3)
        # Four years of weekday workload, explicitly synthetic calendar.
        while len(self.dates)<252*years+140:
            if d.weekday()<5:self.dates.append(d.isoformat())
            d+=dt.timedelta(days=1)
        self.metadata={}
    def fetch_benchmark_kline(self,**kwargs):
        return [dict(date=d,open=100+i*.01,close=100+i*.01,high=101+i*.01,low=99+i*.01,volume=1e6) for i,d in enumerate(self.dates)]
    def fetch_stock_kline(self,symbol,start,end):
        n=int(symbol[2:])%100
        bars=[]
        for i,d in enumerate(self.dates):
            close=round(20+n/10+i*.01,2);opening=round(close-.02,2)
            bars.append(dict(date=d,open=opening,close=close,high=close+.05,low=opening-.05,
                raw_open=opening,raw_close=close,raw_high=close+.05,raw_low=opening-.05,
                raw_pre_close=close-.01,volume=1e6,amount=close*1e6,adjustment_factor=1.,is_st=False))
        return symbol,symbol,bars

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=['live','synthetic','real'],required=True)
    parser.add_argument('--cache',type=Path);parser.add_argument('--config',type=Path)
    parser.add_argument('--count',type=int,default=3185);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--start',default='2024-01-01');parser.add_argument('--end',default='2026-10-30');parser.add_argument('--hold-seconds',type=int,default=0)
    args=parser.parse_args();start=time.monotonic()
    symbols=json.loads(args.config.read_text())['symbols'][:args.count] if args.config else ['sh'+str(600000+i) for i in range(args.count)]
    if args.mode=='live':
        feed=HistoricalDataFeed(cache_dir=args.cache);retained={};mismatch=[];loaded=0
        for n,sym in enumerate(symbols):
            item=feed._peek_cached(sym,'2025-09-25',args.end)
            bars=item['bars'];compact=CompactBars(bars,126)
            if bars:
                loaded+=1
                for fn in (lambda b:evaluate_momentum(b,args.end,MomentumConfig()),lambda b:holding_signal(b,args.end)):
                    if fn(bars)!=fn(compact):mismatch.append(sym)
            retained[sym]=compact
            if n%500==0:print('live',n,flush=True)
        result=dict(mode='live_cache_parity',symbols=len(symbols),loaded=loaded,mismatches=mismatch,
            elapsed_seconds=time.monotonic()-start,peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024)
        args.output.write_text(json.dumps(result));print(json.dumps(result),flush=True)
        if mismatch:raise SystemExit(1)
        if args.hold_seconds:time.sleep(args.hold_seconds)
        return
    if args.mode=='synthetic':
        feed=SyntheticFeed(symbols);start_date,end_date=feed.dates[140],feed.dates[-1]
    else:
        feed=CacheOnlyFeed(cache_dir=args.cache);start_date,end_date=args.start,args.end
    def progress(phase,done,total):
        if done%100==0:print(phase,done,total,round(time.monotonic()-start,1),flush=True)
    engine=MomentumBacktestEngine(start_date=start_date,end_date=end_date,symbols=symbols,
        feed=feed,streaming=True,spill_results=True,progress=progress,work_dir=args.output.parent)
    try:
        result=engine.run()
        report=dict(mode=args.mode,symbols=len(symbols),days=result['summary']['trading_days'],
            elapsed_seconds=time.monotonic()-start,peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            summary=result['summary'])
        args.output.write_text(json.dumps(report,ensure_ascii=False));print(json.dumps(report),flush=True)
    finally:engine.close()

if __name__=='__main__':main()
