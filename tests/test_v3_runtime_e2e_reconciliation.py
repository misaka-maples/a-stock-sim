# tests/test_v3_runtime_e2e_reconciliation.py
# -*- coding: utf-8 -*-
"""
End-to-End Runtime & SQLite Reconciliation Test
Verifies that Web API endpoints (/api/strategy/dual_accounts, /api/audit/daily_acceptance)
return assets, cash, and positions that match the SQLite ledger with 0 discrepancy.
"""

import json
import urllib.request
import pytest
from pathlib import Path
from astock.trade.runtime import TradingRuntime

def test_web_api_matches_sqlite_ledger_exactly():
    db_path = "data/trading_v3.sqlite"
    runtime = TradingRuntime(db_path=db_path, mode="ACTIVE")

    # Read token
    creds_path = Path("data/private/api_credentials.json")
    if not creds_path.exists():
        pytest.skip("api_credentials.json not found")

    with open(creds_path, "r") as f:
        token = json.load(f)["api_token"]

    req = urllib.request.Request("http://127.0.0.1:8000/api/strategy/dual_accounts")
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
    except Exception as exc:
        pytest.skip(f"Web server not reachable: {exc}")

    # Query SQLite directly
    b_bal = runtime.ledger.get_balance("balanced")
    b_pos = runtime.ledger.get_positions("balanced")

    # Compare API vs SQLite
    api_bal = data["primary"]
    assert api_bal["account_id"] == "balanced"
    assert abs(api_bal["cash"] - b_bal.cash) < 0.01, f"Cash diff: API={api_bal['cash']}, DB={b_bal.cash}"
    assert abs(api_bal["total_equity"] - b_bal.total_equity) < 0.01, f"Equity diff: API={api_bal['total_equity']}, DB={b_bal.total_equity}"
    assert api_bal["positions_count"] == len(b_pos)
    assert api_bal["max_positions"] == 6

    # Verify audit endpoint
    req_audit = urllib.request.Request("http://127.0.0.1:8000/api/audit/daily_acceptance")
    req_audit.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req_audit, timeout=3.0) as resp_a:
        assert resp_a.status == 200
        audit_data = json.loads(resp_a.read().decode())
        assert audit_data["success"] is True
        assert audit_data["data"]["target_consecutive_days"] == 5
        assert "IN_PROGRESS" in audit_data["data"]["acceptance_status"] or audit_data["data"]["is_completed"] is True
