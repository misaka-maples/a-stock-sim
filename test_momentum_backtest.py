"""Offline regression checks for timing, tradability and cash conservation."""
import copy
import datetime as dt

import pytest

from backtest import run_backtest, BacktestEngine
from momentum_backtest import MomentumBacktestEngine
from research import FixtureFeed
from strategy_rules import MomentumConfig
from trade_calendar import is_trading_day


def fixture_data(count=190, symbols=("sh600096",)):
    dates, date = [], dt.date(2025, 1, 2)
    while len(dates) < count:
        if is_trading_day(date):
            dates.append(date.isoformat())
        date += dt.timedelta(days=1)
    stocks = {}
    for n, symbol in enumerate(symbols):
        bars = []
        for i, date in enumerate(dates):
            close = round(20 + i*.03 + n, 2)
            opening = round(close-.01, 2)
            bars.append(dict(date=date, open=opening, close=close, high=round(close+.05, 2),
                             low=round(opening-.05, 2), raw_open=opening, raw_close=close,
                             raw_high=round(close+.05, 2), raw_low=round(opening-.05, 2),
                             raw_pre_close=round(close-.03, 2), adjustment_factor=1., volume=100000))
        stocks[symbol] = dict(name="测试股份", bars=bars)
    benchmark = [dict(date=date, open=100+i*.01, close=100+i*.01,
                      high=101+i*.01, low=99+i*.01, volume=1000000)
                 for i, date in enumerate(dates)]
    return dict(benchmark=benchmark, stocks=stocks), dates


def engine(data, dates, start=130, end=135, **kwargs):
    config = kwargs.pop("config", MomentumConfig(max_positions=1, max_stock_weight=.5,
                                                risk_per_trade=0, volatility_target=0, stop_loss_mode="daily_close",
                                                max_sector_weight=1, rebalance_every=1,
                                                min_holding_days=0, cooldown_days=0,
                                                refill_vacancies=False))
    return MomentumBacktestEngine(start_date=dates[start], end_date=dates[end],
                                  symbols=list(data["stocks"]), feed=FixtureFeed(data),
                                  config=config, **kwargs)


def set_price(bar, close, opening=None, volume=None):
    opening = close if opening is None else opening
    for field, value in dict(open=opening, close=close, high=max(close, opening)+.05,
                             low=min(close, opening)-.05).items():
        bar[field] = bar["raw_"+field] = value
    if volume is not None:
        bar["volume"] = volume


def test_previous_completed_signal_fills_next_raw_open_and_warmup():
    data, dates = fixture_data()
    bar = data["stocks"]["sh600096"]["bars"][130]
    bar["raw_open"] = bar["open"] = 24.10
    bar["raw_low"] = bar["low"] = 23.80
    bar["raw_high"] = bar["high"] = 24.20
    result = engine(data, dates).run()
    fill = result["fills"][0]
    assert fill["signal_date"] == dates[129] < fill["date"] == dates[130]
    assert fill["price"] == pytest.approx(24.12)
    assert result["data_coverage"]["sh600096"]["pre_start_bars"] >= 125
    assert result["summary"]["total_fees"] > 0  # unclosed BUY is included


def test_future_values_cannot_change_earlier_fills():
    data, dates = fixture_data()
    baseline = engine(data, dates, end=132).run()
    changed = copy.deepcopy(data)
    for bar in changed["stocks"]["sh600096"]["bars"][133:]:
        set_price(bar, 999)
    assert engine(changed, dates, end=132).run()["fills"] == baseline["fills"]


def test_missing_raw_prices_fail_closed():
    data, dates = fixture_data()
    del data["stocks"]["sh600096"]["bars"][130]["raw_open"]
    with pytest.raises(ValueError, match="raw_prices_missing"):
        engine(data, dates).run()


@pytest.mark.parametrize("zero_volume", [True, False])
def test_zero_volume_and_one_price_limit_never_fill(zero_volume):
    data, dates = fixture_data()
    bars = data["stocks"]["sh600096"]["bars"]
    bar = bars[130]
    if zero_volume:
        bar["volume"] = 0
    else:
        price = round(bars[129]["raw_close"]*1.1, 2)
        for field in ("open", "close", "high", "low"):
            bar[field] = bar["raw_"+field] = price
    result = engine(data, dates, end=130).run()
    assert not result["fills"]
    assert result["rejections"]


