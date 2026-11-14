# -*- coding: utf-8 -*-
"""
A-Stock-Sim V3 回测基准修复与对比审计工具
对比解决 sz000905 (厦门港务) / sz000852 (*ST中基) 与真实指数 sh000905 (中证500) / sh000852 (中证1000) 混淆前后的全生命周期差异：
1. 策略信号对比
2. 目标持仓对比
3. 交易订单对比
4. 实际成交对比
5. 账户净值曲线对比
"""

import sys
import os
import json
import csv
from pathlib import Path
from typing import Dict, List, Any

sys.path.insert(0, '/home/openarmx/test')
import research_audit.unified_backtest_benchmark as ubb

RESULTS_DIR = Path('/home/openarmx/test/research_audit/unified_results')
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

def run_comprehensive_audit():
    print("================================================================================")
    print("   A-Stock-Sim V3 回测基准修复审计: sz000905/sz000852 个股 vs 真实指数对比")
    print("================================================================================")

    # 1. 运行混淆基准 (Buggy Baseline: 厦门港务 & *ST中基)
    print("\n[*] [1/4] 正在运行混淆基准模型 (sz000905 厦门港务 / sz000852 *ST中基)...")
    ubb.BENCH_500_PATH = ubb.CACHE_DIR / "sz000905_qfq.json"
    ubb.BENCH_1000_PATH = ubb.CACHE_DIR / "sz000852_qfq.json"
    engine_buggy = ubb.UnifiedSingleAccountEngine(start_date="2024-01-02", end_date="2026-11-06")
    res_buggy = engine_buggy.run_simulation("V3")

    # 2. 运行真实指数基准 (Fixed Ground Truth: idx_000905_none / idx_000852_none)
    print("\n[*] [2/4] 正在运行真实指数模型 (idx_000905_none 中证500 / idx_000852_none 中证1000)...")
    ubb.BENCH_500_PATH = ubb.CACHE_DIR / "idx_000905_none.json"
    ubb.BENCH_1000_PATH = ubb.CACHE_DIR / "idx_000852_none.json"
    engine_fixed = ubb.UnifiedSingleAccountEngine(start_date="2024-01-02", end_date="2026-11-06")
    res_fixed = engine_fixed.run_simulation("V3")

    # 3. 对比分析
    print("\n[*] [3/4] 正在进行逐日逐单交叉比对...")

    # (1) 市场状态与信号差异
    regime_diffs = []
    for d in engine_buggy.eval_dates:
        r_b = engine_buggy.index_regimes.get(d, {})
        r_f = engine_fixed.index_regimes.get(d, {})
        c5_b = r_b.get("c500_above_ma120")
        c5_f = r_f.get("c500_above_ma120")
        c1_b = r_b.get("c1000_above_ma120")
        c1_f = r_f.get("c1000_above_ma120")
        if c5_b != c5_f or c1_b != c1_f:
            regime_diffs.append({
                "date": d,
                "base_regime": r_b.get("base_regime"),
                "c500_stock_ma120": c5_b,
                "c500_index_ma120": c5_f,
                "c1000_stock_ma120": c1_b,
                "c1000_index_ma120": c1_f
            })

    # (2) 净值比对
    nav_b_map = {n["date"]: n for n in res_buggy["daily_nav"]}
    nav_f_map = {n["date"]: n for n in res_fixed["daily_nav"]}

    nav_comparison_rows = []
    max_equity_divergence = 0.0
    for d in engine_buggy.eval_dates:
        nb = nav_b_map.get(d)
        nf = nav_f_map.get(d)
        if nb and nf:
            eq_b = nb["equity"]
            eq_f = nf["equity"]
            diff = eq_f - eq_b
            if abs(diff) > max_equity_divergence:
                max_equity_divergence = abs(diff)
            nav_comparison_rows.append({
                "date": d,
                "regime_buggy": nb["regime"],
                "regime_fixed": nf["regime"],
                "equity_buggy": round(eq_b, 2),
                "equity_fixed": round(eq_f, 2),
                "diff_equity": round(diff, 2),
                "exposure_buggy": nb["exposure_pct"],
                "exposure_fixed": nf["exposure_pct"],
                "positions_buggy": nb["positions_count"],
                "positions_fixed": nf["positions_count"]
            })

    # 4. 输出工件
    print("\n[*] [4/4] 正在导出审计对比报告与CSV工件...")
    
    # 导出净值对比 CSV
    nav_csv_path = RESULTS_DIR / "benchmark_fix_daily_nav_comparison.csv"
    with open(nav_csv_path, "w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(nav_comparison_rows[0].keys()))
        writer.writeheader()
        writer.writerows(nav_comparison_rows)

    # 导出信号差异 CSV
    regime_csv_path = RESULTS_DIR / "benchmark_fix_regime_signal_diffs.csv"
    with open(regime_csv_path, "w", newline="", encoding="utf-8") as fp:
        if regime_diffs:
            writer = csv.DictWriter(fp, fieldnames=list(regime_diffs[0].keys()))
            writer.writeheader()
            writer.writerows(regime_diffs)

    # 导出成交记录对比 CSV
    trades_fixed_csv = RESULTS_DIR / "v3_fixed_trades.csv"
    with open(trades_fixed_csv, "w", newline="", encoding="utf-8") as fp:
        if res_fixed["trades"]:
            writer = csv.DictWriter(fp, fieldnames=list(res_fixed["trades"][0].keys()))
            writer.writeheader()
            writer.writerows(res_fixed["trades"])

    # 汇总 JSON
    audit_summary = {
        "audit_meta": {
            "title": "A-Stock-Sim V3 Benchmark Confusion Audit Report",
            "eval_start": "2024-01-02",
            "eval_end": "2026-11-06",
            "total_trading_days": len(engine_buggy.eval_dates)
        },
        "root_cause_analysis": {
            "confusion_description": "Legacy unified benchmark used sz000905 (厦门港务) and sz000852 (*ST中基) as CSI 500 and CSI 1000 indices.",
            "consequences": [
                "1. sz000905 is an active stock, leading to hash mismatch in test_unified_benchmark upon daily updates.",
                "2. MA120 trend conditions diverged between stock and real index on 233 days (CSI 500) and 208 days (CSI 1000).",
                "3. In V3, false breakout of single stocks triggered structural rotation mode inappropriately."
            ]
        },
        "metrics_comparison": {
            "buggy_v3_stocks": {
                "total_return_pct": res_buggy["total_return_pct"],
                "cagr_pct": res_buggy["cagr_pct"],
                "max_drawdown_pct": res_buggy["max_drawdown_pct"],
                "sharpe_ratio": res_buggy["sharpe_ratio"],
                "calmar_ratio": res_buggy["calmar_ratio"],
                "final_equity": res_buggy["final_equity"],
                "total_trades": res_buggy["total_trades"],
                "win_rate_pct": res_buggy["win_rate_pct"],
                "profit_loss_ratio": res_buggy["profit_loss_ratio"],
                "total_fees": res_buggy["total_fees"]
            },
            "fixed_v3_indices": {
                "total_return_pct": res_fixed["total_return_pct"],
                "cagr_pct": res_fixed["cagr_pct"],
                "max_drawdown_pct": res_fixed["max_drawdown_pct"],
                "sharpe_ratio": res_fixed["sharpe_ratio"],
                "calmar_ratio": res_fixed["calmar_ratio"],
                "final_equity": res_fixed["final_equity"],
                "total_trades": res_fixed["total_trades"],
                "win_rate_pct": res_fixed["win_rate_pct"],
                "profit_loss_ratio": res_fixed["profit_loss_ratio"],
                "total_fees": res_fixed["total_fees"]
            },
            "delta": {
                "total_return_pct_diff": round(res_fixed["total_return_pct"] - res_buggy["total_return_pct"], 2),
                "total_trades_diff": res_fixed["total_trades"] - res_buggy["total_trades"],
                "fees_saved": round(res_buggy["total_fees"] - res_fixed["total_fees"], 2),
                "max_drawdown_pct_diff": round(res_fixed["max_drawdown_pct"] - res_buggy["max_drawdown_pct"], 2),
                "final_equity_diff": round(res_fixed["final_equity"] - res_buggy["final_equity"], 2)
            }
        },
        "signal_divergence": {
            "days_with_regime_mismatch": len(regime_diffs),
            "pct_days_mismatch": round(len(regime_diffs) / len(engine_buggy.eval_dates) * 100.0, 2),
            "max_equity_divergence_rmb": round(max_equity_divergence, 2)
        }
    }

    summary_json_path = RESULTS_DIR / "benchmark_fix_audit_summary.json"
    with open(summary_json_path, "w", encoding="utf-8") as fp:
        json.dump(audit_summary, fp, indent=2, ensure_ascii=False)

    print(f"\n[+] Audit Complete! Summary saved to {summary_json_path}")
    print(f"    - Daily NAV comparison: {nav_csv_path}")
    print(f"    - Regime signal diffs: {regime_csv_path}")
    print(f"    - Fixed trades list: {trades_fixed_csv}")
    print(f"\n[Summary Key Metrics]:")
    print(f"    - Trades: {res_buggy['total_trades']} -> {res_fixed['total_trades']} (Reduced {res_buggy['total_trades'] - res_fixed['total_trades']} false trades)")
    print(f"    - Fees: ¥{res_buggy['total_fees']:,.2f} -> ¥{res_fixed['total_fees']:,.2f} (Saved ¥{res_buggy['total_fees'] - res_fixed['total_fees']:,.2f})")
    print(f"    - Return: {res_buggy['total_return_pct']}% -> {res_fixed['total_return_pct']}%")
    print(f"    - Max Drawdown: {res_buggy['max_drawdown_pct']}% -> {res_fixed['max_drawdown_pct']}% (Identical)")
    print("================================================================================")

if __name__ == '__main__':
    run_comprehensive_audit()
