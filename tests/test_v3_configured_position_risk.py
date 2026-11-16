"""Configured risk behavior with raw-price indicators and isolated SQLite accounts."""
import datetime
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.astock.trade.execution_policy import load_execution_policies
from src.astock.trade.market_inputs import completed_position_indicators
from src.astock.trade.risk.position import PositionRiskRule
from src.astock.trade.models import Position,Order,OrderSide,OrderStatus,TradeFill
from src.astock.trade.runtime import TradingRuntime
from trade_calendar import BEIJING,is_trading_day

ROOT=Path(__file__).resolve().parents[1]
POLICIES=load_execution_policies(ROOT)
SYMBOL='sh600185'
SIGNAL_DAY='2026-11-16'
EXECUTION_DAY='2026-11-17'


def pos(highest=130.,available=100,trailing=0.):
    return Position(symbol=SYMBOL,shares=100,available_shares=available,cost_price=100.,
        current_price=125.,highest_price=highest,market_value=12500.,
        trailing_stop_price=trailing,name='test',sector='test-sector')


def evaluate(account,position=None,current=125.,atr=2.,ma20=100.,consecutive=0):
    return PositionRiskRule().check_position_exit(position or pos(),current,atr,ma20,
        consecutive_below_ma20=consecutive,policy=POLICIES[account])


@pytest.mark.parametrize('account',['balanced','aggressive'])
def test_peak_below_twenty_percent_does_not_activate_atr_exit(account):
    exit_,reason,audit=evaluate(account,pos(highest=119.),current=110.,atr=.1)
    assert exit_ is False and reason=='HOLD'
    assert audit.details['trailing_stop_price']==0
    assert audit.details['activation_pct']==20.


def test_exact_twenty_percent_peak_activates_existing_rule():
    exit_,reason,audit=evaluate('balanced',pos(highest=120.),current=119.,atr=.1)
    assert exit_ and reason=='动态ATR止盈'
    assert audit.details['trailing_stop_price']>119.


def test_accounts_keep_different_atr_lines_from_real_profiles():
    b=evaluate('balanced');a=evaluate('aggressive')
    assert b[2].details['trailing_stop_price']==122.72
    assert a[2].details['trailing_stop_price']==120.64
    assert evaluate('balanced',current=121.)[0] is True
    assert evaluate('aggressive',current=121.)[0] is False


def test_balanced_three_percent_floor_is_not_applied_to_aggressive():
    b=evaluate('balanced',pos(highest=125.),current=102.5,atr=10.)
    a=evaluate('aggressive',pos(highest=125.),current=102.5,atr=10.)
    assert b[0] is True and b[2].details['trailing_stop_price']==103.
    assert a[0] is False and a[2].details['trailing_stop_price']<100.


@pytest.mark.parametrize('account',['balanced','aggressive'])
def test_existing_protection_line_can_only_rise_even_when_atr_expands(account):
    position=pos(trailing=123.)
    result=evaluate(account,position,current=125.,atr=6.)
    assert result[0] is False
    assert result[2].details['trailing_stop_price']==123.
    result=evaluate(account,replace(position,trailing_stop_price=124.),current=123.5,atr=6.)
    assert result[0] is True and result[2].details['trailing_stop_price']==124.


def test_aggressive_ma20_requires_two_completed_closes_below_99_percent():
    position=pos(highest=100.)
    assert evaluate('aggressive',position,current=98.,atr=1.,ma20=100.,consecutive=1)[0] is False
    result=evaluate('aggressive',position,current=101.,atr=1.,ma20=100.,consecutive=2)
    assert result[0] is True and result[1]=='跌破MA20退出'
    assert result[2].details['required_days']==2


def test_balanced_ma20_uses_one_percent_buffer():
    position=pos(highest=100.)
    assert evaluate('balanced',position,current=99.1,atr=1.,ma20=100.)[0] is False
    assert evaluate('balanced',position,current=98.9,atr=1.,ma20=100.)[1]=='跌破MA20退出'


@pytest.mark.parametrize('account',['balanced','aggressive'])
def test_six_percent_hard_stop_and_t_plus_one_are_preserved(account):
    result=evaluate(account,pos(highest=100.),current=94.,atr=0.,ma20=0.)
    assert result[0] is True and result[1]=='硬止损触发'
    locked=evaluate(account,pos(highest=100.,available=0),current=90.,atr=0.,ma20=0.)
    assert locked[0] is False and locked[1]=='T_PLUS_1_LOCKED'


