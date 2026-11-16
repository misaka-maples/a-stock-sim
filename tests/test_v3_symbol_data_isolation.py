"""Incomplete ingestion quarantines only its own symbols; exits need real quotes."""
import copy
import datetime as dt
from types import SimpleNamespace

import pytest

from src.astock.trade.models import MarketRegime,OrderSide,TradeFill
from src.astock.trade.runtime import TradingRuntime,_live_exit_price
from trade_calendar import BEIJING

SIG='2026-11-16';EXEC='2026-11-17';NOW=dt.datetime(2026,11,17,9,31,tzinfo=BEIJING)


def payload(regime,excluded=None):
    end=dt.date.fromisoformat(SIG)
    benchmark=[{'date':(end-dt.timedelta(days=149-i)).isoformat(),'close':4500-i*3} for i in range(150)]
    bars=[{'date':(end-dt.timedelta(days=129-i)).isoformat(),'close':9+i*.01,
           'volume':100000,'amount':40000000,'is_final':True,'is_st':False,'is_suspended':False}
          for i in range(130)]
    snapshot=SimpleNamespace(signal_asof_date=SIG,market_regime=regime,market_breadth=.5,
        benchmark_bars=[SimpleNamespace(**b) for b in benchmark],
        source_lineage={'excluded_symbols':excluded or [],'coverage_ratio':.9978})
    candidates=[{'symbol':s,'name':'普通股票','price':10.29,'score':80-i,'sector':f'行业{i}',
                 'as_of':SIG,'ret20':10,'ret60':20,'amount':40000000}
                for i,s in enumerate(['sh600000','sh600001'])]
    return {'snapshot':snapshot,'candidates':candidates,'csi300_bars':benchmark,
            'market_breadth':.5,'coverage_ratio':.9978,'top_sectors':['行业0','行业1'],
            'candidate_bars':{'sh600000':copy.deepcopy(bars),'sh600001':copy.deepcopy(bars)}}


def runtime(tmp_path,monkeypatch,mode='SHADOW'):
    monkeypatch.setenv('ASTOCK_ENV','test')
    monkeypatch.setenv('ASTOCK_AUDIT_PATH',str(tmp_path/'audit.json'))
    return TradingRuntime(db_path=str(tmp_path/'ledger.sqlite'),mode=mode)


@pytest.mark.parametrize('regime',list(MarketRegime))
def test_stale_stock_isolated_in_every_regime_without_blocking_fresh_peer(tmp_path,monkeypatch,regime):
    rt=runtime(tmp_path,monkeypatch)
    p=payload(regime)
    p['candidate_bars']['sh600001']=p['candidate_bars']['sh600001'][:-1]
    res=rt.step(EXEC,market_data=p,signal_asof_date=SIG,now=NOW)
    for account in ['balanced','aggressive']:
        assert res[account]['allowed_signal_count']==1
        assert 'sh600000' in rt.shadow_decisions[account]['target_portfolio']['target_positions']
        assert 'sh600001' not in rt.shadow_decisions[account]['target_portfolio']['target_positions']


@pytest.mark.parametrize('fault',['excluded','suspended','st','zero_volume','nonfinal','wrong_signal_date','missing_history','known_suspended','known_delisted'])
def test_quarantine_and_risk_flags_reject_only_the_affected_symbol(tmp_path,monkeypatch,fault):
    rt=runtime(tmp_path,monkeypatch);p=payload(MarketRegime.NEUTRAL)
    bar=p['candidate_bars']['sh600001'][-1]
    if fault=='excluded':p['snapshot'].source_lineage['excluded_symbols']=['sh600001']
    elif fault=='suspended':bar['is_suspended']=True
    elif fault=='st':bar['is_st']=True
    elif fault=='zero_volume':bar['volume']=0
    elif fault=='nonfinal':bar['is_final']=False
    elif fault=='wrong_signal_date':p['candidates'][1]['as_of']='2026-11-13'
    elif fault=='missing_history':del p['candidate_bars']['sh600001']
    elif fault=='known_suspended':p['snapshot'].source_lineage['nontrading_statuses']={'sh600001':'SUSPENDED'}
    elif fault=='known_delisted':p['snapshot'].source_lineage['nontrading_statuses']={'sh600001':{'status':'DELISTED'}}
    res=rt.step(EXEC,market_data=p,signal_asof_date=SIG,now=NOW)
    assert res['balanced']['allowed_signal_count']==1
    assert res['aggressive']['allowed_signal_count']==1


def test_unknown_st_flag_keeps_existing_name_policy(tmp_path,monkeypatch):
    rt=runtime(tmp_path,monkeypatch);p=payload(MarketRegime.NEUTRAL)
    p['candidate_bars']['sh600000'][-1]['is_st']=None
    res=rt.step(EXEC,market_data=p,signal_asof_date=SIG,now=NOW)
    assert res['balanced']['allowed_signal_count']==2


def seed_position(rt):
    fill=TradeFill(fill_id='seed',order_id='seed-order',client_order_id='seed-client',account_id='balanced',
        symbol='sh600000',name='普通股票',side=OrderSide.BUY,price=10,shares=100,
        amount=1000,commission=5,stamp_tax=0,transfer_fee=.01,slippage=0,realized_pnl=0,
        timestamp='2026-11-13T10:00:00')
    rt.ledger.apply_fill(fill)