def test_partial_liquidation_preserves_cash_fees_and_t1():
    data, dates = fixture_data()
    bars = data["stocks"]["sh600096"]["bars"]
    set_price(bars[131], 21.5, opening=23.8)
    for i in range(132, 137):
        set_price(bars[i], 21.55, opening=21.5, volume=20)
        bars[i]["raw_pre_close"] = bars[i-1]["raw_close"]
    result = engine(data, dates, end=135).run()
    sells = [f for f in result["fills"] if f["side"] == "SELL"]
    assert sells and all(s["status"] == "PARTIALLY_FILLED" for s in sells)
    assert sells[0]["signal_date"] == dates[131] and sells[0]["date"] == dates[132]
    assert all(t["buy_date"] < t["sell_date"] for t in result["trades"])
    assert result["summary"]["total_fees"] == pytest.approx(round(sum(f["fees"] for f in result["fills"]), 2))
    positions = result["open_positions"]
    unrealized = sum(p["shares"]*(p["last_price"]-p["buy_price"])-p["remaining_buy_fees"]
                     for p in positions.values())
    realized = sum(t["net_pnl"] for t in result["trades"])
    assert result["summary"]["total_pnl"] == pytest.approx(realized+unrealized, abs=.03)
    assert result["summary"]["annualized_turnover"] > 0
    assert 0 < result["summary"]["avg_gross_exposure"] < 1
    assert result["summary"]["industry_exposure_max"]["未知行业"] > 0
    assert result["summary"]["excess_return_pct"] == result["summary"]["alpha_pct"]


def test_stop_loss_configuration_changes_next_day_exit():
    data, dates = fixture_data()
    bars = data["stocks"]["sh600096"]["bars"]
    # A small loss is still above MA20; only the configurable hard stop fires.
    set_price(bars[131], bars[130]["raw_open"]*.997)
    tight = engine(data, dates, end=132)
    loose = engine(data, dates, end=132)
    tight.config.stop_loss_pct = -.2
    loose.config.stop_loss_pct = -20
    a, b = tight.run(), loose.run()
    assert any(f["side"] == "SELL" for f in a["fills"])
    assert not any(f["side"] == "SELL" for f in b["fills"])


def test_suspension_valuation_uses_last_close_not_cost():
    data, dates = fixture_data()
    result = engine(data, dates, end=130).run()
    position = result["open_positions"]["sh600096"]
    data["stocks"]["sh600096"]["bars"] = [b for b in data["stocks"]["sh600096"]["bars"]
                                               if b["date"] != dates[131]]
    suspended = engine(data, dates, end=131).run()
    expected = position["shares"] * position["last_price"]
    assert suspended["equity_curve"][-1]["market_value"] == pytest.approx(expected)
    assert "terminal_stale_marks" in " ".join(suspended["warnings"])


def test_industry_and_historical_eligibility_caps():
    data, dates = fixture_data(symbols=("sh600096", "sh600036", "sh600900"))
    config = MomentumConfig(max_positions=3, max_stock_weight=.3, risk_per_trade=0,
                            volatility_target=0, max_sector_weight=.35, rebalance_every=1)
    result = engine(data, dates, end=131, config=config).run()
    assert len([f for f in result["fills"] if f["side"] == "BUY"]) == 1
    assert any(r["reason"] == "industry_cap" for r in result["rejections"])
    intervals = {s: [{"start": dates[132]}] for s in data["stocks"]}
    restricted = engine(data, dates, end=131, config=config, universe_intervals=intervals).run()
    assert not restricted["fills"]


def test_public_entry_point_routes_momentum_without_touching_legacy():
    data, dates = fixture_data()
    result = run_backtest(start_date=dates[130], end_date=dates[131],
                          strategy_name="Momentum_Rotation_Pro", feed=FixtureFeed(data),
                          symbols=list(data["stocks"]))
    assert result["execution_model"].startswith("previous_close_signal_next_raw_open")
    assert result["summary"]["universe_mode"] == "fixed_pool_research_only"


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("strategy", ["Momentum_Rotation", "Momentum_Rotation_Pro"])
def test_direct_public_class_and_wrapper_agree(explicit, strategy):
    data, dates = fixture_data()
    options = dict(start_date=dates[130], end_date=dates[135],
                   strategy_name=strategy,
                   industry_map={"sh600096": "化工"})
    if explicit:
        options.update(max_positions=3, max_stock_weight=.33, stop_loss_pct=-4)
    wrapped = run_backtest(feed=FixtureFeed(data), **options)
    direct = BacktestEngine(feed=FixtureFeed(data), **options).run()
    assert direct == wrapped
    assert direct["parameters"]["stop_loss_pct"] == (-4 if explicit else -6)
    assert direct["parameters"]["max_positions"] == (3 if explicit else 6)