def bars(n=25,factor=.5,prices=None,signal_day=SIGNAL_DAY):
    stop=datetime.date.fromisoformat(signal_day);dates=[];cursor=stop
    while len(dates)<n:
        if is_trading_day(cursor):dates.append(cursor.isoformat())
        cursor-=datetime.timedelta(days=1)
    dates.reverse();prices=prices or [100.]*n
    return [{'date':d,'open':price*factor,'high':(price+2)*factor,'low':(price-2)*factor,
             'close':price*factor,'raw_open':price,'raw_high':price+2,'raw_low':price-2,
             'raw_close':price,'adjustment_factor':factor,'is_final':True,
             'volume':1000.,'amount':50000000.} for d,price in zip(dates,prices)]


def test_qfq_indicators_are_converted_to_raw_execution_basis():
    info=completed_position_indicators(bars(),SIGNAL_DAY)
    assert info['signal_asof']==SIGNAL_DAY
    assert info['ma20']==100.
    assert info['ma20_previous19_sum']==1900.
    assert info['atr']==4.
    assert info['consecutive_below_ma20']==0


def test_future_bar_cannot_change_completed_indicators():
    history=bars()
    expected=completed_position_indicators(history,SIGNAL_DAY)
    future={**history[-1],'date':EXECUTION_DAY,'close':999999.,'is_final':False}
    assert completed_position_indicators(history+[future],SIGNAL_DAY)==expected


def test_aggressive_two_day_indicator_uses_both_completed_close_ma20_windows():
    history=bars(prices=[100.]*23+[95.,95.],factor=.5)
    result=completed_position_indicators(history,SIGNAL_DAY)
    assert result['consecutive_below_ma20']==2
    assert result['ma20']==pytest.approx(99.5)


@pytest.mark.parametrize('change',['missing_factor','zero_factor','nan_factor','mismatched_raw','nonfinal_last','nonfinal_previous'])
def test_unproved_price_basis_or_nonfinal_lookback_does_not_make_indicators(change):
    history=bars()
    if change=='missing_factor':history[-1].pop('adjustment_factor')
    if change=='zero_factor':history[-1]['adjustment_factor']=0
    if change=='nan_factor':history[-1]['adjustment_factor']=float('nan')
    if change=='mismatched_raw':history[-1]['raw_close']=80.
    if change=='nonfinal_last':history[-1]['is_final']=False
    if change=='nonfinal_previous':history[-2]['is_final']=False
    assert completed_position_indicators(history,SIGNAL_DAY)=={}


def make_runtime(tmp_path,monkeypatch):
    monkeypatch.setenv('ASTOCK_ENV','test')
    monkeypatch.setenv('ASTOCK_AUDIT_PATH',str(tmp_path/'audit.json'))
    runtime=TradingRuntime(db_path=str(tmp_path/'isolated.sqlite'),mode='ACTIVE')
    runtime.configure_execution_policies(POLICIES)
    return runtime


def seed(runtime,account='balanced',day=SIGNAL_DAY):
    oid=f'seed-{account}'
    order=Order(order_id=oid,client_order_id=oid,account_id=account,symbol=SYMBOL,
                side=OrderSide.BUY,shares=100,price=100.,status=OrderStatus.SUBMITTED)
    runtime.ledger.save_order(order)
    fill=TradeFill(fill_id=f'fill-{account}',order_id=oid,client_order_id=oid,
        account_id=account,symbol=SYMBOL,name='test',side=OrderSide.BUY,price=100.,
        shares=100,amount=10000.,commission=0.,stamp_tax=0.,transfer_fee=0.,
        slippage=0.,realized_pnl=0.,timestamp=day+'T09:31:00')
    runtime.ledger.apply_fill(fill,name='test',sector='test-sector')


def active_step(runtime,current,info=None,snapshot=None,day=EXECUTION_DAY,minute=45):
    now=datetime.datetime.fromisoformat(day+f'T09:{minute:02d}:00').replace(tzinfo=BEIJING)
    quote={'current':current,'fresh':True,'timestamp':now.isoformat(),'name':'test',
           'pre_close':max(100.,current),'open':current,'is_suspended':False,'is_st':False}
    data={'candidates':[],'csi300_bars':[],  # Freeze new buys only; risk exits must still run.
          'position_indicators':{SYMBOL:info or {}},'execution_policy':POLICIES}
    if snapshot is not None:data['snapshot']=snapshot
    return runtime.step(day,quotes={SYMBOL:quote},market_data=data,now=now,
                        signal_asof_date=SIGNAL_DAY)


@pytest.mark.parametrize('current,expected_exit',[(98.95,False),(98.9,True)])
def test_runtime_balanced_uses_last_19_completed_closes_plus_live_price(tmp_path,monkeypatch,current,expected_exit):
    runtime=make_runtime(tmp_path,monkeypatch);seed(runtime)
    info=completed_position_indicators(bars(factor=.5),SIGNAL_DAY)
    active_step(runtime,current,info)
    # 98.95 is below completed MA20*0.99 (=99), but above dynamic
    # ((19*100+98.95)/20)*0.99 (=98.948025), so the configured live MA holds.
    assert (SYMBOL not in runtime.ledger.get_positions('balanced')) is expected_exit


