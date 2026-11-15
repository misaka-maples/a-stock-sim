# tests/test_v3_runtime_e2e_reconciliation.py
# -*- coding: utf-8 -*-
"""
End-to-End Runtime & SQLite Reconciliation Test (P1-4)
Strictly verifies that Web API endpoints (/api/strategy/dual_accounts, /api/audit/daily_acceptance)
return assets, cash, and positions that match the SQLite ledger with 0 discrepancy for
BOTH Balanced and Aggressive accounts across 8 dimensions:
- available_cash
- frozen_cash
- total_cash
- stock_market_value
- total_equity
- positions
- tradable_shares
- open_orders

STRICT ACCEPTANCE RULE: If the server is unreachable, test FAILS (no pytest.skip masking).
"""

import json
import urllib.request
import pytest
from pathlib import Path
from astock.trade.runtime import TradingRuntime
from astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger


def test_web_api_matches_sqlite_ledger_exactly_both_accounts():
    db_path = "data/trading_v3.sqlite"
    ledger = SQLiteAccountLedger(db_path)

    # 1. Read API authentication token
    creds_path = Path("data/private/api_credentials.json")
    if not creds_path.exists():
        pytest.fail(f"api_credentials.json not found at {creds_path}")

    with open(creds_path, "r", encoding="utf-8") as f:
        token = json.load(f)["api_token"]

    # 2. Query /api/strategy/dual_accounts (Strict failure on unreachable)
    url_dual = "http://127.0.0.1:8000/api/strategy/dual_accounts"
    req_dual = urllib.request.Request(url_dual)
    req_dual.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req_dual, timeout=5.0) as resp:
            assert resp.status == 200, f"Unexpected HTTP status {resp.status} from {url_dual}"
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        pytest.fail(f"CRITICAL: Web server is NOT reachable at {url_dual}: {exc}")

    assert data.get("success") is True, f"API returned failure: {data}"
    assert data.get("version") == "v3.0", f"API is not running V3.0! Got {data.get('version')}"

    # 3. Reconcile PRIMARY (Balanced) Account across 8 Dimensions
    api_bal = data.get("primary")
    assert api_bal is not None, "Missing primary (Balanced) account data in API response"
    assert api_bal["account_id"] == "balanced"

    db_bal_balance = ledger.get_balance("balanced")
    db_bal_positions = ledger.get_positions("balanced")
    db_bal_orders = ledger.get_active_orders("balanced")

    # (1) available_cash
    assert abs(api_bal["available_cash"] - db_bal_balance.available_cash) < 0.01, (
        f"Balanced available_cash mismatch: API={api_bal['available_cash']}, DB={db_bal_balance.available_cash}"
    )
    # (2) frozen_cash
    assert abs(api_bal["frozen_cash"] - db_bal_balance.frozen_cash) < 0.01, (
        f"Balanced frozen_cash mismatch: API={api_bal['frozen_cash']}, DB={db_bal_balance.frozen_cash}"
    )
    # (3) total_cash
    assert abs(api_bal["total_cash"] - db_bal_balance.cash) < 0.01, (
        f"Balanced total_cash mismatch: API={api_bal['total_cash']}, DB={db_bal_balance.cash}"
    )
    # (4) stock_market_value
    assert abs(api_bal["stock_market_value"] - db_bal_balance.stock_market_value) < 0.01, (
        f"Balanced stock_market_value mismatch: API={api_bal['stock_market_value']}, DB={db_bal_balance.stock_market_value}"
    )
    # (5) total_equity
    assert abs(api_bal["total_equity"] - db_bal_balance.total_equity) < 0.01, (
        f"Balanced total_equity mismatch: API={api_bal['total_equity']}, DB={db_bal_balance.total_equity}"
    )
    # (6) positions count & map
    assert api_bal["positions_count"] == len(db_bal_positions), (
        f"Balanced positions_count mismatch: API={api_bal['positions_count']}, DB={len(db_bal_positions)}"
    )
    assert len(api_bal.get("positions", {})) == len(db_bal_positions)
    # (7) tradable_shares per position
    for sym, pos in db_bal_positions.items():
        assert sym in api_bal["positions"], f"Position {sym} missing from API positions map"
        api_tradable = api_bal["positions"][sym].get("available_shares", 0)
        assert api_tradable == pos.tradable_shares, (
            f"Balanced {sym} tradable shares mismatch: API={api_tradable}, DB={pos.tradable_shares}"
        )
    # (8) open_orders
    assert len(api_bal.get("open_orders", [])) == len(db_bal_orders), (
        f"Balanced open_orders mismatch: API={len(api_bal.get('open_orders', []))}, DB={len(db_bal_orders)}"
    )

    # 4. Reconcile SECONDARY (Aggressive) Account across 8 Dimensions
    api_agg = data.get("secondary")
    assert api_agg is not None, "Missing secondary (Aggressive) account data in API response"
    assert api_agg["account_id"] == "aggressive"

    db_agg_balance = ledger.get_balance("aggressive")
    db_agg_positions = ledger.get_positions("aggressive")
    db_agg_orders = ledger.get_active_orders("aggressive")

    # (1) available_cash
    assert abs(api_agg["available_cash"] - db_agg_balance.available_cash) < 0.01, (
        f"Aggressive available_cash mismatch: API={api_agg['available_cash']}, DB={db_agg_balance.available_cash}"
    )
    # (2) frozen_cash
    assert abs(api_agg["frozen_cash"] - db_agg_balance.frozen_cash) < 0.01, (
        f"Aggressive frozen_cash mismatch: API={api_agg['frozen_cash']}, DB={db_agg_balance.frozen_cash}"
    )
    # (3) total_cash
    assert abs(api_agg["total_cash"] - db_agg_balance.cash) < 0.01, (
        f"Aggressive total_cash mismatch: API={api_agg['total_cash']}, DB={db_agg_balance.cash}"
    )
    # (4) stock_market_value
    assert abs(api_agg["stock_market_value"] - db_agg_balance.stock_market_value) < 0.01, (
        f"Aggressive stock_market_value mismatch: API={api_agg['stock_market_value']}, DB={db_agg_balance.stock_market_value}"
    )
    # (5) total_equity
    assert abs(api_agg["total_equity"] - db_agg_balance.total_equity) < 0.01, (
        f"Aggressive total_equity mismatch: API={api_agg['total_equity']}, DB={db_agg_balance.total_equity}"
    )
    # (6) positions count & map
    assert api_agg["positions_count"] == len(db_agg_positions), (
        f"Aggressive positions_count mismatch: API={api_agg['positions_count']}, DB={len(db_agg_positions)}"
    )
    assert len(api_agg.get("positions", {})) == len(db_agg_positions)
    # (7) tradable_shares per position
    for sym, pos in db_agg_positions.items():
        assert sym in api_agg["positions"], f"Aggressive Position {sym} missing from API positions map"
        api_tradable = api_agg["positions"][sym].get("available_shares", 0)
        assert api_tradable == pos.tradable_shares, (
            f"Aggressive {sym} tradable shares mismatch: API={api_tradable}, DB={pos.tradable_shares}"
        )
    # (8) open_orders
    assert len(api_agg.get("open_orders", [])) == len(db_agg_orders), (
        f"Aggressive open_orders mismatch: API={len(api_agg.get('open_orders', []))}, DB={len(db_agg_orders)}"
    )

    # 5. Verify /api/audit/daily_acceptance endpoint
    url_audit = "http://127.0.0.1:8000/api/audit/daily_acceptance"
    req_audit = urllib.request.Request(url_audit)
    req_audit.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req_audit, timeout=5.0) as resp_a:
            assert resp_a.status == 200, f"Unexpected HTTP status {resp_a.status} from {url_audit}"
            audit_data = json.loads(resp_a.read().decode("utf-8"))
    except Exception as exc:
        pytest.fail(f"CRITICAL: /api/audit/daily_acceptance is NOT reachable: {exc}")

    assert audit_data.get("success") is True
    audit_info = audit_data.get("data", {})
    assert audit_info.get("target_consecutive_days") == 5
    assert "acceptance_status" in audit_info
    assert "consecutive_valid_days" in audit_info