def test_missing_global_data_still_allows_verified_hard_stop_without_atr_or_ma(tmp_path,monkeypatch):
    rt=runtime(tmp_path,monkeypatch,mode='ACTIVE');seed_position(rt)
    p=payload(MarketRegime.RISK_OFF,excluded=['sh600000'])
    p['csi300_bars']=[];p['snapshot'].benchmark_bars=[];p['candidates']=[]
    quote={'price':9,'timestamp':NOW.isoformat(),'fresh':True}
    res=rt.step(EXEC,quotes={'sh600000':quote},market_data=p,signal_asof_date=SIG,now=NOW)
    assert res['balanced']['entry_risk_freeze'] is True
    assert rt.ledger.get_positions('balanced')=={}
    with rt.ledger.connect() as conn:
        audits=[dict(r) for r in conn.execute('SELECT rule_name,message FROM risk_events')]
    assert any(a['rule_name']=='HARD_STOP_LOSS' for a in audits)
    assert any(a['rule_name']=='POSITION_INDICATOR_AVAILABILITY' for a in audits)


@pytest.mark.parametrize('quote',[{}, {'price':9,'fresh':False},
    {'price':9,'timestamp':'2026-11-16T15:00:00+08:00','fresh':True},
    {'price':9,'timestamp':'2026-11-17T09:20:00+08:00','fresh':True}])
def test_no_missing_or_stale_quote_can_force_sell_an_excluded_holding(tmp_path,monkeypatch,quote):
    rt=runtime(tmp_path,monkeypatch,mode='ACTIVE');seed_position(rt)
    p=payload(MarketRegime.RISK_OFF,excluded=['sh600000']);p['candidates']=[]
    rt.step(EXEC,quotes={'sh600000':quote} if quote else {},market_data=p,signal_asof_date=SIG,now=NOW)
    assert rt.ledger.get_positions('balanced')['sh600000'].shares==100
    with rt.ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fills WHERE side='SELL'").fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM risk_events WHERE rule_name='POSITION_QUOTE_FRESHNESS'").fetchone()[0]>=1


def test_old_snapshot_after_new_session_blocks_buys(tmp_path,monkeypatch):
    rt=runtime(tmp_path,monkeypatch);p=payload(MarketRegime.NEUTRAL)
    p['snapshot'].signal_asof_date='2026-11-13'
    p['csi300_bars']=p['csi300_bars'][:-3]
    res=rt.step(EXEC,market_data=p,signal_asof_date='2026-11-13',now=NOW)
    assert res['balanced']['entry_risk_freeze'] is True
    assert res['aggressive']['entry_risk_freeze'] is True


def test_zero_missing_indicators_do_not_create_a_trailing_or_ma_exit():
    from src.astock.trade.risk.position import PositionRiskRule
    from src.astock.trade.models import Position
    pos=Position(symbol='sh600000',shares=100,available_shares=100,cost_price=10,
                 highest_price=20,current_price=19,market_value=1900)
    should_exit,reason,_=PositionRiskRule().check_position_exit(
        pos,19,atr=0,ma20=0,consecutive_below_ma20=5)
    assert should_exit is False and reason=='HOLD'


@pytest.mark.parametrize('status',['SUSPENDED','DELISTED'])
def test_verified_nontrading_holding_is_not_sold_on_residual_fresh_quote(tmp_path,monkeypatch,status):
    rt=runtime(tmp_path,monkeypatch,mode='ACTIVE');seed_position(rt)
    p=payload(MarketRegime.RISK_OFF,excluded=['sh600000']);p['candidates']=[]
    p['snapshot'].source_lineage['nontrading_statuses']={'sh600000':{'status':status}}
    quote={'price':9,'timestamp':NOW.isoformat(),'fresh':True}
    rt.step(EXEC,quotes={'sh600000':quote},market_data=p,signal_asof_date=SIG,now=NOW)
    assert rt.ledger.get_positions('balanced')['sh600000'].shares==100
    with rt.ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fills WHERE side='SELL'").fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM risk_events WHERE rule_name='POSITION_NONTRADING_STATUS'").fetchone()[0]==1


def test_explicit_suspended_quote_is_never_an_exit_price():
    assert _live_exit_price({'price':9,'timestamp':NOW.isoformat(),'fresh':True,'is_suspended':True},EXEC,NOW) is None


def test_invalid_snapshot_cannot_bypass_data_freeze_with_fresh_candidates(tmp_path,monkeypatch):
    rt=runtime(tmp_path,monkeypatch);p=payload(MarketRegime.NEUTRAL)
    p['snapshot'].quality='INVALID'
    res=rt.step(EXEC,market_data=p,signal_asof_date=SIG,now=NOW)
    for account in ['balanced','aggressive']:
        assert res[account]['entry_risk_freeze'] is True
        assert res[account]['total_target_weight']==0


def test_explicit_snapshot_argument_enforces_excluded_lineage_and_real_coverage(tmp_path,monkeypatch):
    rt=runtime(tmp_path,monkeypatch);p=payload(MarketRegime.NEUTRAL,excluded=['sh600001'])
    snapshot=p.pop('snapshot');p.pop('coverage_ratio')
    res=rt.step(EXEC,market_data=p,snapshot=snapshot,signal_asof_date=SIG,now=NOW)
    assert res['balanced']['allowed_signal_count']==1
    assert res['aggressive']['allowed_signal_count']==1
    # A second explicitly supplied snapshot must not inherit a made-up 95% coverage.
    snapshot.source_lineage['coverage_ratio']=.3
    res=rt.step(EXEC,market_data=p,snapshot=snapshot,signal_asof_date=SIG,now=NOW)
    assert res['market_risk_audit']['passed'] is False
    assert res['balanced']['entry_risk_freeze'] is True