def test_completed_trade_cooldown_blocks_three_full_days():
    data, dates = fixture_data()
    bars = data["stocks"]["sh600096"]["bars"]
    set_price(bars[131], 22.3, opening=23.8)
    set_price(bars[132], 23.8, opening=22.3)
    bars[132]["raw_pre_close"] = 22.3
    for i in range(133, 138):
        set_price(bars[i], 23.8+(i-132)*.04, opening=23.79+(i-132)*.04)
        bars[i]["raw_pre_close"] = bars[i-1]["raw_close"]
    obj = engine(data, dates, end=137)
    obj.config.cooldown_days = 3
    result = obj.run()
    sells = [f for f in result["fills"] if f["side"] == "SELL"]
    buys = [f for f in result["fills"] if f["side"] == "BUY"]
    assert sells[0]["date"] == dates[132]
    assert len(buys) >= 2
    assert buys[1]["date"] == dates[136]


def test_rank_exit_observes_minimum_holding_days():
    data, dates = fixture_data(symbols=("sh600096", "sh600036"))
    a, b = (data["stocks"][s]["bars"] for s in ("sh600096", "sh600036"))
    for i, bar in enumerate(a):
        set_price(bar, 20+i*.10, opening=19.99+i*.10)
        bar["raw_pre_close"] = 20+(i-1)*.10
    for i in range(131, 140):
        previous = b[i-1]["raw_close"]
        set_price(b[i], round(previous*1.04, 2), opening=round(previous*1.005, 2))
        b[i]["raw_pre_close"] = previous
    obj = engine(data, dates, end=138)
    obj.config.rank_buffer = 0
    obj.config.min_holding_days = 5
    result = obj.run()
    first_sell = next(f for f in result["fills"] if f["side"] == "SELL")
    assert first_sell["symbol"] == "sh600096"
    assert first_sell["date"] == dates[136]
    assert "排名" in result["trades"][0]["reason"]


def test_market_filter_prevents_new_risk_when_benchmark_below_ma120():
    data, dates = fixture_data()
    for i, bar in enumerate(data["benchmark"]):
        bar["close"] = 100-i*.05
    obj = engine(data, dates)
    obj.config.market_filter = True
    assert not obj.run()["fills"]


def test_non_main_board_and_risk_warning_stocks_do_not_enter_or_rank():
    data, dates = fixture_data(symbols=("sh600096", "sz300750"))
    data["stocks"]["sh600096"]["name"] = "ST测试"
    result = engine(data, dates).run()
    assert not result["fills"]
    assert "non_main_board_excluded" in " ".join(result["warnings"])


def test_missing_benchmark_and_stock_signal_day_does_not_reuse_old_signal():
    data, dates = fixture_data()
    missing = dates[129]
    data["benchmark"] = [b for b in data["benchmark"] if b["date"] != missing]
    data["stocks"]["sh600096"]["bars"] = [b for b in data["stocks"]["sh600096"]["bars"]
                                               if b["date"] != missing]
    result = engine(data, dates, end=130).run()
    assert not result["fills"]
    assert "benchmark_coverage_gap" in " ".join(result["warnings"])


def test_symbol_aliases_are_normalized_and_deduplicated_before_fetch_and_rank():
    data, dates = fixture_data()
    canonical = engine(data, dates)
    aliases = MomentumBacktestEngine(start_date=dates[130], end_date=dates[135],
                                     symbols=["600096", "SH600096", "sh600096"],
                                     feed=FixtureFeed(data), config=canonical.config)
    assert aliases.symbols == ["sh600096"]
    assert aliases.run()["fills"] == canonical.run()["fills"]


