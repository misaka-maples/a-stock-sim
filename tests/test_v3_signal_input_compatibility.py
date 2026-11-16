"""The server's legacy candidate payload must retain dated risk evidence in V3."""
import datetime
import pytest
from src.astock.trade.strategy.balanced import BalancedStrategy
from src.astock.trade.strategy.aggressive import AggressiveStrategy
from src.astock.trade.strategy.signal_inputs import leading_industries
from src.astock.trade.risk.market import MarketRiskRule
from src.astock.trade.models import MarketRegime


def data(as_of="2026-11-13"):
    end = datetime.date.fromisoformat(as_of)
    bars = [{"date": (end - datetime.timedelta(days=129-i)).isoformat(),
             "close": 10 + i * .01, "amount": 50_000_000, "volume": 100000} for i in range(130)]
    bars.append({"date": "2026-11-16", "close": 1000, "amount": 900_000_000, "volume": 100000})
    return {"candidates": [{"symbol": "sh600001", "as_of": as_of, "current": 22.58,
              "raw_signal_price": 22.58, "signal_price": 11.29, "adjustment_factor": .5,
              "ma20": 11.1, "ma60": 10.9, "ma120": 10.5,
              "ret_20": 10, "ret_60": 20, "atr_pct": .05, "score": 40}],
            "candidate_bars": {"sh600001": bars},
            "industry_map": {"sh600001": "机械"}, "top_sectors": ["机械"]}


@pytest.mark.parametrize("strategy", [BalancedStrategy, AggressiveStrategy])
def test_legacy_payload_keeps_raw_price_and_adjusted_risk_features(strategy):
    payload = data()
    signal = strategy().generate_signals("2026-11-13", payload, MarketRegime.RISK_OFF)[0]
    assert signal.price == 22.58
    assert signal.features["signal_price_adjusted"] == 11.29
    assert signal.sector == "机械"
    assert signal.ret20 == 10 and signal.ret60 == 20
    assert signal.amount == 50_000_000
    assert signal.atr == pytest.approx(1.129)
    gate = MarketRiskRule().check_risk_off_entry_gate(
        symbol=signal.symbol, price=signal.features["signal_price_adjusted"],
        bars=payload["candidate_bars"][signal.symbol], sector=signal.sector,
        top_sectors=payload["top_sectors"], ret20=signal.ret20,
        ret60=signal.ret60, amount=signal.amount,
        signal_asof=signal.signal_asof, current_asof="2026-11-16")
    assert gate.passed


@pytest.mark.parametrize("strategy", [BalancedStrategy, AggressiveStrategy])
def test_native_payload_remains_compatible(strategy):
    p = {"candidates": [{"symbol": "sh600001", "price": 15, "ret20": 2,
                         "ret60": 3, "sector": "银行", "amount": 80_000_000, "atr": .7}]}
    s = strategy().generate_signals("2026-11-13", p, MarketRegime.RISK_OFF)[0]
    assert (s.price, s.ret20, s.ret60, s.amount, s.sector, s.atr) == (15, 2, 3, 80_000_000, "银行", .7)


def test_missing_top_industries_still_rejects_valid_legacy_signal():
    p = data()
    s = BalancedStrategy().generate_signals("2026-11-13", p, MarketRegime.RISK_OFF)[0]
    audit = MarketRiskRule().check_risk_off_entry_gate(
        symbol=s.symbol, price=s.features["signal_price_adjusted"], bars=p["candidate_bars"][s.symbol],
        sector=s.sector, top_sectors=[], ret20=s.ret20, ret60=s.ret60, amount=s.amount,
        signal_asof=s.signal_asof, current_asof="2026-11-16")
    assert not audit.passed
    assert "Sector" in audit.message


def test_expired_signal_date_is_preserved_for_gate_rejection():
    p = data(as_of="2026-11-10")
    s = BalancedStrategy().generate_signals("2026-11-13", p, MarketRegime.RISK_OFF)[0]
    assert s.signal_asof == "2026-11-10"
    audit = MarketRiskRule().check_risk_off_entry_gate(
        symbol=s.symbol, price=s.features["signal_price_adjusted"], bars=p["candidate_bars"][s.symbol],
        sector=s.sector, top_sectors=[s.sector], ret20=s.ret20, ret60=s.ret60, amount=s.amount,
        signal_asof=s.signal_asof, current_asof="2026-11-16")
    assert not audit.passed
    assert "expired" in audit.message


def test_industry_screen_uses_history_not_selected_winners_or_future_bars():
    d = data(); bars = d['candidate_bars']['sh600001']
    history = {}; industry_map = {}
    for k in range(6):
        sym = f'sh60000{k}'
        history[sym] = bars
        industry_map[sym] = '机械' if k < 3 else '银行'
    result = leading_industries('2026-11-13', history, industry_map)
    assert result['top_sectors'] == ['机械', '银行']
    assert result['industry_stats']['机械']['stock_count'] == 3
    assert 0 < result['industry_stats']['机械']['mean_ret20'] < 3
    assert result['industry_stats']['机械']['breadth_ma20'] == 1
    # No sector can qualify from a single hand-picked stock.
    assert leading_industries('2026-11-13', history, industry_map, symbols=['sh600000'])['top_sectors'] == []
    # Missing dates and liquidity must not be repaired with future observations.
    poor_history = {s: [{**b, 'amount': 0} for b in history[s] if b['date'] <= '2026-11-13'] for s in history}
    assert leading_industries('2026-11-13', poor_history, industry_map)['top_sectors'] == []


