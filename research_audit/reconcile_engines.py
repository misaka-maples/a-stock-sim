# -*- coding: utf-8 -*-
import json
import math
import os
import sys
import copy
import statistics
from collections import defaultdict
import datetime as dt

sys.path.insert(0, '/home/openarmx/test')

from product_ab_engine import ProductABEngine, ProductABConfig
from research_market_regime.comprehensive_regime_study import ComprehensiveRegimeStudyEngine

# Monkey patch _calc_version_metrics to return equity_curve and trades
_orig_calc = ComprehensiveRegimeStudyEngine._calc_version_metrics
def _patched_calc(self, label, equity_curve, closed_trades, exposure_records):
    res = _orig_calc(self, label, equity_curve, closed_trades, exposure_records)
    res['equity_curve'] = equity_curve
    res['closed_trades'] = closed_trades
    return res
ComprehensiveRegimeStudyEngine._calc_version_metrics = _patched_calc

def run_reconciliation():
    print('=== Phase 3: Day-by-Day NAV & Order Replay Reconciliation ===')
    
    print('[*] Running Research A (ProductABEngine Synthetic Blending)...')
    engine_a = ProductABEngine(start_date='2024-01-02', end_date='2026-11-06')
    res_a = engine_a.run()
    curve_synth = {p['date']: p for p in res_a['equity_curve']}
    
    print('[*] Running Research B (ComprehensiveRegimeStudyEngine V0)...')
    engine_b = ComprehensiveRegimeStudyEngine(start_date='2024-01-02', end_date='2026-11-06')
    res_v0 = engine_b.simulate_version('V0', mode='AB')
    curve_v0 = {p['date']: p for p in res_v0['equity_curve']}
    
    common_dates = sorted(set(curve_synth.keys()) & set(curve_v0.keys()))
    print(f'[*] Common dates count: {len(common_dates)} (from {common_dates[0]} to {common_dates[-1]})')
    
    first_divergence = None
    periods = {
        '2024_Q1_Crash': ('2024-01-02', '2024-02-08'),
        '2024_Apr_New9': ('2024-04-01', '2024-04-30'),
        '2024_Sep_924Rally': ('2024-09-24', '2024-10-08'),
        '2024_Oct_Post924': ('2024-10-09', '2024-11-06')
    }
    
    period_stats = {}
    for p_name, (s_dt, e_dt) in periods.items():
        p_dates = [d for d in common_dates if s_dt <= d <= e_dt]
        if p_dates:
            eq_synth_start = curve_synth[p_dates[0]]['equity']
            eq_synth_end = curve_synth[p_dates[-1]]['equity']
            ret_synth = (eq_synth_end / eq_synth_start - 1.0) * 100.0
            
            eq_v0_start = curve_v0[p_dates[0]]['equity']
            eq_v0_end = curve_v0[p_dates[-1]]['equity']
            ret_v0 = (eq_v0_end / eq_v0_start - 1.0) * 100.0
            
            period_stats[p_name] = {
                'start_date': s_dt, 'end_date': e_dt,
                'synth_return_pct': round(ret_synth, 2),
                'research_b_v0_return_pct': round(ret_v0, 2),
                'diff_pct': round(ret_synth - ret_v0, 2)
            }
            
    for d in common_dates:
        eq_s = curve_synth[d]['equity']
        eq_v = curve_v0[d]['equity']
        diff_pct = (eq_s - eq_v) / 100000.0 * 100.0
        if abs(diff_pct) >= 1.0 and first_divergence is None:
            first_divergence = {
                'date': d,
                'synth_equity': eq_s,
                'v0_equity': eq_v,
                'diff_pct': round(diff_pct, 2),
                'synth_regime': curve_synth[d].get('regime'),
                'v0_regime': curve_v0[d].get('regime'),
                'synth_ret_today': curve_synth[d].get('daily_return'),
                'v0_exposure_today': curve_v0[d].get('exposure_pct')
            }
            
    print(f'[*] First Significant Divergence: {first_divergence}')
    print('[*] Stress Periods:')
    for k, v in period_stats.items():
        print(f"  {k}: Synth={v['synth_return_pct']}%, V0={v['research_b_v0_return_pct']}%, Diff={v['diff_pct']}%")

    out_data = {
        'first_divergence': first_divergence,
        'stress_period_performance': period_stats,
        'summary_comparison': {
            'Research_A_Synthetic_Blending': {
                'total_return_pct': res_a['summary']['total_return_pct'],
                'cagr_pct': res_a['summary']['cagr_pct'],
                'mdd_pct': res_a['summary']['max_drawdown_pct'],
                'sharpe': res_a['summary']['sharpe_ratio'],
                'calmar': res_a['summary']['calmar_ratio']
            },
            'Research_B_Single_Account_V0': {
                'total_return_pct': res_v0['total_return_pct'],
                'cagr_pct': res_v0['cagr_pct'],
                'mdd_pct': res_v0['max_drawdown_pct'],
                'sharpe': res_v0['sharpe_ratio'],
                'calmar': res_v0['calmar_ratio']
            }
        }
    }
    
    out_file = '/home/openarmx/test/research_audit/results/reconciliation_results.json'
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)
    print(f'Phase 3 reconciliation saved to {out_file}')

if __name__ == '__main__':
    run_reconciliation()
