# -*- coding: utf-8 -*-
"""
自动化回归测试：验证统一单账户 Product AB v1.0 基准引擎的可复现性与数据完整性
"""

import sys
import os
import json
import hashlib
from pathlib import Path

sys.path.insert(0, '/home/openarmx/test')

from research_audit.unified_backtest_benchmark import UnifiedSingleAccountEngine

def test_unified_benchmark_reproducibility():
    print("=== Running Automated Regression Test: Unified Benchmark Reproducibility ===")
    
    # 1. 验证基准数据 SHA256 哈希完整性
    print("[*] Test 1: Verifying data file checksums...")
    bench_dir = Path("/home/openarmx/test/research_audit/unified_results")
    with open(bench_dir / "data_checksums.json", "r", encoding="utf-8") as fp:
        expected_checksums = json.load(fp)
        
    for fname, exp_hash in expected_checksums.items():
        if fname.endswith(".json"):
            fpath = Path("/home/openarmx/test/data/history_cache") / fname
            assert fpath.exists(), f"Missing data file: {fpath}"
            with open(fpath, "rb") as fp:
                cur_hash = hashlib.sha256(fp.read()).hexdigest()
            assert cur_hash == exp_hash, f"Hash mismatch for {fname}: expected {exp_hash}, got {cur_hash}"
            print(f"  [PASS] {fname}: SHA256 verified ({cur_hash[:12]}...)")
            
    # 2. 验证 V0 基准回测复现一致性
    print("\n[*] Test 2: Re-running V0 simulation to verify deterministic execution...")
    engine = UnifiedSingleAccountEngine(start_date="2024-01-02", end_date="2026-11-06")
    res_v0 = engine.run_simulation("V0")
    
    with open(bench_dir / "v0_to_v3_comparison.json", "r", encoding="utf-8") as fp:
        baseline = json.load(fp)["V0"]
        
    assert res_v0["total_return_pct"] == baseline["total_return_pct"], f"Return mismatch: {res_v0['total_return_pct']} vs {baseline['total_return_pct']}"
    assert res_v0["max_drawdown_pct"] == baseline["max_drawdown_pct"], f"MDD mismatch: {res_v0['max_drawdown_pct']} vs {baseline['max_drawdown_pct']}"
    assert res_v0["total_trades"] == baseline["total_trades"], f"Trade count mismatch: {res_v0['total_trades']} vs {baseline['total_trades']}"
    assert res_v0["final_equity"] == baseline["final_equity"], f"Final equity mismatch: {res_v0['final_equity']} vs {baseline['final_equity']}"
    print(f"  [PASS] V0 Baseline Determinism: Return={res_v0['total_return_pct']}%, MDD={res_v0['max_drawdown_pct']}%, Trades={res_v0['total_trades']}")
    
    # 3. 验证 CSV 交付工件完整性
    print("\n[*] Test 3: Verifying delivered CSV artifacts...")
    nav_csv = bench_dir / "v0_daily_nav.csv"
    orders_csv = bench_dir / "v0_orders.csv"
    trades_csv = bench_dir / "v0_trades.csv"
    
    assert nav_csv.exists() and nav_csv.stat().st_size > 10000, "Daily NAV CSV invalid"
    assert orders_csv.exists() and orders_csv.stat().st_size > 10000, "Orders CSV invalid"
    assert trades_csv.exists() and trades_csv.stat().st_size > 5000, "Trades CSV invalid"
    
    with open(nav_csv, "r", encoding="utf-8") as fp:
        lines = fp.readlines()
        assert len(lines) == 688 + 1, f"NAV lines count mismatch: expected 689, got {len(lines)}"
        last_equity = float(lines[-1].split(",")[1])
        assert abs(last_equity - res_v0["final_equity"]) < 0.01, "Final equity in CSV does not match engine summary"
    print(f"  [PASS] v0_daily_nav.csv: 688 trading days verified, final equity ¥{last_equity:,.2f}")
    
    with open(trades_csv, "r", encoding="utf-8") as fp:
        t_lines = fp.readlines()
        assert len(t_lines) == res_v0["total_trades"] + 1, f"Trades lines count mismatch"
    print(f"  [PASS] v0_trades.csv: {res_v0['total_trades']} closed trades verified")
    
    print("\n>>> ALL REGRESSION TESTS PASSED SUCCESSFULLY! <<<")

if __name__ == "__main__":
    test_unified_benchmark_reproducibility()
