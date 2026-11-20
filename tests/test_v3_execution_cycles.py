"""Entry cycles prohibit accidental refill across polls, sessions and restarts."""
import json
from concurrent.futures import ThreadPoolExecutor

from src.astock.trade.execution_cycles import restrict_entry_cycle
from src.astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from src.astock.trade.models import TargetPortfolio, TargetPosition, MarketRegime, TradeFill, OrderSide

POLICY={'rebalance_every':5,'refill_vacancies':False,'cooldown_days':3}


def db(tmp_path):
    obj=SQLiteAccountLedger(str(tmp_path/'ledger.sqlite'))
    for account in ['balanced','aggressive']:obj.init_account(account,account,100000.)
    return obj


def plan(account='balanced',targets=None,frozen=False):
    targets=targets if targets is not None else {'sh600001':.10}
    positions={s:TargetPosition(s,w,reason='QUALIFIED_SIGNAL') for s,w in targets.items()}
    total=sum(targets.values())
    return TargetPortfolio(account_id=account,target_date='2026-11-17',target_positions=positions,
        regime=MarketRegime.RISK_OFF,regime_cap=.30 if account=='balanced' else .50,
        total_weight=total,cash_weight=1-total,constraints={'is_frozen':frozen})


def trade(ledger,tag,side=OrderSide.BUY,day='2026-11-13',symbol='sh600001',account='balanced'):
    fill=TradeFill(fill_id='fill-'+tag,order_id='order-'+tag,client_order_id='client-'+tag,
        account_id=account,symbol=symbol,name='Test',side=side,price=10.,shares=100,
        amount=1000.,commission=5.,stamp_tax=0.,transfer_fee=.01,slippage=0.,
        realized_pnl=0.,timestamp=day+'T10:00:00')
    ledger.apply_fill(fill)


def apply(ledger,p=None,signal='2026-11-16',execution='2026-11-17',account='balanced',positions=None,**kwargs):
    return restrict_entry_cycle(ledger,account,signal,execution,p or plan(account),
        ledger.get_positions(account) if positions is None else positions,POLICY,**kwargs)


def financial_state(ledger):
    with ledger.connect() as conn:
        return {t:[tuple(r) for r in conn.execute('SELECT * FROM '+t+' ORDER BY rowid')]
                for t in ['accounts','positions','position_lots','orders','fills','cash_journal']}


def test_none_policy_preserves_existing_api_without_even_a_ledger():
    original=plan()
    result,diag=restrict_entry_cycle(None,'balanced','2026-11-16','2026-11-17',original,{},None)
    assert result is original and diag['reason']=='legacy_passthrough'


def test_same_signal_retries_only_frozen_initial_targets_and_never_raises_weights(tmp_path):
    ledger=db(tmp_path);before=financial_state(ledger)
    first,diag=apply(ledger,plan(targets={'sh600001':.10}))
    assert diag['cycle_started'] and first.target_positions['sh600001'].target_weight==.10
    retry,diag=apply(ledger,plan(targets={'sh600001':.25,'sh600002':.10}))
    assert set(retry.target_positions)=={'sh600001'}
    assert retry.target_positions['sh600001'].target_weight==.10
    assert diag['blocked_symbols']['sh600002']=='not_in_original_cycle'
    smaller,_=apply(ledger,plan(targets={'sh600001':.05}))
    assert smaller.target_positions['sh600001'].target_weight==.05
    missing,_=apply(ledger,plan(targets={'sh600002':.10}))
    assert missing.target_positions=={}  # Previously valid symbols cannot bypass current rejection.
    assert financial_state(ledger)==before


def test_any_same_day_exit_blocks_all_new_buys_in_same_tick_and_after_restart(tmp_path):
    ledger=db(tmp_path);trade(ledger,'old',symbol='sh600009')
    apply(ledger,plan(targets={'sh600001':.10}))
    ledger.roll_t1('2026-11-17')
    trade(ledger,'stop',OrderSide.SELL,'2026-11-17',symbol='sh600009')
    for obj in [ledger,SQLiteAccountLedger(str(ledger.db_path))]:
        result,diag=apply(obj,plan(targets={'sh600001':.25,'sh600002':.10}))
        assert result.target_positions=={}
        assert diag['reason']=='same_day_sell_blocks_new_entries'
        assert diag['sold_today']==['sh600009']