def test_dated_non_st_status_overrides_current_st_name():
    data, dates = fixture_data()
    data["stocks"]["sh600096"]["name"] = "ST测试股份"
    for bar in data["stocks"]["sh600096"]["bars"]:
        bar["is_st"] = False
    result = engine(data, dates, end=130).run()
    assert result["fills"] and result["fills"][0]["side"] == "BUY"


def test_missing_held_signal_day_does_not_fabricate_next_day_rank_exit():
    data, dates = fixture_data()
    data["stocks"]["sh600096"]["bars"] = [b for b in data["stocks"]["sh600096"]["bars"]
                                                if b["date"] != dates[131]]
    obj = engine(data, dates, end=132)
    obj.config.rank_buffer = 0
    result = obj.run()
    assert [f["side"] for f in result["fills"]] == ["BUY"]
    assert "sh600096" in result["open_positions"]


def test_post_fill_commission_and_mark_reduce_lots_to_risk_budget():
    data, dates = fixture_data()
    for bar in data["stocks"]["sh600096"]["bars"]:
        for key in ("open", "close"):
            bar["raw_"+key] = 10.
        bar["raw_high"], bar["raw_low"], bar["raw_pre_close"] = 10.05, 9.95, 10.
        bar["adjustment_factor"] = bar["close"] / bar["raw_close"]
    config = MomentumConfig(max_positions=1, max_stock_weight=.1, risk_per_trade=0,
                            volatility_target=0, max_sector_weight=.1, rebalance_every=1)
    obj = engine(data, dates, end=130, config=config, initial_cash=100051.,
                 commission_rate=0, transfer_fee_rate=0, slippage_rate=0)
    result = obj.run()
    fill = result["fills"][0]
    assert fill["shares"] == 900 and fill["status"] == "PARTIALLY_FILLED"
    committed = fill["shares"] * fill["price"] + fill["fees"]
    post_equity = obj.initial_cash - fill["fees"]
    assert committed <= .1 * post_equity


def test_dated_industry_metadata_overrides_current_snapshot_for_caps_and_exposure():
    data, dates = fixture_data(symbols=("sh600096", "sh600036"))
    for index, item in enumerate(data["stocks"].values()):
        for bar in item["bars"]:
            bar["industry"] = "历史行业" + str(index)
    config = MomentumConfig(max_positions=3, max_stock_weight=.3, risk_per_trade=0,
                            volatility_target=0, max_sector_weight=.35, rebalance_every=1)
    result = engine(data, dates, end=130, config=config,
                    industry_map={symbol: "当前同一行业" for symbol in data["stocks"]}).run()
    assert len(result["fills"]) == 2
    assert set(result["equity_curve"][-1]["industry_exposure"]) == {"历史行业0", "历史行业1"}


def test_entry_guard_blocks_limit_down_even_when_day_has_other_trades():
    data, dates = fixture_data()
    bars=data['stocks']['sh600096']['bars'];previous=bars[129]['raw_close']
    lower=round(previous*.9,2);bar=bars[130]
    set_price(bar,lower+.5,opening=lower)
    result=engine(data,dates,end=130).run()
    assert not result['fills']
    assert result['rejections'][0]['reason']=='entry_limit_down'


def test_open_entry_guard_never_uses_later_close_to_cancel_buy():
    data, dates = fixture_data()
    bars=data['stocks']['sh600096']['bars'];bar=bars[130]
    opening=bar['raw_open'];set_price(bar,round(opening*.94,2),opening=opening)
    result=engine(data,dates,end=130).run()
    assert result['fills'] and result['fills'][0]['date']==dates[130]
    assert not [r for r in result['rejections'] if r['reason'].startswith('entry_')]


def test_backtest_gap_down_rejection_uses_same_shared_rule():
    data, dates=fixture_data();bars=data['stocks']['sh600096']['bars'];previous=bars[129]['raw_close']
    set_price(bars[130],previous,opening=round(previous*.96,2))
    result=engine(data,dates,end=130).run()
    assert not result['fills'] and result['rejections'][0]['reason']=='entry_gap_down'