def test_runtime_persists_a_protection_line_that_survives_restart_and_atr_growth(tmp_path,monkeypatch):
    runtime=make_runtime(tmp_path,monkeypatch);seed(runtime)
    runtime.ledger.update_market_prices({SYMBOL:{'current':130.,'fresh':True}})
    runtime.ledger.raise_trailing_stop('balanced',SYMBOL,123.)
    info={'signal_asof':SIGNAL_DAY,'ma20':100.,'ma20_previous19_sum':1900.,'atr':2.}
    active_step(runtime,125.,info)
    assert runtime.ledger.get_positions('balanced')[SYMBOL].trailing_stop_price==123.
    restarted=TradingRuntime(db_path=str(tmp_path/'isolated.sqlite'),mode='ACTIVE')
    restarted.configure_execution_policies(POLICIES)
    assert restarted.ledger.get_positions('balanced')[SYMBOL].trailing_stop_price==123.
    active_step(restarted,125.,{**info,'atr':6.})
    assert restarted.ledger.get_positions('balanced')[SYMBOL].trailing_stop_price==123.


def test_expired_suspension_in_yesterdays_snapshot_does_not_block_resumption_hard_stop(tmp_path,monkeypatch):
    runtime=make_runtime(tmp_path,monkeypatch);seed(runtime)
    yesterday=SimpleNamespace(source_lineage={'nontrading_statuses':{
        SYMBOL:{'status':'SUSPENDED','effective_from':'2026-11-13','verified_through':SIGNAL_DAY}}})
    active_step(runtime,93.,snapshot=yesterday)
    assert SYMBOL not in runtime.ledger.get_positions('balanced')
    assert any(t['side']=='SELL' for t in runtime.ledger.get_trades('balanced'))


def test_current_verified_suspension_still_blocks_residual_quote_exit(tmp_path,monkeypatch):
    runtime=make_runtime(tmp_path,monkeypatch);seed(runtime)
    current=SimpleNamespace(source_lineage={'nontrading_statuses':{
        SYMBOL:{'status':'SUSPENDED','effective_from':'2026-11-13','verified_through':EXECUTION_DAY}}})
    active_step(runtime,93.,snapshot=current)
    assert SYMBOL in runtime.ledger.get_positions('balanced')


def test_runtime_today_bought_stock_remains_t_plus_one_locked(tmp_path,monkeypatch):
    runtime=make_runtime(tmp_path,monkeypatch);seed(runtime,day=EXECUTION_DAY)
    active_step(runtime,93.)
    assert runtime.ledger.get_positions('balanced')[SYMBOL].tradable_shares==0
    assert len(runtime.ledger.get_trades('balanced'))==1


def test_runtime_keeps_account_specific_persisted_atr_lines(tmp_path,monkeypatch):
    runtime=make_runtime(tmp_path,monkeypatch)
    seed(runtime,'balanced');seed(runtime,'aggressive')
    runtime.ledger.update_market_prices({SYMBOL:{'current':130.,'fresh':True}})
    info={'signal_asof':SIGNAL_DAY,'ma20':100.,'ma20_previous19_sum':1900.,'atr':2.}
    active_step(runtime,125.,info)
    assert runtime.ledger.get_positions('balanced')[SYMBOL].trailing_stop_price==122.72
    assert runtime.ledger.get_positions('aggressive')[SYMBOL].trailing_stop_price==120.64


def test_runtime_aggressive_two_completed_closes_and_balanced_live_ma_are_distinct(tmp_path,monkeypatch):
    runtime=make_runtime(tmp_path,monkeypatch)
    seed(runtime,'balanced');seed(runtime,'aggressive')
    info=completed_position_indicators(bars(prices=[100.]*23+[95.,95.],factor=.5),SIGNAL_DAY)
    assert info['consecutive_below_ma20']==2
    active_step(runtime,101.,info)
    assert SYMBOL in runtime.ledger.get_positions('balanced')
    assert SYMBOL not in runtime.ledger.get_positions('aggressive')


def test_runtime_twenty_percent_activation_prevents_previous_early_atr_exit(tmp_path,monkeypatch):
    runtime=make_runtime(tmp_path,monkeypatch)
    seed(runtime,'balanced');seed(runtime,'aggressive')
    runtime.ledger.update_market_prices({SYMBOL:{'current':119.,'fresh':True}})
    info={'signal_asof':SIGNAL_DAY,'ma20':100.,'ma20_previous19_sum':1900.,'atr':.1}
    active_step(runtime,110.,info)
    for account in ['balanced','aggressive']:
        assert runtime.ledger.get_positions(account)[SYMBOL].trailing_stop_price==0
