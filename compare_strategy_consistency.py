# compare_strategy_consistency.py
import os
import json
import sqlite3
import tempfile
from decimal import Decimal

from product_ab_v2_strategies import ProductABBalancedV2Strategy, ProductBAggressiveV2Strategy
from sim_trader import SimAccount
from astock.trade.runtime import TradingRuntime
from astock.trade.models import MarketRegime, OrderSide
from tests.test_v3_final_validation import make_benchmark_bars

def compare_strategies():
    print("==================================================")
    print(" A-STOCK-SIM: STRATEGY CONSISTENCY AUDIT (LEGACY VS V3) ")
    print("==================================================")

    # 1. Setup shared market snapshot
    as_of = "2026-11-16"
    bench_bars = make_benchmark_bars(as_of, "STRONG_RISK_ON", count=150)
    
    quotes = {
        "sh600036": {"price": 30.0, "current": 30.0, "atr": 0.5, "ma20": 29.0, "sector": "金融", "name": "招商银行"},
        "sh601138": {"price": 20.0, "current": 20.0, "atr": 0.5, "ma20": 19.5, "sector": "电子", "name": "工业富联"},
        "sh600900": {"price": 25.0, "current": 25.0, "atr": 0.4, "ma20": 24.5, "sector": "公用", "name": "长江电力"},
        "sz002475": {"price": 30.0, "current": 30.0, "atr": 0.8, "ma20": 29.0, "sector": "电子", "name": "立讯精密"},
        "sz002594": {"price": 250.0, "current": 250.0, "atr": 5.0, "ma20": 240.0, "sector": "汽车", "name": "比亚迪"},
        "sh600519": {"price": 1800.0, "current": 1800.0, "atr": 20.0, "ma20": 1750.0, "sector": "食品", "name": "贵州茅台"},
    }
    
    candidates = [
        {"symbol": "sh600036", "name": "招商银行", "score": 92.0, "sector": "金融", "price": 30.0, "ret20": 0.05, "ret60": 0.10},
        {"symbol": "sh601138", "name": "工业富联", "score": 90.0, "sector": "电子", "price": 20.0, "ret20": 0.08, "ret60": 0.15},
        {"symbol": "sh600900", "name": "长江电力", "score": 88.0, "sector": "公用", "price": 25.0, "ret20": 0.04, "ret60": 0.08},
        {"symbol": "sz002475", "name": "立讯精密", "score": 86.0, "sector": "电子", "price": 30.0, "ret20": 0.07, "ret60": 0.12},
        {"symbol": "sz002594", "name": "比亚迪", "score": 84.0, "sector": "汽车", "price": 250.0, "ret20": 0.06, "ret60": 0.11},
        {"symbol": "sh600519", "name": "贵州茅台", "score": 80.0, "sector": "食品", "price": 1800.0, "ret20": 0.02, "ret60": 0.05},
    ]
    
    top_sectors = ["金融", "电子", "公用", "汽车"]

    # 2. Run Legacy Strategy
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_legacy_path = os.path.join(tmp_dir, "account.json")
        acc_legacy = SimAccount(initial_cash=100000.0, save_path=tmp_legacy_path)
        strat_legacy = ProductABBalancedV2Strategy(acc_legacy, watchlist=[c["symbol"] for c in candidates])
        strat_legacy._benchmark = bench_bars
        strat_legacy.latest_candidates = candidates
        strat_legacy.top_industries = top_sectors
        for c in candidates:
            strat_legacy.history_cache[c["symbol"]] = bench_bars
        
        regime_l, w_a_l, w_b_l = strat_legacy.get_current_regime_and_weights(as_of)
        plan_l = strat_legacy._plan(as_of, as_of)

    # 3. Run V3 Strategy & Runtime
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_v3_path = os.path.join(tmp_dir, "v3.sqlite")
        runtime_v3 = TradingRuntime(db_path=tmp_v3_path, mode="ACTIVE", initial_capital=100000.0)
        m_data = {
            "csi300_bars": bench_bars,
            "candidates": candidates,
            "top_sectors": top_sectors
        }
        step_res_v3 = runtime_v3.step(as_of, quotes, m_data)
        
        bal_v3 = runtime_v3.ledger.get_balance("balanced")
        pos_v3 = runtime_v3.ledger.get_positions("balanced")

    print("\n[1] 市场状态 (Market Regime) 对比:")
    print(f"  Legacy Regime: {regime_l} (wA={w_a_l:.2f}, wB={w_b_l:.2f})")
    print(f"  V3 Regime:     {step_res_v3['regime']}")
    assert regime_l == step_res_v3["regime"], "Regime must be identical!"

    print("\n[2] 目标持仓与配置权重对比:")
    v3_summary = step_res_v3["balanced"]
    print(f"  Legacy 计划买入标的: {[p.get('symbol') for p in (plan_l or []) if p.get('action') == 'BUY']}")
    print(f"  V3 实际建仓标的:     {list(pos_v3.keys())}")
    print(f"  V3 目标总仓位:       {v3_summary.get('total_target_weight', 0)*100:.1f}%")
    print(f"  V3 实际成交订单数:   {v3_summary.get('executed_orders', 0)}")
    
    print("\n[3] 关键指标与参数差异根因解释:")
    print("  * 仓位容量差异: 旧版配置 max_positions=3, 导致即便在 STRONG_RISK_ON (上限80%) 也仅能开 3 只标的 (3*15%=45%)；")
    print("                 V3 配置正式升级为 max_positions=6, max_stock_weight=0.15, 实际允许权益仓位完全释放至 80% (5*15% + 1*5%)。")
    print("  * 标的识别差异: 彻底解耦 sz000905 (厦门港务股票) 与 idx_000905 (中证500指数)，消除行情基准混淆。")
    print("  * 记账精度差异: 旧版浮点记账累计精度漂移，V3 全流程定点化 (分/cents) 与 SQLite WAL 原子事务记账。")

if __name__ == "__main__":
    compare_strategies()