@pytest.mark.parametrize("gap", [False, True])
def test_intraday_stop_uses_threshold_or_worse_gap_not_future_close(gap):
    data, dates = fixture_data()
    obj = engine(data, dates, end=131)
    obj.config.stop_loss_mode = "intraday"
    initial = engine(copy.deepcopy(data), dates, end=130).run()
    cost = initial["open_positions"]["sh600096"]["cost_price"]
    stop = cost * .94
    bar = data["stocks"]["sh600096"]["bars"][131]
    opening = stop-.2 if gap else 23.9
    set_price(bar, 23.9, opening=opening)
    bar["raw_low"] = bar["low"] = stop-.3
    result = obj.run()
    sell = next(f for f in result["fills"] if f["side"] == "SELL")
    assert sell["date"] == sell["signal_date"] == dates[131]
    assert sell["fill_basis"] == ("raw_open_gap" if gap else "ohlc_stop_threshold")
    assert sell["price"] == pytest.approx(round(min(opening, stop)*.999, 2))
    assert "intraday_stop_ohlc_approximation" in " ".join(result["warnings"])
    changed = copy.deepcopy(data)
    changed["stocks"]["sh600096"]["bars"][131]["raw_close"] = stop+.1
    assert engine(changed, dates, end=131, config=obj.config).run()["fills"] == result["fills"]


def test_same_day_stop_intent_survives_t1_and_rebound():
    data, dates = fixture_data()
    bar = data["stocks"]["sh600096"]["bars"][130]
    bar["raw_low"] = bar["low"] = 22.
    obj = engine(data, dates, end=130)
    obj.config.stop_loss_mode = "intraday"
    day_one = obj.run()
    assert [f["side"] for f in day_one["fills"]] == ["BUY"]
    assert day_one["pending_exits"]["sh600096"]["signal_date"] == dates[130]
    obj.end_date = dates[131]
    result = obj.run()
    sell = next(f for f in result["fills"] if f["side"] == "SELL")
    assert sell["date"] == dates[131] and sell["signal_date"] == dates[130]
    assert sell["fill_basis"] == "raw_open"
    assert sell["price"] > day_one["pending_exits"]["sh600096"]["stop_price"]


def test_intraday_partial_stop_capacity_is_shared_and_rebound_does_not_cancel():
    data, dates = fixture_data()
    bars = data["stocks"]["sh600096"]["bars"]
    bars[131]["raw_low"] = bars[131]["low"] = 22.
    bars[131]["volume"] = 20
    bars[132]["volume"] = 20
    obj = engine(data, dates, end=132)
    obj.config.stop_loss_mode = "intraday"
    result = obj.run()
    sells = [f for f in result["fills"] if f["side"] == "SELL"]
    assert [(f["date"], f["shares"]) for f in sells] == [(dates[131],100), (dates[132],100)]
    assert all(f["signal_date"] == dates[131] for f in sells)
    assert "sh600096" in result["pending_exits"]
    assert result["summary"]["total_fees"] == pytest.approx(round(sum(f["fees"] for f in result["fills"]),2))


def test_pending_intraday_stop_does_not_fill_missing_or_limit_down_bar():
    data, dates = fixture_data()
    bars = data["stocks"]["sh600096"]["bars"]
    bars[130]["raw_low"] = bars[130]["low"] = 22.
    price = round(bars[130]["raw_close"]*.9,2)
    for key in ("open", "close", "high", "low"):
        bars[131][key] = bars[131]["raw_"+key] = price
    bars[131]["raw_pre_close"] = bars[130]["raw_close"]
    del bars[132]
    obj = engine(data, dates, end=132)
    obj.config.stop_loss_mode = "intraday"
    result = obj.run()
    assert [f["side"] for f in result["fills"]] == ["BUY"]
    assert "sh600096" in result["pending_exits"]
    assert any(r["reason"] == "missing_daily_bar" for r in result["rejections"])


def test_intraday_sale_proceeds_cannot_fund_an_earlier_opening_buy():
    data, dates = fixture_data(symbols=("sh600096", "sh600036"))
    config = MomentumConfig(max_positions=2, max_stock_weight=.8, max_sector_weight=1,
                            risk_per_trade=0, volatility_target=0, rebalance_every=1,
                            stop_loss_mode="intraday")
    intervals = {"sh600096": [{}], "sh600036": [{"start": dates[130]}]}
    baseline = engine(copy.deepcopy(data), dates, end=131, config=config,
                      universe_intervals=intervals).run()
    data["stocks"]["sh600096"]["bars"][131]["raw_low"] = 22.
    changed = engine(data, dates, end=131, config=config,
                     universe_intervals=intervals).run()
    opening_buys = lambda result: [f for f in result["fills"] if f["side"] == "BUY"]
    assert len(opening_buys(baseline)) == 2
    assert opening_buys(changed) == opening_buys(baseline)
    assert any(f["side"] == "SELL" and f["date"] == dates[131] for f in changed["fills"])


