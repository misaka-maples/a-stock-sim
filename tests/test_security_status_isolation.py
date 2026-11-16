"""A dated security exception is not missing market data for all accounts."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

from src.astock.data.security_status import load_nontrading_statuses
from src.astock.data.scheduler import DataScheduler
from src.astock.domain.enums import DataQuality
from test_data_sync_reliability import bar, fake_service, service


def evidence(tmp_path, **overrides):
    entry = dict(symbol="sh600000", status="SUSPENDED", effective_from="2026-11-13",
                 announcement_date="2026-11-12", verified_through="2026-11-16",
                 source_url="https://static.cninfo.com.cn/finalpage/2026-11-12/example.PDF",
                 title="Company suspension announcement", evidence_summary="Suspended on target session")
    entry.update(overrides)
    path = tmp_path / "security_status.json"
    path.write_text(json.dumps({"version": 1, "records": [entry]}))
    return path


def test_suspension_requires_dated_evidence_and_expires(tmp_path):
    path = evidence(tmp_path)
    assert load_nontrading_statuses(path, "2026-11-16", ["sh600000"])
    assert not load_nontrading_statuses(path, "2026-11-11")
    assert not load_nontrading_statuses(path, "2026-11-17")
    assert not load_nontrading_statuses(path, "2026-11-16", ["sh600001"])


def test_definitive_delisting_persists_but_never_uses_future_announcement(tmp_path):
    path = evidence(tmp_path, status="DELISTED", announcement_date="2026-11-16")
    assert not load_nontrading_statuses(path, "2026-11-13")
    assert load_nontrading_statuses(path, "2026-11-17")


def test_resumed_unknown_and_unsupported_sources_cannot_waive_missing_data(tmp_path):
    for changes in ({"status": "RESUMED"}, {"status": "UNKNOWN"},
                    {"source_url": "https://example.com/announcement"},
                    {"source_url": "https://cninfo.com.cn.example.com/x"},
                    {"announcement_date": "bad"}):
        assert not load_nontrading_statuses(evidence(tmp_path, **changes), "2026-11-16")


def test_verified_nontrading_symbol_is_not_fetched_or_given_fake_bar(tmp_path):
    svc, providers = service(tmp_path, [[bar("sh600001", "2026-11-16")]])
    status = {"sh600000": {"status": "SUSPENDED"}}
    svc.get_security_statuses = Mock(return_value=status)
    result = svc.sync_daily_increment("2026-11-16", ["sh600000", "sh600001"])
    assert result["status"] == "COMPLETED"
    assert result["missing_symbols"] == []
    assert result["excluded_symbols"] == ["sh600000"]
    assert result["coverage"] == .5  # Raw all-pool coverage remains truthful.
    assert providers.fetch_stock_daily.call_args.args[0] == "sh600001"
    assert not svc.storage.read_daily_bars("sh600000")


def test_partial_but_usable_pipeline_publishes_once_and_keeps_retrying(monkeypatch):
    monkeypatch.setattr("src.astock.data.scheduler.time.monotonic", lambda: 1000.)
    svc, snap = fake_service()
    snap.source_lineage = {"excluded_symbols": ["sh600001"]}
    svc.sync_daily_increment.return_value = {
        "status": "PARTIAL", "coverage": .999, "fail_count": 1,
        "missing_symbols": ["sh600001"], "excluded_symbols": ["sh600001"]}
    callback = Mock()
    scheduler = DataScheduler(svc, on_pipeline_completed=callback)
    for _ in range(2):
        result = scheduler.run_daily_pipeline("2026-11-16")
        assert result["status"] == "PARTIAL"
        assert result["trading_ready"] is True
        assert result["data_complete"] is False and result["quality_passed"] is False
        assert scheduler.last_sync_date is None
        assert scheduler.last_usable_date == "2026-11-16"
        assert scheduler.latest_snapshot is snap
        assert scheduler.get_status()["next_retry_seconds"] > 0
    callback.assert_called_once_with("2026-11-16", snap)


def test_bad_benchmark_or_low_coverage_still_blocks_shared_snapshot():
    for coverage, quality, benchmark in ((.69, DataQuality.VALID, True),
                                         (.999, DataQuality.INVALID, True),
                                         (.999, DataQuality.VALID, False)):
        svc, snap = fake_service()
        svc.sync_daily_increment.return_value = {"status": "PARTIAL", "coverage": coverage}
        snap.quality = quality
        if not benchmark:
            snap.benchmark_bars = []
        scheduler = DataScheduler(svc)
        result = scheduler.run_daily_pipeline("2026-11-16")
        assert not result["trading_ready"]
        assert scheduler.latest_snapshot is None
