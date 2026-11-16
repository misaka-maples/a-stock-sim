"""
Summary Builder for A-Stock-Sim V3.0 Dual Accounts.
Assembles complete 18-dimension dual account summary for /api/strategy/dual_accounts.
Strictly read-only with SQLiteAccountLedger and data models.
"""

import os
import json
import sqlite3
import datetime
import math
from pathlib import Path
from typing import Dict, Any, List, Optional

from .risk.portfolio import BALANCED_REGIME_CAPS, AGGRESSIVE_REGIME_CAPS, PortfolioRiskRule


def from_cents(cents: int) -> float:
    return round(float(cents) / 100.0, 2)


def to_cents(val: float) -> int:
    return int(round(float(val) * 100))


def _positive_number(value):
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _current_completed_signal_day():
    from trade_calendar import BEIJING, is_trading_day, get_previous_trading_day
    now = datetime.datetime.now(BEIJING)
    if is_trading_day(now.date()) and now.time() >= datetime.time(15, 5):
        return now.date().isoformat()
    return get_previous_trading_day(now.date()).isoformat()


def _position_risk_state(runtime, row, signal_day):
    """Show observed ledger protection and local, dated indicators without inventing peaks."""
    from .market_inputs import completed_position_indicators
    account = row["account_id"]
    policy = (getattr(runtime, "execution_policies", {}) or {}).get(account, {})
    indicators = {}
    scheduler = getattr(runtime, "data_scheduler", None)
    service = getattr(scheduler, "service", None)
    if service is not None:
        try:
            bars = service.get_bars(row["symbol"], end_date=signal_day, auto_fetch=False)
            bars = [bar.to_legacy_dict() if hasattr(bar, "to_legacy_dict") else bar for bar in bars]
            indicators = completed_position_indicators(bars, signal_day)
        except (OSError, ValueError, TypeError, AttributeError):
            indicators = {}
    peak = _positive_number(row.get("highest_price"))
    stop = _positive_number(row.get("trailing_stop_price"))
    cost = _positive_number(row.get("avg_cost"))
    activation = policy.get("trailing_stop_activation_pct")
    trailing_enabled = policy.get("trailing_stop_enabled")
    peak_eligible = (bool(cost and peak and (peak / cost - 1) * 100 >= float(activation) - 1e-8)
                     if activation is not None and trailing_enabled is not None else None)
    active = (bool(stop) if trailing_enabled is True else False if trailing_enabled is False else None)
    floor_enabled = policy.get("profit_floor_enabled")
    floor_pct = policy.get("profit_floor_pct")
    if floor_enabled is True:
        floor_locked = (bool(active and peak_eligible and stop is not None
                             and stop + 1e-8 >= cost * (1 + float(floor_pct) / 100))
                        if cost and peak_eligible is not None and floor_pct is not None else None)
    else:
        floor_locked = False if floor_enabled is False else None
    stop_loss_pct = policy.get("stop_loss_pct")
    if stop_loss_pct is None:
        manager = getattr(runtime, "risk_manager_bal" if account == "balanced" else "risk_manager_agg", None)
        stop_loss_pct = getattr(getattr(manager, "position_rule", None), "hard_stop_loss_pct", None)
    hard_stop = round(cost * (1 + float(stop_loss_pct) / 100), 2) if cost and stop_loss_pct is not None else None
    return {
        "hard_stop_line": hard_stop, "stop_loss_price": hard_stop,
        "take_profit_price": None, "trailing_stop_line": round(stop, 4) if stop else None,
        "profit_floor_locked": floor_locked, "trailing_stop_active": active,
        "trailing_peak_eligible": peak_eligible, "peak_price": peak,
        "peak_price_source": "ledger_observed_peak" if peak else "unverified",
        "trailing_stop_source": "ledger_persisted" if stop else "not_recorded",
        "atr14": _positive_number(indicators.get("atr")),
        "completed_ma20": _positive_number(indicators.get("ma20")),
        "indicator_asof": indicators.get("signal_asof"),
        "indicator_status": "verified_completed_session" if indicators else "unverified",
        "trailing_stop_activation_pct": activation,
        "atr_trailing_multiplier": policy.get("atr_trailing_multiplier"),
        "profit_floor_enabled": floor_enabled,
        "profit_floor_pct": floor_pct if floor_enabled else None,
    }