def test_intraday_stop_can_execute_same_day_only_when_t1_explicitly_disabled():
    data, dates = fixture_data()
    data["stocks"]["sh600096"]["bars"][130]["raw_low"] = 22.
    obj = engine(data, dates, end=130, strict_t1=False)
    obj.config.stop_loss_mode = "intraday"
    result = obj.run()
    assert [f["side"] for f in result["fills"]] == ["BUY", "SELL"]
    assert result["trades"][0]["holding_days"] == 0
    assert not result["pending_exits"]


def vacancy_config(**overrides):
    options = dict(max_positions=1, max_stock_weight=.5, risk_per_trade=0,
                   volatility_target=0, stop_loss_mode="daily_close", max_sector_weight=1,
                   rebalance_every=5, min_holding_days=0, cooldown_days=0,
                   refill_vacancies=True)
    options.update(overrides)
    return MomentumConfig(**options)


@pytest.mark.parametrize("enabled", [False, True])
def test_daily_vacancy_refill_after_completed_open_exit(enabled):
    data, dates = fixture_data(symbols=("sh600096", "sh600036"))
    bars = data["stocks"]["sh600096"]["bars"]
    set_price(bars[131], 21.5, opening=23.8)
    set_price(bars[132], 21.55, opening=21.5)
    bars[132]["raw_pre_close"] = 21.5
    config = vacancy_config(refill_vacancies=enabled)
    result = engine(data, dates, end=132, config=config,
                    universe_intervals={"sh600096": [{}], "sh600036": [{"start": dates[131]}]}).run()
    assert [(f["symbol"], f["date"]) for f in result["fills"] if f["side"] == "SELL"] == [("sh600096", dates[132])]
    replacements = [f for f in result["fills"] if f["side"] == "BUY" and f["date"] == dates[132]]
    assert len(replacements) == int(enabled)
    if enabled:
        assert replacements[0]["symbol"] == "sh600036"
        assert replacements[0]["signal_date"] == dates[131]
        assert replacements[0]["entry_reason"] == "vacancy_refill"
        assert "vacancy_refill_open_only" in " ".join(result["warnings"])


def test_partial_pending_exit_does_not_create_refill_slot():
    data, dates = fixture_data(symbols=("sh600096", "sh600036"))
    bars = data["stocks"]["sh600096"]["bars"]
    set_price(bars[131], 21.5, opening=23.8)
    for i in range(132, 136):
        set_price(bars[i], 21.55, opening=21.5, volume=20)
        bars[i]["raw_pre_close"] = bars[i-1]["raw_close"]
    result = engine(data, dates, end=134, config=vacancy_config(),
                    universe_intervals={"sh600096": [{}], "sh600036": [{"start": dates[131]}]}).run()
    assert len([f for f in result["fills"] if f["side"] == "BUY"]) == 1
    assert "sh600096" in result["pending_exits"]
    assert all(f["status"] == "PARTIALLY_FILLED" for f in result["fills"] if f["side"] == "SELL")


def test_refill_keeps_original_five_day_rank_rotation_clock(monkeypatch):
    import momentum_backtest
    original = momentum_backtest.evaluate_momentum
    def ranked(bars, as_of, config):
        signal = original(bars, as_of, config)
        if signal:
            signal["score"] = {20.: 2, 21.: 1, 22.: 3}[bars[0]["raw_close"]]
        return signal
    monkeypatch.setattr(momentum_backtest, "evaluate_momentum", ranked)
    data, dates = fixture_data(symbols=("sh600096", "sh600036", "sh600900"))
    config = vacancy_config(max_positions=2, max_stock_weight=.4, rank_buffer=0)
    result = engine(data, dates, end=135, config=config,
                    universe_intervals={"sh600096": [{}], "sh600036": [{"start": dates[130]}],
                                        "sh600900": [{"start": dates[134]}]}).run()
    second = next(f for f in result["fills"] if f["symbol"] == "sh600036" and f["side"] == "BUY")
    assert second["date"] == dates[131] and second["entry_reason"] == "vacancy_refill"
    sell = next(t for t in result["trades"] if t["symbol"] == "sh600036")
    assert sell["sell_date"] == dates[135] and "排名" in sell["reason"]


