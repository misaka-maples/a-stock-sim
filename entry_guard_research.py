"""Offline frozen comparisons. Run after the entry engine is ready.

No live services, network, account files, configuration writes or notifications.
The 2026 period was already observed and is not an independent holdout.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys

PROJECT = Path(__file__).resolve().parent
DEFAULT_BASELINE = '37a5746db8940028d3053455d14175d0e3f12f22'
PERIODS = {
    'training_2024': ('2024-01-01', '2024-12-31'),
    'validation_2025': ('2025-01-01', '2025-12-31'),
    'observed_2026_diagnostic_only': ('2026-01-01', '2026-10-13'),
    'continuous_2024_2026_diagnostic_only': ('2024-01-01', '2026-10-13'),
}
CASES = {
    'baseline_prior_commit': {},
    'hard_limit_guard_only': dict(max_entry_gap_down_pct=0, max_entry_gap_up_pct=0,
                                  max_entry_intraday_drop_pct=0, entry_require_ma20=False),
    'guard_defaults': {},
    'guard_risk_adjusted_experiment': dict(risk_adjusted=True),
    'guard_six_positions_experiment': dict(max_positions=6, max_stock_weight=.20),
}


def worker(args):
    case, period = args.worker
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(PROJECT))
    if case == 'baseline_prior_commit':
        sys.path.insert(0, str(work / 'baseline'))
    from research import FixtureFeed
    from history_data import PRESET_POOLS
    from strategy_rules import MomentumConfig
    from momentum_backtest import MomentumBacktestEngine
    fixture = json.loads(args.fixture.read_text())
    context = json.loads(args.context.read_text())
    params = dict(context['strategy']['parameters'], **CASES[case])
    config = MomentumConfig.from_dict(params)
    start, end = PERIODS[period]
    result = MomentumBacktestEngine(start_date=start, end_date=end,
        symbols=PRESET_POOLS['core_active'], pool_type='core_active', config=config,
        feed=FixtureFeed(fixture), industry_map=context['industry_map']).run()
    exit_counts = collections.Counter(t['reason'] for t in result['trades'])
    rejection_counts = collections.Counter(t['reason'] for t in result['rejections'])
    result['diagnostic'] = dict(exit_counts=dict(exit_counts), rejection_counts=dict(rejection_counts))
    target = work / (case + '__' + period + '.json')
    if target.resolve() in (args.fixture, args.context):
        raise ValueError('Worker detail must not overwrite frozen inputs')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(dict(case=case, period=period, summary=result['summary'],
                         rejection_counts=rejection_counts), ensure_ascii=False), flush=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--fixture', required=True, type=Path,
                     help='Frozen local JSON market fixture; no provider/network access')
    cli.add_argument('--context', required=True, type=Path,
                     help='Frozen local JSON with strategy.parameters and industry_map')
    cli.add_argument('--output', required=True, type=Path, help='Diagnostic report JSON')
    cli.add_argument('--work-dir', type=Path, help='Local detail results; default temporary directory')
    cli.add_argument('--baseline-commit', default=DEFAULT_BASELINE,
                     help='Existing local Git commit; never fetched from a remote')
    cli.add_argument('--worker', nargs=2, metavar=('CASE', 'PERIOD'),
                     help=argparse.SUPPRESS)
    return cli


def main(args):
    fixture_hash, context_hash = digest(args.fixture), digest(args.context)
    generated = [args.work_dir / 'baseline' / filename for filename in
                 ('strategy_rules.py', 'momentum_backtest.py')]
    generated += [args.work_dir / (case + '__' + period + '.json')
                  for case in CASES for period in PERIODS]
    if any(path.resolve() in (args.fixture, args.context) for path in generated):
        raise ValueError('Work directory files must not overwrite frozen inputs')
    args.work_dir.mkdir(parents=True, exist_ok=True)
    (args.work_dir / 'baseline').mkdir(exist_ok=True)
    # Resolve a local commit first; do not let arbitrary input become Git options.
    baseline = subprocess.check_output(
        ['git', 'rev-parse', '--verify', args.baseline_commit + '^{commit}'],
        cwd=PROJECT, text=True).strip()
    for filename in ('strategy_rules.py', 'momentum_backtest.py'):
        source = subprocess.check_output(['git', 'show', baseline + ':' + filename], cwd=PROJECT)
        (args.work_dir / 'baseline' / filename).write_bytes(source)
    source_hashes = {name: digest(PROJECT / name) for name in
        ('strategy_rules.py', 'daily_momentum.py', 'momentum_backtest.py', 'trade_calendar.py')}
    results = {}
    for case in CASES:
        results[case] = {}
        for period in PERIODS:
            subprocess.run([sys.executable, str(Path(__file__).resolve()),
                '--fixture', str(args.fixture), '--context', str(args.context),
                '--output', str(args.output), '--work-dir', str(args.work_dir),
                '--baseline-commit', baseline, '--worker', case, period], check=True,
                cwd=args.work_dir, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            if digest(args.fixture) != fixture_hash or digest(args.context) != context_hash:
                raise RuntimeError('Frozen input changed during comparisons; no report published')
            if any(digest(PROJECT / name) != value for name, value in source_hashes.items()):
                raise RuntimeError('Source changed during comparisons; rerun after changes finish')
            result = json.loads((args.work_dir / (case + '__' + period + '.json')).read_text())
            results[case][period] = {k: result[k] for k in
                ('summary', 'parameters', 'diagnostic', 'warnings', 'open_positions')}
    report = dict(purpose='Frozen paired diagnostic; no winner selection or live economic parameter change',
        baseline_commit=baseline, updated_source_sha256=source_hashes,
        comparison_script_sha256=digest(Path(__file__).resolve()),
        fixture_sha256=fixture_hash, context_sha256=context_hash,
        periods=PERIODS, cases=CASES, results=results,
        interpretation=['2024 is a descriptive training comparison; 2025 is validation',
            '2026 was already observed and is not independent holdout evidence',
            'Each individual year starts with fresh cash and uses only preceding bars for warmup',
            'Continuous period retains positions across years; independent-year returns do not chain',
            'Hard lower-limit guard cannot be disabled in updated implementation',
            'Baseline uses prior engine/rules and the current calendar/history/fixture implementation',
            'Alternative baseline commits must accept frozen context parameters and current fixture format',
            'Risk-adjusted ranking and six positions are experiments only, not selected live parameters'],
        model_limits=['Fixed current core20 pool has only 16 main-board stocks; historical membership and survivorship are unverified',
            'Current industry snapshot is not point-in-time classification',
            'Daily opening-price and whole-day volume approximation does not reproduce live order-book timing',
            'Intraday drop and visible bid-depth guards cannot be measured with opening-only fill data',
            'Cash dividends, splits and delisting recovery events are not accounted',
            'No optimization or future profitability claim'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))


def run():
    args = parser().parse_args()
    args.fixture, args.context, args.output = (path.resolve() for path in
        (args.fixture, args.context, args.output))
    if args.output in (args.fixture, args.context):
        raise ValueError('Output must not overwrite frozen inputs')
    if args.work_dir:
        args.work_dir = args.work_dir.resolve()
    if args.worker:
        if args.work_dir is None or args.worker[0] not in CASES or args.worker[1] not in PERIODS:
            raise ValueError('Worker requires a work directory and a known case/period')
        worker(args)
    elif args.work_dir:
        main(args)
    else:
        with tempfile.TemporaryDirectory(prefix='quant-entry-comparison-') as temporary:
            args.work_dir = Path(temporary)
            main(args)


if __name__ == '__main__':
    run()
