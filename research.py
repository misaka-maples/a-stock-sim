"""Chronological momentum comparisons with one untouched holdout evaluation.

Examples (all dates must describe nonoverlapping, ordered periods):
 python research.py --fixture historical.json --train 2020-01-01:2022-12-31 \
   --validation 2023-01-01:2024-12-31 --holdout 2025-01-01:2026-09-30

Fixture: {"benchmark": [bars], "stocks": {symbol: {"name": ..., "bars": [...]}}}.
Directory: benchmark.json and stocks/<symbol>.json; each stock file is an info
object or an array of bars. All stocks must include adjusted and raw OHLC.
"""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path

from history_data import HistoricalDataFeed
from momentum_backtest import MomentumBacktestEngine
from strategy_rules import MomentumConfig


# Prespecified execution stresses applied to the frozen validation winner only.
COST_STRESSES = (
    ("fees_2x", dict(commission_rate=.00050, min_commission=10.,
                     stamp_tax_rate=.0010, transfer_fee_rate=.00002)),
    ("slippage_2x", dict(slippage_rate=.002)),
    ("slippage_5x", dict(slippage_rate=.005)),
)


class FixtureFeed:
    def __init__(self, data):
        self.benchmark = data["benchmark"]
        self.stocks = data["stocks"]

    def get_preset_symbols(self, pool_type=None):
        return list(self.stocks)

    def fetch_benchmark_kline(self, index_code=None, start_date="0000", end_date="9999", **kwargs):
        return [b for b in self.benchmark if start_date <= b["date"] <= end_date]

    def fetch_universe_klines(self, symbols, start_date="0000", end_date="9999", **kwargs):
        output = {}
        for symbol in symbols:
            item = self.stocks.get(symbol, {})
            if isinstance(item, list):
                item = dict(bars=item)
            bars = [b for b in item.get("bars", []) if start_date <= b["date"] <= end_date]
            output[symbol] = dict(item, name=item.get("name", symbol), bars=bars,
                                  by_date={b["date"]: b for b in bars})
        return output


def load_fixture(path=None, directory=None):
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    root = Path(directory)
    benchmark = json.loads((root / "benchmark.json").read_text(encoding="utf-8"))
    stocks = {p.stem: json.loads(p.read_text(encoding="utf-8"))
              for p in sorted((root / "stocks").glob("*.json"))}
    if not stocks:
        raise ValueError("No stocks/<symbol>.json files")
    return dict(benchmark=benchmark, stocks=stocks)


def default_scenarios():
    """Separate equal-weight count/filter ablations from risk-adjusted sizing."""
    scenarios = []
    for count in (3, 5, 6, 8):
        base = MomentumConfig(max_positions=count, max_stock_weight=1/count,
                              risk_per_trade=0, volatility_target=0,
                              use_ma120=False, use_high_filter=False)
        for label, config in (
            ("base", base),
            ("ma120", replace(base, use_ma120=True)),
            ("ma120_high", replace(base, use_ma120=True, use_high_filter=True)),
            ("risk_adjusted", replace(base, use_ma120=True, use_high_filter=True,
                                      risk_adjusted=True, risk_per_trade=.01, volatility_target=.25)),
        ):
            scenarios.append(dict(id=f"positions_{count}_{label}", config=asdict(config)))
    return scenarios


def validate_periods(periods):
    from datetime import date
    previous_end = None
    for label in ("train", "validation", "holdout"):
        if label not in periods or len(periods[label]) != 2:
            raise ValueError("train/validation/holdout require start and end dates")
        start, end = periods[label]
        date.fromisoformat(start)
        date.fromisoformat(end)
        if start > end or (previous_end is not None and start <= previous_end):
            raise ValueError("Periods must be chronological and strictly nonoverlapping")
        previous_end = end


def _score(record):
    summary = record["summary"]
    # This prespecified research rule is not a promise of economic optimality.
    return (summary["sharpe_ratio"], -summary["max_drawdown_pct"], summary["total_return_pct"], record["id"])