def test_industry_screen_missing_metadata_fail_closed():
    d = data()
    assert leading_industries('2026-11-13', d['candidate_bars'], {})['top_sectors'] == []


def test_runtime_accepts_legacy_payload_with_candidate_bars_in_shadow(tmp_path, monkeypatch):
    from src.astock.trade.runtime import TradingRuntime
    monkeypatch.setenv('ASTOCK_ENV', 'test')
    monkeypatch.setenv('ASTOCK_AUDIT_PATH', str(tmp_path / 'audit.json'))
    rt = TradingRuntime(db_path=str(tmp_path/'isolated.sqlite'), mode='SHADOW')
    payload = data()
    end = datetime.date(2026, 11, 13)
    payload['csi300_bars'] = [
        {'date': (end - datetime.timedelta(days=149-i)).isoformat(), 'close': 4500-i*3}
        for i in range(150)]
    payload['market_breadth'] = .3
    payload['coverage_ratio'] = 1
    res = rt.step('2026-11-16', market_data=payload, signal_asof_date='2026-11-13')
    assert res['regime'] == 'RISK_OFF'
    assert res['market_risk_audit']['passed']
    assert res['balanced']['total_target_weight'] > 0
    assert res['balanced']['total_target_weight'] <= .30
    assert res['aggressive']['total_target_weight'] > 0
    assert res['aggressive']['total_target_weight'] <= .50

    # Industry absence continues to prevent all weak-market entries.
    payload['top_sectors'] = []
    res = rt.step('2026-11-16', market_data=payload, signal_asof_date='2026-11-13')
    assert res['balanced']['total_target_weight'] == 0
    assert res['aggressive']['total_target_weight'] == 0


def test_adjusted_weak_market_comparison_does_not_pass_from_large_raw_price():
    p = data()
    p['candidates'][0]['signal_price'] = 9.0
    p['candidates'][0]['raw_signal_price'] = 18.0
    p['candidates'][0]['current'] = 18.0
    s = BalancedStrategy().generate_signals('2026-11-13', p, MarketRegime.RISK_OFF)[0]
    audit = MarketRiskRule().check_risk_off_entry_gate(
        symbol=s.symbol, price=s.features['signal_price_adjusted'], bars=p['candidate_bars'][s.symbol],
        sector=s.sector, top_sectors=[s.sector], ret20=s.ret20, ret60=s.ret60, amount=s.amount,
        signal_asof=s.signal_asof, current_asof='2026-11-16')
    assert s.price == 18
    assert not audit.passed
    assert 'MA60' in audit.message


def test_runtime_legacy_signal_can_fill_both_isolated_accounts_in_risk_off(tmp_path, monkeypatch):
    from src.astock.trade.runtime import TradingRuntime
    monkeypatch.setenv('ASTOCK_ENV', 'test')
    monkeypatch.setenv('ASTOCK_AUDIT_PATH', str(tmp_path / 'audit.json'))
    rt = TradingRuntime(db_path=str(tmp_path/'isolated.sqlite'), mode='ACTIVE')
    payload = data()
    end = datetime.date(2026, 11, 13)
    payload['csi300_bars'] = [
        {'date': (end - datetime.timedelta(days=149-i)).isoformat(), 'close': 4500-i*3}
        for i in range(150)]
    payload['market_breadth'] = .3
    payload['coverage_ratio'] = 1
    quotes = {'sh600001': {'price': 22.58, 'current': 22.58, 'pre_close': 22.58,
                           'name': 'Test stock', 'sector': '机械'}}
    res = rt.step('2026-11-16', quotes=quotes, market_data=payload, signal_asof_date='2026-11-13')
    assert res['regime'] == 'RISK_OFF'
    assert res['balanced']['executed_fills'] == 1
    assert res['aggressive']['executed_fills'] == 1
    for account, cap in [('balanced', .30), ('aggressive', .50)]:
        balance = rt.ledger.get_balance(account)
        assert 0 < balance.stock_market_value / balance.total_equity <= cap
        assert rt.ledger.get_positions(account)['sh600001'].tradable_shares == 0


def test_snapshot_benchmark_is_shared_with_both_account_data_checks(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from src.astock.trade.runtime import TradingRuntime
    monkeypatch.setenv('ASTOCK_ENV','test')
    monkeypatch.setenv('ASTOCK_AUDIT_PATH',str(tmp_path/'audit.json'))
    rt=TradingRuntime(db_path=str(tmp_path/'snapshot.sqlite'),mode='SHADOW')
    payload=data()
    end=datetime.date(2026,11,13)
    snapshot=SimpleNamespace(market_regime=MarketRegime.RISK_OFF,market_breadth=.3,
        signal_asof_date='2026-11-13',benchmark_bars=[
            SimpleNamespace(date=(end-datetime.timedelta(days=149-i)).isoformat(),close=4500-i*3)
            for i in range(150)])
    res=rt.step('2026-11-16',market_data=payload,snapshot=snapshot)
    assert res['market_risk_audit']['passed']
    for account in ['balanced','aggressive']:
        assert res[account]['missing_market_data'] is False
        assert res[account]['entry_risk_freeze'] is False
        assert res[account]['total_target_weight']>0
    assert 'csi300_bars' not in payload  # The caller's payload is not mutated.
