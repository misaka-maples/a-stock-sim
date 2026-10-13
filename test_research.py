"""Holdout isolation and ablation configuration checks, without network."""
from dataclasses import asdict

import pytest

from research import default_scenarios, run_research, run_walk_forward, validate_periods, FixtureFeed, main
from strategy_rules import MomentumConfig


PERIODS = dict(train=["2020-01-01", "2022-12-31"],
               validation=["2023-01-01", "2024-12-31"],
               holdout=["2025-01-01", "2026-09-30"])


def test_holdout_evaluated_once_after_selection_and_never_for_losers():
    calls = []

    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            period = self.kwargs["start_date"]
            count = self.kwargs["config"]["max_positions"]
            calls.append((period, count))
            # Holdout strongly favors a different configuration; it cannot select it.
            score = {"2020-01-01": count, "2023-01-01": -count,
                     "2025-01-01": count*100}[period]
            return dict(summary=dict(sharpe_ratio=score, max_drawdown_pct=5,
                                     total_return_pct=score), warnings=[])

    scenarios = [dict(id=str(n), config=asdict(MomentumConfig(max_positions=n))) for n in (3, 5, 6, 8)]
    report = run_research(FixtureFeed(dict(benchmark=[], stocks={})), PERIODS,
                          symbols=["sh600096"], scenarios=scenarios,
                          validation_candidates=2, engine_factory=FakeEngine)
    assert report["selected_id"] == "6"
    assert calls[-1] == ("2025-01-01", 6)
    assert len([c for c in calls if c[0] == "2025-01-01"]) == 1
    assert {c[1] for c in calls if c[0] == "2023-01-01"} == {6, 8}
    assert len(report["cost_stress_validation"]) == 3
    assert {r["id"] for r in report["cost_stress_validation"]} == {"6"}


def test_overlapping_or_reversed_periods_rejected():
    bad = dict(PERIODS, holdout=["2024-12-31", "2026-09-30"])
    with pytest.raises(ValueError, match="nonoverlapping"):
        validate_periods(bad)


def test_scenarios_isolate_equal_weight_counts_filters_and_risk_variant():
    scenarios = {s["id"]: s["config"] for s in default_scenarios()}
    assert len(scenarios) == 16
    for n in (3, 5, 6, 8):
        base = scenarios[f"positions_{n}_base"]
        assert base["max_stock_weight"] == pytest.approx(1/n)
        assert not base["use_ma120"] and not base["use_high_filter"]
        assert base["risk_per_trade"] == 0
        assert scenarios[f"positions_{n}_ma120"]["use_ma120"]
        assert not scenarios[f"positions_{n}_ma120"]["use_high_filter"]
        assert scenarios[f"positions_{n}_risk_adjusted"]["risk_adjusted"]


def test_fixture_cli_runs_chronological_report(tmp_path):
    import json
    from test_momentum_backtest import fixture_data
    data, dates = fixture_data(count=170)
    fixture, output, scenarios = tmp_path/"data.json", tmp_path/"report.json", tmp_path/"scenarios.json"
    fixture.write_text(json.dumps(data))
    scenarios.write_text(json.dumps([dict(id="single", config=asdict(MomentumConfig()))]))
    result = main(["--fixture", str(fixture), "--scenarios", str(scenarios),
                   "--train", dates[130]+":"+dates[139],
                   "--validation", dates[140]+":"+dates[149],
                   "--holdout", dates[150]+":"+dates[159],
                   "--output", str(output)])
    parsed = json.loads(output.read_text())
    assert result["selected_id"] == parsed["holdout"]["id"] == "single"
    assert parsed["data_quality"]["universe_mode"] == "fixed_pool_research_only"
    assert parsed["data_quality"]["industry_mapping_coverage"] == 0
    baseline = parsed["validation"][0]["summary"]
    costs = {r["cost_scenario"]: r["summary"] for r in parsed["cost_stress_validation"]}
    assert costs["fees_2x"]["total_fees"] > baseline["total_fees"]
    assert costs["slippage_5x"]["total_return_pct"] < baseline["total_return_pct"]


def test_walk_forward_final_holdout_runs_once_for_last_fold_choice():
    calls = []

    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            start = self.kwargs["start_date"]
            count = self.kwargs["config"]["max_positions"]
            calls.append((start, count))
            return dict(summary=dict(sharpe_ratio=count, max_drawdown_pct=2, total_return_pct=1))

    scenarios = [dict(id=str(n), config=asdict(MomentumConfig(max_positions=n))) for n in (3, 6)]
    folds = [dict(train=["2017-01-01", "2019-12-31"], validation=["2020-01-01", "2020-12-31"]),
             dict(train=["2017-01-01", "2020-12-31"], validation=["2021-01-01", "2021-12-31"])]
    report = run_walk_forward(FixtureFeed(dict(benchmark=[], stocks={})), folds,
                              ["2022-01-01", "2024-12-31"], symbols=["sh600096"],
                              scenarios=scenarios, engine_factory=FakeEngine)
    assert len(report["folds"]) == 2
    assert calls[-1] == ("2022-01-01", 6)
    assert sum(start == "2022-01-01" for start, _ in calls) == 1
    assert report["holdout"]["id"] == "6"