def test_already_bought_cycle_target_cannot_be_bought_again_from_stale_empty_positions(tmp_path):
    ledger=db(tmp_path);apply(ledger)
    trade(ledger,'bought',day='2026-11-17')
    result,diag=apply(ledger,positions={})
    assert result.target_positions=={}
    assert diag['blocked_symbols']['sh600001']=='cycle_target_already_bought_today'


def test_waits_five_exchange_sessions_not_five_calendar_days(tmp_path):
    ledger=db(tmp_path);apply(ledger)
    too_early,diag=apply(ledger,plan(targets={'sh600002':.10}),signal='2026-11-20',execution='2026-11-23')
    assert too_early.target_positions=={} and diag['sessions_since_cycle']==4
    assert diag['reason']=='waiting_for_scheduled_cycle'
    due,diag=apply(ledger,plan(targets={'sh600002':.10}),signal='2026-11-23',execution='2026-11-24')
    assert set(due.target_positions)=={'sh600002'}
    assert diag['sessions_since_cycle']==5 and diag['cycle_started']


def test_account_cycles_and_exit_blocks_are_independent(tmp_path):
    ledger=db(tmp_path);apply(ledger)
    trade(ledger,'bal-old',symbol='sh600009')
    ledger.roll_t1('2026-11-17');trade(ledger,'bal-stop',OrderSide.SELL,'2026-11-17',symbol='sh600009')
    balanced,diag=apply(ledger)
    aggressive,agg_diag=apply(ledger,plan('aggressive'),account='aggressive')
    assert balanced.target_positions=={} and diag['reason']=='same_day_sell_blocks_new_entries'
    assert set(aggressive.target_positions)=={'sh600001'} and agg_diag['cycle_started']


def test_three_session_sold_symbol_cooldown_does_not_block_other_eligible_stocks(tmp_path):
    ledger=db(tmp_path);trade(ledger,'old');ledger.roll_t1('2026-11-17')
    trade(ledger,'sold',OrderSide.SELL,'2026-11-17')
    result,diag=apply(ledger,plan(targets={'sh600001':.10,'sh600002':.10}),
        signal='2026-11-17',execution='2026-11-18')
    assert set(result.target_positions)=={'sh600002'}
    assert diag['blocked_symbols']['sh600001']=='sell_cooldown'


def test_cooldown_expiry_is_after_three_complete_sessions(tmp_path):
    ledger=db(tmp_path);trade(ledger,'old');ledger.roll_t1('2026-11-17')
    trade(ledger,'sold',OrderSide.SELL,'2026-11-17')
    result,diag=apply(ledger,signal='2026-11-20',execution='2026-11-23')
    assert set(result.target_positions)=={'sh600001'}  # Tue->Mon: four sessions, weekend excluded.
    assert 'sh600001' not in diag['blocked_symbols']


def test_held_positions_survive_empty_signals_or_risk_freeze_without_forced_liquidation(tmp_path):
    ledger=db(tmp_path);trade(ledger,'held',symbol='sh600009')
    result,diag=apply(ledger,plan(targets={},frozen=True))
    assert set(result.target_positions)=={'sh600009'}
    assert result.target_positions['sh600009'].reason=='HOLD_EXISTING'
    assert result.total_weight>0 and diag['reason']=='risk_freeze'
    with ledger.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM execution_cycles').fetchone()[0]==0