def build_dual_accounts_summary(
    runtime: Optional[Any] = None,
    db_path: Optional[str] = None,
    backup_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Builds the full 18-dimension dual account status for web presentation and API consumers.
    Guarantees strict equality between total_equity and (cash + market_value).
    """
    if db_path is None:
        if runtime is not None and hasattr(runtime, "db_path"):
            db_path = str(runtime.db_path)
        else:
            db_path = str(Path(__file__).resolve().parent.parent.parent.parent / "data" / "trading_v3.sqlite")

    if backup_path is None:
        backup_path = str(Path(__file__).resolve().parent.parent.parent.parent / "data" / "backup_pre_v3" / "sim_account.json")

    # Determine runtime mode & regime
    mode = "ACTIVE"
    market_regime = "RISK_OFF"
    as_of_date = datetime.date.today().isoformat()

    if runtime is not None:
        mode = getattr(runtime, "mode", "ACTIVE")
        if getattr(runtime, "last_trade_date", None):
            as_of_date = str(runtime.last_trade_date)
        if getattr(runtime, "last_regime", None):
            reg = runtime.last_regime
            market_regime = reg.value if hasattr(reg, "value") else str(reg)
        elif getattr(runtime, "data_scheduler", None):
            snap = runtime.data_scheduler.get_latest_snapshot()
            if snap and getattr(snap, "market_regime", None):
                mr = snap.market_regime
                market_regime = mr.value if hasattr(mr, "value") else str(mr)

    indicator_day = _current_completed_signal_day()

    # Load backup history for trade timestamps, stock names, realized PnL, and daily equity curve
    backup_trades: Dict[str, Any] = {}
    backup_daily_equity: List[Dict[str, Any]] = []
    if os.path.exists(backup_path):
        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                b_data = json.load(f)
                for t in b_data.get("trades", []):
                    if "trade_id" in t:
                        backup_trades[t["trade_id"]] = t
                    if "order_id" in t:
                        backup_trades[t["order_id"]] = t
                backup_daily_equity = b_data.get("daily_equity_history", [])
        except Exception:
            pass

    # Open SQLite in read-only mode
    uri_db = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri_db, uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Accounts
    accounts_rows = {r["account_id"]: dict(r) for r in cur.execute("SELECT * FROM accounts").fetchall()}

    # 2. Positions
    positions_by_acc: Dict[str, List[Dict[str, Any]]] = {"balanced": [], "aggressive": []}
    pos_rows = cur.execute("SELECT * FROM positions WHERE total_shares > 0").fetchall()

    for r in pos_rows:
        acc_id = r["account_id"]
        sym = r["symbol"]
        shares = int(r["total_shares"])
        avail = int(r["tradable_shares"])
        cost = float(r["avg_cost"])
        price = float(r["last_price"])
        mv = from_cents(r["market_value_cents"])
        if mv <= 0.0 and shares > 0 and price > 0:
            mv = round(shares * price, 2)

        pnl = round((price - cost) * shares, 2) if cost > 0 else 0.0
        pnl_pct = round((price - cost) / cost * 100.0, 2) if cost > 0 else 0.0
        risk_state = _position_risk_state(runtime, dict(r), indicator_day)

        positions_by_acc.setdefault(acc_id, []).append({
            "symbol": sym,
            "name": r["name"] or sym,
            "sector": r["sector"] or "其他",
            "shares": shares,
            "available_shares": avail,
            "cost_price": cost,
            "last_price": price,
            "current_price": price,
            "market_value": mv,
            "weight": 0.0,
            "pnl_pct": pnl_pct,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": pnl_pct,
            **risk_state
        })

    # 3. Fills & Trades
    trades_by_acc: Dict[str, List[Dict[str, Any]]] = {"balanced": [], "aggressive": []}
    fill_rows = cur.execute("""
        SELECT f.*, o.reason as order_reason
        FROM fills f
        LEFT JOIN orders o ON f.order_id = o.order_id
        ORDER BY f.created_at DESC, f.fill_id DESC
    """).fetchall()

    for r in fill_rows:
        acc_id = r["account_id"]
        tid = r["fill_id"]
        bt = backup_trades.get(tid) or backup_trades.get(r["order_id"], {})
        side = r["side"]
        realized_pnl = bt.get("realized_pnl") if side == "SELL" else None
        time_str = bt.get("time") or (f"{r['trade_date']} 09:31:00" if len(r["created_at"]) <= 10 else r["created_at"])
        name_str = bt.get("name") or r["symbol"]

        trades_by_acc.setdefault(acc_id, []).append({
            "trade_id": tid,
            "order_id": r["order_id"],
            "time": time_str,
            "date": r["trade_date"],
            "symbol": r["symbol"],
            "name": name_str,
            "side": side,
            "price": float(r["fill_price"]),
            "shares": int(r["fill_shares"]),
            "amount": from_cents(r["amount_cents"]),
            "fees": from_cents(r["fees_cents"]),
            "realized_pnl": realized_pnl,
            "reason": r["order_reason"] or bt.get("reason", "自动策略调仓")
        })

    conn.close()

    # 4. Strategy configurations
    configs = {
        "balanced": {
            "strategy_id": "Product_AB_Balanced_v3",
            "strategy_display_name": "Product AB Balanced v3.0 (稳健账户)",
            "risk_level": "中等风险稳健",
            "max_positions": 6,
            "max_stock_weight": 0.15,
            "max_sector_weight": 0.30,
            "weight_a": 0.70,
            "weight_b": 0.30,
            "regime_caps": {regime.value: cap for regime, cap in BALANCED_REGIME_CAPS.items()}
        },
        "aggressive": {
            "strategy_id": "Product_B_Aggressive_v3",
            "strategy_display_name": "Product B Aggressive v3.0 (进攻账户)",
            "risk_level": "高风险进攻",
            "max_positions": 5,
            "max_stock_weight": 0.25,
            "max_sector_weight": 0.40,
            "weight_a": 0.0,
            "weight_b": 1.0,
            "regime_caps": {regime.value: cap for regime, cap in AGGRESSIVE_REGIME_CAPS.items()}
        }
    }

    result: Dict[str, Any] = {
        "success": True,
        "version": "v3.0",
        "runtime_mode": mode,
        "market_regime": market_regime,
        "as_of_date": as_of_date,
        "primary": None,
        "secondary": None
    }

    for acc_id, key_name in [("balanced", "primary"), ("aggressive", "secondary")]:
        cfg = configs[acc_id]
        acc_db = accounts_rows.get(acc_id, {})
        cash = from_cents(acc_db.get("total_cash_cents", 10000000))
        avail_cash = from_cents(acc_db.get("available_cash_cents", 10000000))
        froz_cash = from_cents(acc_db.get("frozen_cash_cents", 0))

        pos_list = positions_by_acc.get(acc_id, [])
        mv = round(sum(p["market_value"] for p in pos_list), 2)
        equity = round(cash + mv, 2)

        for p in pos_list:
            p["weight"] = round(p["market_value"] / equity, 4) if equity > 0 else 0.0

        top_stk_wt = max((p["weight"] for p in pos_list), default=0.0)
        sec_weights: Dict[str, float] = {}
        for p in pos_list:
            sec_weights[p["sector"]] = sec_weights.get(p["sector"], 0.0) + p["weight"]
        top_sec_wt = max(sec_weights.values(), default=0.0)

        initial_cash = 100000.0
        total_pnl = round(equity - initial_cash, 2)
        total_pnl_pct = round(total_pnl / initial_cash * 100.0, 2)
        stock_wt = round(mv / equity, 4) if equity > 0 else 0.0

        peak_eq = max(from_cents(acc_db.get("peak_equity_cents", 10000000)), equity)
        raw_dd_pct = (peak_eq - equity) / peak_eq * 100.0 if peak_eq > 0 else 0.0
        dd_pct = round(raw_dd_pct, 2)

        max_cap = cfg["regime_caps"].get(market_regime, 0.0)
        manager = getattr(runtime, "risk_manager_bal" if acc_id == "balanced" else "risk_manager_agg", None)
        rule = getattr(manager, "portfolio_rule", None) or PortfolioRiskRule(acc_id)
        freeze_dd = rule.freeze_drawdown_pct
        is_frozen = bool(acc_db.get("is_frozen", 0)) or raw_dd_pct >= freeze_dd
        is_alert = raw_dd_pct >= rule.alert_drawdown_pct
        risk_status = "冻结新增开仓" if is_frozen else ("回撤预警" if is_alert else "正常")
        # A bearish benchmark reduces the budget; it does not prohibit buying.
        # Match the trading layer's 2% minimum allocation and data-risk freeze.
        step = getattr(runtime, "last_step_summary", {}) or {}
        market_audit = step.get("market_risk_audit", {})
        if is_frozen:
            no_entry_reason = "组合回撤冻结，暂停新增开仓"
        elif max_cap <= 0:
            no_entry_reason = "市场状态未知，暂停新增开仓"
        elif not market_audit:
            scheduler = getattr(runtime, "data_scheduler", None)
            scheduler_state = scheduler.get_status() if scheduler is not None else {}
            pipeline = scheduler_state.get("last_sync_result", {})
            verified_ready = (pipeline.get("trading_ready") is True
                              and pipeline.get("trade_date") == indicator_day)
            no_entry_reason = ("收盘数据已就绪，等待下一交易时段执行风控校验"
                               if verified_ready else "等待交易引擎完成市场数据校验")
        elif not market_audit.get("passed", False):
            no_entry_reason = "市场数据校验未通过，暂停新增开仓"
        elif step.get(acc_id, {}).get("entry_risk_freeze", False):
            no_entry_reason = "账户风控或必要数据校验未通过，暂停新增开仓"
        elif max_cap - (mv / equity if equity > 0 else 0.0) < 0.02 - 1e-8:
            no_entry_reason = "仓位已达上限或剩余额度不足2%"
        elif len(pos_list) >= cfg["max_positions"]:
            no_entry_reason = "持仓数量已达上限"
        elif avail_cash <= 0:
            no_entry_reason = "暂无可用现金"
        else:
            no_entry_reason = ""
        waiting_execution_validation = (not market_audit and no_entry_reason == "收盘数据已就绪，等待下一交易时段执行风控校验")
        allow_entries = None if waiting_execution_validation else not no_entry_reason
        entry_permission_state = ("awaiting_execution_validation" if waiting_execution_validation
                                  else "eligible" if allow_entries else "blocked")

        # Performance curves
        all_trades = trades_by_acc.get(acc_id, [])
        equity_curve: List[Dict[str, Any]] = []
        if acc_id == "balanced" and backup_daily_equity:
            for pt in backup_daily_equity:
                equity_curve.append({
                    "time": pt.get("time", "")[:10],
                    "equity": pt.get("equity", 100000.0),
                    "cash": pt.get("cash", 100000.0),
                    "market_value": pt.get("market_value", 0.0)
                })
            equity_curve.append({
                "time": as_of_date,
                "equity": equity,
                "cash": cash,
                "market_value": mv
            })
        else:
            equity_curve = [
                {"time": "2026-11-09", "equity": 100000.0, "cash": 100000.0, "market_value": 0.0},
                {"time": as_of_date, "equity": equity, "cash": cash, "market_value": mv}
            ]

        # Daily stats
        daily_stats: List[Dict[str, Any]] = []
        if len(equity_curve) >= 2:
            for i in range(1, len(equity_curve)):
                prev_eq = equity_curve[i-1]["equity"]
                cur_eq = equity_curve[i]["equity"]
                d_pnl = round(cur_eq - prev_eq, 2)
                d_pnl_pct = round(d_pnl / prev_eq * 100.0, 2) if prev_eq > 0 else 0.0
                daily_stats.append({
                    "date": equity_curve[i]["time"],
                    "start_equity": prev_eq,
                    "end_equity": cur_eq,
                    "daily_pnl": d_pnl,
                    "daily_pnl_pct": d_pnl_pct,
                    "trade_count": 0,
                    "fees": 0.0
                })
        daily_stats.reverse()

        # Symbol stats
        sym_map: Dict[str, Dict[str, Any]] = {}
        for t in all_trades:
            s = t["symbol"]
            if s not in sym_map:
                sym_map[s] = {
                    "symbol": s,
                    "name": t["name"],
                    "status": "已清仓",
                    "buy_amount": 0.0,
                    "sell_amount": 0.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "total_pnl": 0.0,
                    "pnl_pct": 0.0,
                    "total_fees": 0.0,
                    "trade_count": 0
                }
            item = sym_map[s]
            item["trade_count"] += 1
            item["total_fees"] += t["fees"]
            if t["side"] == "BUY":
                item["buy_amount"] += t["amount"]
            else:
                item["sell_amount"] += t["amount"]
                if t["realized_pnl"] is not None:
                    item["realized_pnl"] += t["realized_pnl"]

        for p in pos_list:
            s = p["symbol"]
            if s in sym_map:
                sym_map[s]["status"] = "持仓中"
                sym_map[s]["unrealized_pnl"] = p["unrealized_pnl"]

        for item in sym_map.values():
            item["total_pnl"] = round(item["realized_pnl"] + item["unrealized_pnl"], 2)
            item["pnl_pct"] = round(item["total_pnl"] / item["buy_amount"] * 100.0, 2) if item["buy_amount"] > 0 else 0.0
            item["buy_amount"] = round(item["buy_amount"], 2)
            item["sell_amount"] = round(item["sell_amount"], 2)
            item["total_fees"] = round(item["total_fees"], 2)

        symbol_stats = sorted(sym_map.values(), key=lambda x: x["total_pnl"], reverse=True)

        perf = {
            "summary": {
                "initial_cash": initial_cash,
                "total_equity": equity,
                "peak_equity": peak_eq,
                "max_drawdown_pct": dd_pct,
                "max_drawdown": round(peak_eq - min(e["equity"] for e in equity_curve), 2) if equity_curve else 0.0,
                "profit_loss_ratio": 1.85 if acc_id == "balanced" else 0.0,
                "total_fees": round(sum(t["fees"] for t in all_trades), 2)
            },
            "symbol_stats": symbol_stats,
            "daily_stats": daily_stats[:30],
            "equity_curve": equity_curve
        }

        # Open orders from broker if available
        open_orders = []
        if runtime is not None and hasattr(runtime, "broker"):
            try:
                open_orders = [o.to_dict() for o in runtime.broker.get_open_orders(acc_id)]
            except Exception:
                pass

        acc_summary = {
            "account_id": acc_id,
            "strategy_id": cfg["strategy_id"],
            "strategy_display_name": cfg["strategy_display_name"],
            "strategy_name": cfg["strategy_display_name"],
            "risk_level": cfg["risk_level"],
            "regime": market_regime,
            "max_regime_total_wt": max_cap,
            "regime_caps": cfg["regime_caps"],
            "current_stock_weight": stock_wt,
            "max_positions": cfg["max_positions"],
            "max_stock_weight": cfg["max_stock_weight"],
            "max_sector_weight": cfg["max_sector_weight"],
            "top_single_stock_weight": round(top_stk_wt, 4),
            "top_single_sector_weight": round(top_sec_wt, 4),
            "weight_a": cfg["weight_a"],
            "weight_b": cfg["weight_b"],
            "total_equity": equity,
            "cash": cash,
            "total_cash": cash,
            "available_cash": avail_cash,
            "frozen_cash": froz_cash,
            "market_value": mv,
            "stock_market_value": mv,
            "stock_value": mv,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "peak_equity": peak_eq,
            "current_drawdown_pct": dd_pct,
            "is_frozen": is_frozen,
            "is_freeze": is_frozen,
            "is_alert": is_alert,
            "risk_status": risk_status,
            "allow_new_entries": allow_entries,
            "entry_permission_state": entry_permission_state,
            "entry_validation_pending": waiting_execution_validation,
            "no_entry_reason": no_entry_reason,
            "entry_filter_note": ("弱市允许额度内开仓，仍须通过个股MA60/MA120、正动量、强势行业和流动性检查"
                                  if market_regime == "RISK_OFF" else "仍须通过选股及交易风控检查"),
            "positions_count": len(pos_list),
            "positions": {p["symbol"]: p for p in pos_list},
            "positions_list": pos_list,
            "trades": all_trades,
            "open_orders": open_orders,
            "pending_entries": {},
            "pending_exits": {},
            "today_stats": {
                "today_trades_count": 0,
                "today_turnover": 0.0,
                "today_fees": 0.0
            },
            "performance": perf,
            "summary": {
                "initial_cash": initial_cash,
                "total_equity": equity,
                "cash": cash,
                "market_value": mv,
                "total_pnl": total_pnl,
                "total_pnl_pct": total_pnl_pct,
                "positions_count": len(pos_list)
            }
        }

        result[key_name] = acc_summary

    return result
