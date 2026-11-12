#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 4: Product AB v2.0 Dual Strategy Shadow Run Verification Script
Runs a full offline dry-run of Balanced v2.0 and Aggressive v2.0 under current market conditions.
"""

import os
import sys
import copy
import json
import logging
import datetime as dt
from pathlib import Path

BASE_DIR = Path("/home/openarmx/test")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_run")

from sim_trader import SimAccount
from trade_calendar import BEIJING, is_trading_day
from history_data import HistoricalDataFeed
from daily_momentum import evaluate_momentum
from product_ab_v2_strategies import (
    ProductABBalancedV2Strategy,
    ProductBAggressiveV2Strategy,
    ProductABv2DualCoordinator,
    check_risk_off_entry_filter
)


def run_shadow_verification():
    logger.info("==================================================================")
    logger.info("   🚀 开始 Product AB v2.0 双账户影子运行 (Shadow Run) 全流程验证   ")
    logger.info("==================================================================")

    # 1. 载入生产账户数据副本 (只读安全模式，杜绝写入生产数据)
    primary_acc_path = BASE_DIR / "sim_account.json"
    sandbox_acc_path = BASE_DIR / "data" / "sandbox_ab_account.json"

    primary_acc = SimAccount(save_path=str(primary_acc_path), ledger_only=True)
    primary_acc.load()
    primary_acc.save_path = Path("/tmp/mock_primary_shadow.json")

    sandbox_acc = SimAccount(save_path=str(sandbox_acc_path), ledger_only=True)
    if sandbox_acc_path.exists():
        sandbox_acc.load()
    sandbox_acc.save_path = Path("/tmp/mock_sandbox_shadow.json")

    logger.info(f"主账户载入成功: 现金 ¥{primary_acc.cash:,.2f}, 持仓 {len(primary_acc.positions)} 只")
    for s, p in primary_acc.positions.items():
        logger.info(f"  - 主账户持仓: {p.get('name', s)} ({s}), 数量: {p.get('total_shares',0)} 股, 成本: ¥{p.get('cost_price',0):.2f}")

    logger.info(f"第二账户载入成功: 现金 ¥{sandbox_acc.cash:,.2f}, 持仓 {len(sandbox_acc.positions)} 只")

    # 2. 准备历史行情与基准数据
    feed = HistoricalDataFeed()
    bench = feed.peek_benchmark_kline(start_date="2024-01-01", end_date="2026-11-12")
    if not bench:
        raise RuntimeError("无法读取沪深300基准K线数据")
    as_of = bench[-1]["date"]
    execution_day = "2026-11-12"
    logger.info(f"最新基准数据日期: {as_of}, 样本条数: {len(bench)}, 执行日: {execution_day}")

    # 3. 初始化双账户协同调度器
    coord = ProductABv2DualCoordinator(
        account=primary_acc,
        watchlist=["sh600261", "sh603955", "sh600519", "sz000001", "sh601318"],
        feed=feed,
        sandbox_account=sandbox_acc
    )
    coord._benchmark = bench
    coord._benchmark_valid = True
    coord.industry_map = feed.peek_industry_map() if hasattr(feed, "peek_industry_map") else {}

    # 载入持仓与关键标的K线历史
    symbols_to_load = list(set(["sh600261", "sh603955", "sh600519", "sz000001", "sh601318"] + coord.symbols_a))
    cached = feed.peek_universe(symbols_to_load, start_date="2024-01-01", end_date=as_of)
    for sym in symbols_to_load:
        bars = cached.get(sym, {}).get("bars", [])
        if bars:
            coord.history_cache[sym] = bars

    # 构建候选池指标
    cand_list = []
    for sym in coord.symbols_a[:8]:
        bars = coord.history_cache.get(sym, [])
        if len(bars) >= 120:
            sig = evaluate_momentum(bars, as_of, coord.config)
            if sig:
                cand_list.append(dict(symbol=sym, name=cached.get(sym, {}).get("name", sym), **sig))
    coord.latest_candidates = cand_list
    logger.info(f"已评估生成测试候选标的: {len(cand_list)} 只")

    # 4. 执行交易规划
    coord._plan(as_of, execution_day)

    # 5. 验证市场状态与仓位硬顶
    regime = coord.state.get("current_regime", "RISK_OFF")
    bal_cap = coord.get_max_regime_total_wt(regime)
    agg_cap = coord.aggressive_strategy.get_max_regime_total_wt(regime)
    w_a = coord.state.get("weight_a", 0.70)
    w_b = coord.state.get("weight_b", 0.30)

    logger.info("------------------------------------------------------------------")
    logger.info(f"【市场状态判定】: {regime}")
    logger.info(f"【Balanced 稳健账户】: 仓位上限 {bal_cap*100:.1f}%, A/B预算比例: A={w_a*100:.0f}% / B={w_b*100:.0f}%")
    logger.info(f"【Aggressive 进攻账户】: 仓位上限 {agg_cap*100:.1f}%, 纯Product B预算: 100% B")

    assert regime == "RISK_OFF", f"预期市场状态为 RISK_OFF, 实际判定为: {regime}"
    assert bal_cap == 0.30, f"Balanced RISK_OFF 上限必须为 30%, 实际为: {bal_cap}"
    assert agg_cap == 0.50, f"Aggressive RISK_OFF 上限必须为 50%, 实际为: {agg_cap}"
    assert w_a == 0.70 and w_b == 0.30, f"Balanced RISK_OFF 预算必须为 70/30, 实际为: {w_a}/{w_b}"

    # 6. 验证 RISK_OFF 门槛过滤效果
    top_sectors = ["通信设备", "半导体", "汽车零部件", "电网设备"]
    ind_scores = {"通信设备": 8.5, "半导体": 6.2, "汽车零部件": 4.1, "电网设备": 2.0}
    logger.info("------------------------------------------------------------------")
    logger.info(f"【RISK_OFF 弱市严格准入门槛过滤检验】(Top 4行业: {top_sectors}):")

    for c in cand_list:
        sym = c["symbol"]
        bars = coord.history_cache.get(sym, [])
        ok, reason = check_risk_off_entry_filter(c, bars, coord.industry_map, top_sectors, ind_scores)
        status_icon = "🟢 通过" if ok else "🔴 拦截"
        logger.info(f"  标的 {c.get('name', sym)} ({sym}): {status_icon} -> {reason}")

    # 7. 验证持仓保护与止盈止损状态 (模拟盘中 tick)
    quotes = {
        "sh600261": {"price": 6.37, "current": 6.37, "close": 6.37},
        "sh603955": {"price": 33.34, "current": 33.34, "close": 33.34}
    }
    trading_tick_time = dt.datetime(2026, 11, 10, 10, 30, 0)
    coord.on_tick(quotes, now=trading_tick_time)

    # 检查主账户持仓完整性与风控监控线
    monitors = coord.state.get("position_monitors", {})
    pending_exits = coord.state.get("pending_exits", {})
    logger.info("------------------------------------------------------------------")
    logger.info("【主账户持仓风控监控检查】:")
    assert "sh600261" in primary_acc.positions, "阳光照明必须安全保存在主账户持仓中"
    assert "sh603955" in primary_acc.positions, "大千生态必须安全保存在主账户持仓中"
    assert len(pending_exits) == 0, f"主账户持仓未触及止盈止损线，不得产生任何卖出委托，实际为: {pending_exits}"

    for sym, mon in monitors.items():
        pos = primary_acc.positions.get(sym, {})
        cost = float(pos.get("cost_price", 0.0))
        cur_p = quotes.get(sym, {}).get("current", cost)
        pnl_pct = ((cur_p / cost) - 1.0) * 100.0 if cost > 0 else 0.0
        floor_p = cost * 1.03
        logger.info(f"  - {pos.get('name', sym)} ({sym}): 成本 ¥{cost:.2f}, 现价 ¥{cur_p:.2f}, 浮盈 +{pnl_pct:.2f}% | 峰值 ¥{mon.get('peak_price',0):.2f} (+{mon.get('peak_profit_pct',0):.2f}%), +3%底线 ¥{floor_p:.2f}, 跟踪止盈线 ¥{mon.get('trailing_stop_line',0):.2f}")

    # 8. 验证第二账户自治与隔离性
    logger.info("------------------------------------------------------------------")
    logger.info("【第二账户 (Aggressive) 状态与隔离性检查】:")
    sb_status = coord.aggressive_strategy.allocation_status(now=trading_tick_time)
    logger.info(f"  - 状态: {sb_status.get('regime')}, 仓位上限: {sb_status.get('max_regime_total_wt_num')*100:.1f}%, 当前持仓: {sb_status.get('sandbox_positions_count')}/{sb_status.get('sandbox_max_positions')}")
    logger.info(f"  - 是否允许开仓: {sb_status.get('allow_new_entries')}, 原因: {sb_status.get('no_entry_reason')}")
    assert coord.aggressive_strategy.account is sandbox_acc
    assert coord.account is not sandbox_acc
    assert len(coord.aggressive_strategy.account.positions) == len(sandbox_acc.positions)

    # 9. 状态总览 API 校验
    alloc_summary = coord.allocation_status(now=trading_tick_time)
    logger.info("------------------------------------------------------------------")
    logger.info("【双账户整体调度汇总】:")
    logger.info(f"  - 主账户: 持仓 {alloc_summary.get('primary_positions_count')}/{alloc_summary.get('primary_max_positions')}, 仓位占比: {alloc_summary.get('current_stock_weight')*100:.1f}%, 上限: {alloc_summary.get('max_regime_total_wt_num')*100:.1f}%")
    logger.info(f"  - 第二账户: 持仓 {alloc_summary.get('sandbox_positions_count')}/{alloc_summary.get('sandbox_max_positions')}, 仓位占比: {alloc_summary.get('sandbox_stock_weight',0)*100:.1f}%, 上限: {alloc_summary.get('sandbox_max_regime_total_wt_num',0)*100:.1f}%")

    logger.info("==================================================================")
    logger.info("   ✅ 影子运行 (Shadow Run) 验证全部通过！双账户系统运行平稳可靠！   ")
    logger.info("==================================================================")
    return True


if __name__ == "__main__":
    success = run_shadow_verification()
    sys.exit(0 if success else 1)