def test_preview_is_strictly_read_only_and_does_not_create_cycle_table(tmp_path):
    ledger=db(tmp_path);before=financial_state(ledger)
    result,diag=apply(ledger,persist=False)
    assert set(result.target_positions)=={'sh600001'} and diag['cycle_preview']
    assert diag['cycle_started'] is False
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='execution_cycles'").fetchone()[0]==0
    assert financial_state(ledger)==before
    apply(ledger)
    with ledger.connect() as conn:rows=[tuple(r) for r in conn.execute('SELECT * FROM execution_cycles')]
    apply(ledger,persist=False)
    with ledger.connect() as conn:assert [tuple(r) for r in conn.execute('SELECT * FROM execution_cycles')]==rows


def test_concurrent_polls_start_exactly_one_cycle_and_keep_first_weight(tmp_path):
    ledger=db(tmp_path)
    def run(_):
        obj=SQLiteAccountLedger(str(ledger.db_path))
        return apply(obj)[1]
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(run,range(8)))
    assert sum(r['cycle_started'] for r in results)==1
    with ledger.connect() as conn:
        rows=conn.execute('SELECT * FROM execution_cycles').fetchall()
    assert len(rows)==1 and json.loads(rows[0]['targets_json'])['sh600001']['target_weight']==.10


def test_daily_entry_and_same_day_intraday_refill_after_stop_loss(tmp_path):
    from src.astock.trade.execution_cycles import filter_cycle_ineligible_signals
    from src.astock.trade.models import Signal
    daily_refill_policy={'rebalance_every':1,'refill_vacancies':True,'cooldown_days':3}
    ledger=db(tmp_path)
    # Pre-existing T+1 tradable holding sh600009 from 2026-11-13
    trade(ledger,'old-hold',OrderSide.BUY,'2026-11-13',symbol='sh600009',account='aggressive')
    ledger.roll_t1('2026-11-17')
    # 09:30 Morning cycle starts with sh600001
    p1,d1=restrict_entry_cycle(ledger,'aggressive','2026-11-16','2026-11-17',
        plan('aggressive',{'sh600001':.25}),ledger.get_positions('aggressive'),daily_refill_policy)
    assert 'sh600001' in p1.target_positions and d1['cycle_started'] and d1['reason']=='scheduled_cycle'
    trade(ledger,'morning-buy',OrderSide.BUY,'2026-11-17',symbol='sh600001',account='aggressive')
    # 10:15 Same-day stop-loss sell of pre-existing sh600009
    trade(ledger,'intraday-stop',OrderSide.SELL,'2026-11-17',symbol='sh600009',account='aggressive')
    # 10:15:03 Same day (same signal_day='2026-11-16'): sh600002 refills immediately, sh600009 blocked
    p2,d2=restrict_entry_cycle(ledger,'aggressive','2026-11-16','2026-11-17',
        plan('aggressive',{'sh600009':.25,'sh600002':.25}),ledger.get_positions('aggressive'),daily_refill_policy)
    assert d2['reason']=='explicit_refill_enabled'
    assert set(p2.target_positions)=={'sh600001','sh600002'}
    assert d2['blocked_symbols']['sh600009']=='sell_cooldown'
    # Filter helper also excludes sh600001 (bought today) and sh600009 (sold today) prior to plan_targets
    sigs=[Signal(run_id='r',signal_asof='2026-11-16',symbol='sh600009',name='Old',rank=1,score=95.,sector='S',price=10.),
          Signal(run_id='r',signal_asof='2026-11-16',symbol='sh600002',name='B',rank=2,score=90.,sector='S2',price=10.)]
    filtered=filter_cycle_ineligible_signals(ledger,'aggressive','2026-11-17',sigs,daily_refill_policy)
    assert [s.symbol for s in filtered]==['sh600002']
    # Next trading day (signal_day='2026-11-17', execution_day='2026-11-18'): no 5-session wait
    p3,d3=restrict_entry_cycle(ledger,'aggressive','2026-11-17','2026-11-18',
        plan('aggressive',{'sh600003':.25}),ledger.get_positions('aggressive'),daily_refill_policy)
    assert d3['reason']=='scheduled_cycle' and d3['sessions_since_cycle']==1 and d3['cycle_started']
    assert 'sh600003' in p3.target_positions
