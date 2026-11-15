# audit_migration.py
import json
import sqlite3

def audit():
    print("==================================================")
    print("  A-STOCK-SIM V3: JSON vs SQLITE MIGRATION AUDIT  ")
    print("==================================================")

    # 1. Load Legacy JSON files
    with open("data/backup_pre_v3/sim_account.json") as f:
        json_bal = json.load(f)
    sum_bal = json_bal.get("summary", {})

    with open("data/backup_pre_v3/sandbox_ab_account.json") as f:
        json_agg = json.load(f)
    sum_agg = json_agg.get("summary", {})

    # 2. Connect to SQLite V3
    conn = sqlite3.connect("data/trading_v3.sqlite")
    conn.row_factory = sqlite3.Row

    # --- BALANCED ACCOUNT AUDIT ---
    print("\n--- [1] BALANCED ACCOUNT (稳健主账户) 对账 ---")
    acc_bal = conn.execute("SELECT * FROM accounts WHERE account_id = 'balanced'").fetchone()
    
    j_init = float(sum_bal.get("initial_cash", 100000.0))
    j_cash = float(sum_bal.get("cash", 0.0))
    s_cash = acc_bal["total_cash_cents"] / 100.0
    s_avail = acc_bal["available_cash_cents"] / 100.0
    print(f"初始资金: JSON = ¥{j_init:,.2f}")
    print(f"当前现金: JSON = ¥{j_cash:,.2f} | SQLite = ¥{s_cash:,.2f} | 差额: ¥{j_cash - s_cash:.4f}")
    print(f"可用现金: JSON = ¥{sum_bal.get('cash'):,.2f} | SQLite = ¥{s_avail:,.2f}")

    # Check Positions
    j_pos = json_bal.get("positions", {})
    s_pos = conn.execute("SELECT * FROM positions WHERE account_id = 'balanced'").fetchall()
    print(f"持仓数量: JSON = {len(j_pos)} | SQLite = {len(s_pos)}")

    pos_all_match = True
    for p in s_pos:
        sym = p["symbol"]
        jp = j_pos.get(sym)
        if not jp:
            print(f"  [ERROR] SQLite 持仓 {sym} 不存在于 JSON 中!")
            pos_all_match = False
            continue
        shares_match = (p["total_shares"] == jp.get("total_shares"))
        avail_match = (p["tradable_shares"] == jp.get("available_shares"))
        cost_match = abs(p["avg_cost"] - jp.get("cost_price", 0.0)) < 1e-3
        print(f"  标的 {sym} ({p['name']}):")
        print(f"    - 总持股: JSON={jp.get('total_shares')} vs SQLite={p['total_shares']} (匹配: {shares_match})")
        print(f"    - 可卖股: JSON={jp.get('available_shares')} vs SQLite={p['tradable_shares']} (匹配: {avail_match})")
        print(f"    - 成本价: JSON={jp.get('cost_price'):.4f} vs SQLite={p['avg_cost']:.4f} (匹配: {cost_match})")
        if not (shares_match and avail_match and cost_match):
            pos_all_match = False

    # Check Position Lots in SQLite
    print("\n  SQLite T+1 批次明细 (position_lots):")
    lots = conn.execute("SELECT * FROM position_lots WHERE account_id = 'balanced'").fetchall()
    for lot in lots:
        print(f"    - Lot {lot['lot_id']}: {lot['symbol']} {lot['shares']}股 (剩余: {lot['remaining_shares']}股, 可卖: {lot['is_tradable_t1']}, 买入日: {lot['buy_date']}, 成本: ¥{lot['cost_price']:.4f})")

    # Check Trades & Fills by ID lookup
    j_trades = json_bal.get("trades", [])
    s_fills = conn.execute("SELECT * FROM fills WHERE account_id = 'balanced'").fetchall()
    fills_by_id = {f["fill_id"]: f for f in s_fills}
    print(f"\n历史成交笔数: JSON = {len(j_trades)} | SQLite = {len(s_fills)}")

    trade_mismatch = 0
    for jt in j_trades:
        tid = jt.get("trade_id")
        sf = fills_by_id.get(tid)
        if not sf:
            trade_mismatch += 1
            print(f"  [MISSING] JSON trade {tid} not in SQLite fills!")
            continue
        match = (
            jt.get("symbol") == sf["symbol"] and
            jt.get("side") == sf["side"] and
            jt.get("shares") == sf["fill_shares"] and
            abs(jt.get("price", 0.0) - sf["fill_price"]) < 1e-3 and
            abs(jt.get("fees", 0.0) - sf["fees_cents"] / 100.0) < 1e-2 and
            abs(jt.get("amount", 0.0) - sf["amount_cents"] / 100.0) < 1e-2
        )
        if not match:
            trade_mismatch += 1
            print(f"  [MISMATCH {tid}] JSON: {jt['symbol']} {jt['side']} {jt['shares']}@{jt['price']} fee={jt['fees']} amt={jt['amount']} vs SQLITE: {sf['symbol']} {sf['side']} {sf['fill_shares']}@{sf['fill_price']} fee={sf['fees_cents']/100.0} amt={sf['amount_cents']/100.0}")

    if trade_mismatch == 0:
        print(f"  全部 {len(s_fills)} 笔历史成交记录逐项核对 100% 精确匹配！")
    else:
        print(f"  [WARNING] 存在 {trade_mismatch} 笔成交记录不匹配!")

    # Check PnL and Total Equity
    j_eq = float(sum_bal.get("total_equity", 0.0))
    s_mv = sum(p["market_value_cents"] for p in s_pos) / 100.0
    s_eq = s_cash + s_mv
    print(f"\n已实现盈亏: JSON = ¥{sum_bal.get('realized_pnl', 0.0):,.2f}")
    print(f"未实现盈亏: JSON = ¥{sum_bal.get('unrealized_pnl', 0.0):,.2f}")
    print(f"总持仓市值: JSON = ¥{sum_bal.get('market_value', 0.0):,.2f} | SQLite = ¥{s_mv:,.2f}")
    print(f"总资产权益: JSON = ¥{j_eq:,.2f} | SQLite = ¥{s_eq:,.2f} | 差额: ¥{j_eq - s_eq:.4f}")

    # --- AGGRESSIVE ACCOUNT AUDIT ---
    print("\n--- [2] AGGRESSIVE ACCOUNT (进攻账户 / Sandbox) 对账 ---")
    acc_agg = conn.execute("SELECT * FROM accounts WHERE account_id = 'aggressive'").fetchone()
    j_agg_init = float(sum_agg.get("initial_cash", 100000.0))
    j_agg_cash = float(sum_agg.get("cash", 100000.0))
    s_agg_cash = acc_agg["total_cash_cents"] / 100.0
    print(f"初始资金: JSON = ¥{j_agg_init:,.2f}")
    print(f"当前现金: JSON = ¥{j_agg_cash:,.2f} | SQLite = ¥{s_agg_cash:,.2f} | 差额: ¥{j_agg_cash - s_agg_cash:.4f}")
    
    s_agg_pos = conn.execute("SELECT * FROM positions WHERE account_id = 'aggressive'").fetchall()
    print(f"持仓数量: JSON = {len(json_agg.get('positions', {}))} | SQLite = {len(s_agg_pos)}")
    agg_fill_count = conn.execute("SELECT COUNT(*) as c FROM fills WHERE account_id = 'aggressive'").fetchone()['c']
    print(f"历史成交: JSON = {len(json_agg.get('trades', []))} | SQLite = {agg_fill_count}")

    # --- INTERNAL INTEGRITY VERIFICATION ---
    print("\n--- [3] SQLITE LEDGER 内部会计恒等式与完整性验证 ---")
    from src.astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
    ledger = SQLiteAccountLedger("data/trading_v3.sqlite")
    integ_bal = ledger.verify_integrity("balanced")
    integ_agg = ledger.verify_integrity("aggressive")

    print(f"Balanced 内部会计账本校验: 有效 = {integ_bal['valid']} | 现金偏差 = ¥{integ_bal['cash_discrepancy']} | 批次偏差 = {integ_bal['lot_discrepancies']}")
    print(f"Aggressive 内部会计账本校验: 有效 = {integ_agg['valid']} | 现金偏差 = ¥{integ_agg['cash_discrepancy']} | 批次偏差 = {integ_agg['lot_discrepancies']}")

if __name__ == "__main__":
    audit()