def test_daily_refill_respects_stop_cooldown_on_non_rotation_day():
    data, dates = fixture_data()
    bars = data["stocks"]["sh600096"]["bars"]
    set_price(bars[131], 22.3, opening=23.8)
    set_price(bars[132], 23.8, opening=22.3)
    bars[132]["raw_pre_close"] = 22.3
    for i in range(133, 138):
        set_price(bars[i], 23.8+(i-132)*.04, opening=23.79+(i-132)*.04)
        bars[i]["raw_pre_close"] = bars[i-1]["raw_close"]
    result = engine(data, dates, end=137, config=vacancy_config(cooldown_days=3)).run()
    assert next(f for f in result["fills"] if f["side"] == "SELL")["date"] == dates[132]
    assert [f["date"] for f in result["fills"] if f["side"] == "BUY"] == [dates[130], dates[136]]


def test_intraday_exit_refills_only_at_next_open_and_never_tops_up():
    data, dates = fixture_data(symbols=("sh600096", "sh600036"))
    data["stocks"]["sh600096"]["bars"][131]["raw_low"] = 22.
    result = engine(data, dates, end=132, config=vacancy_config(stop_loss_mode="intraday", cooldown_days=3),
                    universe_intervals={"sh600096": [{}], "sh600036": [{"start": dates[130]}]}).run()
    assert [(f["symbol"], f["date"]) for f in result["fills"] if f["side"] == "BUY"] == [
        ("sh600096", dates[130]), ("sh600036", dates[132])]
    assert next(f for f in result["fills"] if f["side"] == "SELL")["execution_phase"] == "intraday_stop"


def test_non_rotation_refill_preserves_market_filter():
    data, dates = fixture_data(symbols=("sh600096", "sh600036"))
    data["benchmark"][130]["close"] = 1.
    result = engine(data, dates, end=131,
                    config=vacancy_config(max_positions=2, max_stock_weight=.4, market_filter=True),
                    universe_intervals={"sh600096": [{}], "sh600036": [{"start": dates[130]}]}).run()
    assert [(f["symbol"], f["date"]) for f in result["fills"] if f["side"] == "BUY"] == [("sh600096", dates[130])]


def test_non_rotation_refill_preserves_entry_guard_and_industry_cap():
    data, dates = fixture_data(symbols=("sh600096", "sh600036"))
    config = vacancy_config(max_positions=2, max_stock_weight=.4, max_sector_weight=.5)
    intervals = {"sh600096": [{}], "sh600036": [{"start": dates[130]}]}
    capped = engine(copy.deepcopy(data), dates, end=131, config=config,
                    universe_intervals=intervals).run()
    assert len(capped["fills"]) == 1
    assert any(r["date"] == dates[131] and r["reason"] == "industry_cap" for r in capped["rejections"])
    bars = data["stocks"]["sh600036"]["bars"]
    set_price(bars[131], bars[131]["raw_close"], opening=round(bars[130]["raw_close"]*.96, 2))
    guarded = engine(data, dates, end=131, config=config, universe_intervals=intervals,
                     industry_map={"sh600096": "甲", "sh600036": "乙"}).run()
    assert len(guarded["fills"]) == 1
    assert any(r["date"] == dates[131] and r["reason"] == "entry_gap_down" for r in guarded["rejections"])


def test_partial_buy_remains_one_holding_without_daily_topup():
    data, dates = fixture_data()
    data["stocks"]["sh600096"]["bars"][130]["volume"] = 20
    result = engine(data, dates, end=134, config=vacancy_config()).run()
    buys = [f for f in result["fills"] if f["side"] == "BUY"]
    assert len(buys) == 1 and buys[0]["shares"] == 100
    assert buys[0]["status"] == "PARTIALLY_FILLED"
    assert result["open_positions"]["sh600096"]["shares"] == 100