def run_research(feed, periods, symbols=None, scenarios=None, industry_map=None,
                 universe_intervals=None, initial_cash=100000., validation_candidates=3,
                 engine_factory=MomentumBacktestEngine, _evaluate_holdout=True):
    validate_periods(periods)
    scenarios = list(default_scenarios() if scenarios is None else scenarios)
    if not scenarios or len({s["id"] for s in scenarios}) != len(scenarios):
        raise ValueError("Scenarios require unique IDs")
    if validation_candidates < 1:
        raise ValueError("validation_candidates must be at least 1")
    symbols = list(symbols or feed.get_preset_symbols("core_active"))
    industry_map = industry_map or {}
    warnings = ["research_only: comparisons do not establish an optimal strategy",
                "holdout_is_single_use: do not retune after reading its result",
                "corporate_actions_not_accounted: require dividend/split/delisting event data",
                "execution_approximation: next open with daily capacity differs from live arrival-time book"]
    if universe_intervals is None:
        warnings.append("point_in_time_universe_missing: fixed pool can contain survivorship/selection bias")
    missing_industry = sum(not industry_map.get(s) for s in symbols)
    if missing_industry:
        warnings.append("industry_mapping_missing: unknown stocks share one 35% bucket; exposure comparisons may be constrained")
    configs = {s["id"]: dict(s["config"]) for s in scenarios}

    def evaluate(scenario, split, cost_overrides=None):
        start, end = periods[split]
        result = engine_factory(start_date=start, end_date=end, initial_cash=initial_cash,
                                strategy_name="Momentum_Rotation_Pro", symbols=symbols,
                                pool_type="provided_research_pool", config=configs[scenario["id"]],
                                feed=feed, industry_map=industry_map,
                                universe_intervals=universe_intervals,
                                **(cost_overrides or {})).run()
        return dict(id=scenario["id"], split=split, config=configs[scenario["id"]],
                    summary=result["summary"], warnings=result.get("warnings", []),
                    data_coverage=result.get("data_coverage", {}),
                    execution_model=result.get("execution_model"))

    train = [evaluate(s, "train") for s in scenarios]
    shortlisted = sorted(train, key=_score, reverse=True)[:validation_candidates]
    selected_ids = {r["id"] for r in shortlisted}
    validation = [evaluate(s, "validation") for s in scenarios if s["id"] in selected_ids]
    selected = max(validation, key=_score)
    # No loop over holdout: evaluate exactly the validation-selected configuration.
    chosen_scenario = next(s for s in scenarios if s["id"] == selected["id"])
    stresses = []
    if _evaluate_holdout:
        for label, overrides in COST_STRESSES:
            stressed = evaluate(chosen_scenario, "validation", overrides)
            stressed.update(cost_scenario=label, cost_overrides=overrides)
            stresses.append(stressed)
    holdout = evaluate(chosen_scenario, "holdout") if _evaluate_holdout else None
    return dict(schema_version=1, periods=periods,
                selection_rule="train top K by Sharpe/drawdown/return; validation selects one; holdout evaluated once",
                validation_candidates=len(validation), selected_id=selected["id"],
                train=train, validation=validation, holdout=holdout,
                cost_stress_validation=stresses,
                data_quality=dict(symbol_count=len(symbols),
                                  universe_mode="historical_eligibility_research" if universe_intervals is not None else "fixed_pool_research_only",
                                  industry_mapping_coverage=1-missing_industry/max(1, len(symbols)),
                                  corporate_action_accounting=False), warnings=warnings)


def run_walk_forward(feed, folds, holdout, **kwargs):
    """Repeated chronological train/validation, then one final holdout.

    Validation windows cannot overlap. Training windows may expand to include
    previous validation periods, as information would become available in time.
    The final fold alone selects the configuration for the final holdout.
    """
    if not folds:
        raise ValueError("At least one walk-forward fold is required")
    last_validation_end = None
    reports = []
    for index, fold in enumerate(folds):
        periods = dict(train=fold["train"], validation=fold["validation"], holdout=holdout)
        validate_periods(periods)
        if last_validation_end is not None and fold["validation"][0] <= last_validation_end:
            raise ValueError("Walk-forward validation windows must be chronological and nonoverlapping")
        last_validation_end = fold["validation"][1]
        reports.append(run_research(feed, periods, **kwargs,
                                    _evaluate_holdout=index == len(folds)-1))
    final_holdout = reports[-1].pop("holdout")
    final_stress = reports[-1].pop("cost_stress_validation")
    return dict(schema_version=1, mode="walk_forward", folds=reports,
                selected_id=reports[-1]["selected_id"], holdout=final_holdout,
                cost_stress_validation=final_stress,
                selection_rule="each fold uses its own train/validation; final fold selects one configuration for one final holdout",
                data_quality=reports[-1]["data_quality"], warnings=reports[-1]["warnings"])


def _period(value):
    parts = value.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD:YYYY-MM-DD")
    return parts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture")
    source.add_argument("--data-dir")
    source.add_argument("--network", action="store_true", help="Explicitly request public historical feed")
    parser.add_argument("--symbols", help="JSON array of symbols; optional with fixture")
    parser.add_argument("--industry-map", help="JSON symbol to industry map")
    parser.add_argument("--universe", help="JSON symbol to [{start,end}] eligibility intervals")
    parser.add_argument("--scenarios", help="Optional JSON [{id,config}] array")
    parser.add_argument("--train", type=_period)
    parser.add_argument("--validation", type=_period)
    parser.add_argument("--walk-forward-folds", help="JSON [{train:[start,end],validation:[start,end]}] folds")
    parser.add_argument("--holdout", required=True, type=_period)
    parser.add_argument("--cash", type=float, default=100000)
    parser.add_argument("--validation-candidates", type=int, default=3)
    parser.add_argument("--output", help="JSON report path; otherwise stdout")
    args = parser.parse_args(argv)

    def read_json(path):
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else None

    feed = HistoricalDataFeed() if args.network else FixtureFeed(load_fixture(args.fixture, args.data_dir))
    industries = read_json(args.industry_map)
    if isinstance(industries, dict) and isinstance(industries.get("industries"), dict):
        industries = industries["industries"]
    options = dict(symbols=read_json(args.symbols), scenarios=read_json(args.scenarios),
                   industry_map=industries, universe_intervals=read_json(args.universe),
                   initial_cash=args.cash, validation_candidates=args.validation_candidates)
    if args.walk_forward_folds:
        if args.train or args.validation:
            parser.error("--walk-forward-folds supplies train/validation; do not also specify their flags")
        report = run_walk_forward(feed, read_json(args.walk_forward_folds), args.holdout, **options)
    else:
        if not args.train or not args.validation:
            parser.error("Provide --train and --validation, or --walk-forward-folds")
        report = run_research(feed, dict(train=args.train, validation=args.validation, holdout=args.holdout), **options)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print("研究报告已保存: " + args.output + "; holdout仅评估 " + report["selected_id"])
    else:
        print(rendered)
    return report


if __name__ == "__main__":
    main()
