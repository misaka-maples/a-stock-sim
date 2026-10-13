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
                                                risk_per_trade=0, volatility_target=0,
                                                max_sector_weight=1, rebalance_every=1,
                                                min_holding_days=0, cooldown_days=0))
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
