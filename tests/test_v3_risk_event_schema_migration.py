"""Regression coverage for an existing V3 ledger, using temporary databases only."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.astock.trade.ledger.sqlite_ledger import SQLiteAccountLedger
from src.astock.trade.models import RiskAuditResult, RiskLayer, RiskSeverity


def _legacy_database(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE risk_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_id TEXT UNIQUE,
                account_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                layer TEXT NOT NULL,
                rule_name TEXT NOT NULL,
                severity TEXT NOT NULL,
                action_taken TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT
            );
            INSERT INTO risk_events
                (audit_id, account_id, timestamp, layer, rule_name, severity,
                 action_taken, message, details_json)
            VALUES ('historical', 'balanced', '2026-11-16T10:00:00', 'MARKET',
                    'LEGACY', 'INFO', 'NORMAL', 'retained event', '{"old":true}');
        """)


def _event(passed):
    return RiskAuditResult(
        audit_id=f"new-{passed}", timestamp="2026-11-16T15:00:00",
        account_id="balanced", layer=RiskLayer.MARKET,
        rule_name="REGRESSION", passed=passed, severity=RiskSeverity.INFO,
        action_taken="ALLOW_ENTRY" if passed else "BLOCK_NEW_BUYS",
        message="new audit outcome", details={"passed": passed},
    )


@pytest.mark.parametrize("passed", [True, False])
def test_legacy_schema_upgrade_preserves_account_and_audit_history(tmp_path, passed):
    path = tmp_path / "legacy.sqlite"
    _legacy_database(path)
    ledger = SQLiteAccountLedger(str(path))
    ledger.init_account("balanced", initial_cash=12345.67)
    with ledger.connect() as conn:
        account_before = dict(conn.execute("SELECT * FROM accounts").fetchone())
        cash_before = [tuple(r) for r in conn.execute("SELECT * FROM cash_journal")]

    # Repeated initialization must neither alter balances nor reset audit history.
    ledger = SQLiteAccountLedger(str(path))
    ledger.record_risk_event(_event(passed))
    with ledger.connect() as conn:
        assert dict(conn.execute("SELECT * FROM accounts").fetchone()) == account_before
        assert [tuple(r) for r in conn.execute("SELECT * FROM cash_journal")] == cash_before
        old = conn.execute("SELECT * FROM risk_events WHERE audit_id='historical'").fetchone()
        assert old["message"] == "retained event"
        assert old["details_json"] == '{"old":true}'
        assert old["passed"] is None  # No historical result may be fabricated.
        new = conn.execute("SELECT passed FROM risk_events WHERE audit_id=?", (f"new-{passed}",)).fetchone()
        assert new["passed"] == int(passed)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_initializers_upgrade_once(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    _legacy_database(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: SQLiteAccountLedger(str(path)), range(8)))
    with sqlite3.connect(path) as conn:
        names = [row[1] for row in conn.execute("PRAGMA table_info(risk_events)")]
        assert names.count("passed") == 1
        assert conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] == 1


def test_new_database_retains_required_audit_outcome(tmp_path):
    ledger = SQLiteAccountLedger(str(tmp_path / "new.sqlite"))
    ledger.record_risk_event(_event(True))
    with ledger.connect() as conn:
        column = next(r for r in conn.execute("PRAGMA table_info(risk_events)") if r["name"] == "passed")
        assert column["notnull"] == 1
        assert conn.execute("SELECT passed FROM risk_events").fetchone()[0] == 1
