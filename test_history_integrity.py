"""Offline contract tests: no HTTP calls or trading actions."""

import datetime as dt
import json
from unittest.mock import Mock

import pytest
import requests

import history_data as history
from history_data import HistoricalDataFeed, last_completed_trading_day, previous_trading_day


def row(date, close=10, volume=1000):
    return {"date": date, "open": close, "close": close, "high": close + 1,
            "low": close - 1, "volume": volume, "amount": close * volume * 100,
            "change_amount": 0}


def raw_bar(date, close=10):
    bar = row(date, close)
    for key in ("open", "close", "high", "low"):
        bar[f"raw_{key}"] = bar[key] * 2
    bar["adjustment_factor"] = 0.5
    bar["raw_pre_close"] = close * 2
    return bar


@pytest.fixture
def feed(tmp_path):
    instance = HistoricalDataFeed(tmp_path)
    instance.industry_file = tmp_path / "industry_map_manual.json"
    # A network call from these tests is always an error.
    instance.session.get = Mock(side_effect=AssertionError("offline tests must not use network"))
    return instance


def mock_download(feed, bars):
    feed._download = Mock(return_value=("测试股票", bars, "test_provider", []))


def test_completed_date_is_beijing_and_waits_for_settlement_buffer():
    assert last_completed_trading_day(dt.datetime(2026, 10, 13, 15, 4, 59)) == "2026-10-12"
    assert last_completed_trading_day(dt.datetime(2026, 10, 13, 15, 5)) == "2026-10-13"
    # 07:05 UTC is the same boundary in Beijing; do not use the host timezone.
    assert last_completed_trading_day(dt.datetime(2026, 10, 13, 7, 5, tzinfo=dt.timezone.utc)) == "2026-10-13"
    assert last_completed_trading_day(dt.datetime(2026, 10, 13, 7, 4, tzinfo=dt.timezone.utc)) == "2026-10-12"


def test_date_helpers_skip_holiday_and_weekend():
    assert previous_trading_day("2026-10-08") == "2026-09-30"
    assert last_completed_trading_day(dt.datetime(2026, 10, 5, 20)) == "2026-09-30"
    assert last_completed_trading_day(dt.datetime(2026, 10, 11, 20)) == "2026-10-09"
    assert previous_trading_day("2026-10-12") == "2026-10-09"


def test_dynamic_end_and_requested_start_passed_to_provider(feed, monkeypatch):
    monkeypatch.setattr(history, "last_completed_trading_day", lambda: "2026-10-13")
    mock_download(feed, [raw_bar("2026-10-12"), raw_bar("2026-10-13")])
    _, _, bars = feed.fetch_stock_kline("600519", "2026-10-12")
    assert [b["date"] for b in bars] == ["2026-10-12", "2026-10-13"]
    feed._download.assert_called_once_with("sh600519", "2026-10-09", "2026-10-13", "qfq")
    assert feed.metadata["sh600519"]["requested_start"] == "2026-10-12"
    assert feed.metadata["sh600519"]["requested_end"] == "2026-10-13"


def test_stable_cache_reuses_complete_range_and_updates_without_daily_files(feed):
    mock_download(feed, [raw_bar("2026-10-12"), raw_bar("2026-10-13")])
    first = feed.fetch_stock_kline("sh600519", "2026-10-12", "2026-10-13")
    second = feed.fetch_stock_kline("sh600519", "2026-10-13", "2026-10-13")
    assert second[2] == first[2][-1:]
    assert feed._download.call_count == 1
    mock_download(feed, first[2] + [raw_bar("2026-10-14")])
    feed.fetch_stock_kline("sh600519", "2026-10-12", "2026-10-14")
    assert feed._download.call_count == 1
    assert [p.name for p in feed.cache_dir.glob("*.json")] == ["sh600519_qfq.json"]
    data = json.loads((feed.cache_dir / "sh600519_qfq.json").read_text())
    assert data["schema_version"] == 2
    assert data["metadata"]["volume_unit"] == "lots_100_shares"
    assert not list(feed.cache_dir.glob("*.tmp"))


def test_expired_complete_cache_is_refetched(feed):
    mock_download(feed, [raw_bar("2026-10-13")])
    feed.fetch_stock_kline("sh600519", "2026-10-13", "2026-10-13")
    path = feed._get_cache_path("sh600519")
    stored = json.loads(path.read_text())
    stored["metadata"]["fetched_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
    path.write_text(json.dumps(stored))
    feed.fetch_stock_kline("sh600519", "2026-10-13", "2026-10-13")
    assert feed._download.call_count == 2


def test_old_date_named_cache_is_never_accepted_as_fresh(feed):
    legacy_path = feed.cache_dir / "sh600519_2026-10-13_2026-10-13.json"
    legacy_path.write_text(json.dumps({"name": "旧缓存", "bars": [row("2026-10-13")]}))
    mock_download(feed, [])
    _, name, bars = feed.fetch_stock_kline("sh600519", "2026-10-13", "2026-10-13")
    assert name == "旧缓存" and bars
    metadata = feed.metadata["sh600519"]
    assert not metadata["fresh"]
    assert not metadata["raw_prices_available"]
    assert metadata["source"] == "legacy_cache"
    assert metadata["error"] == "no_valid_bars_for_requested_range"
    assert feed._download.call_count == 1


def test_old_missing_sessions_stay_visible_without_refetching_fresh_latest_data(feed):
    mock_download(feed, [raw_bar("2026-10-12"), raw_bar("2026-10-14")])
    result = feed.fetch_universe_klines(["sh600519"], "2026-10-12", "2026-10-14")
    metadata = result["sh600519"]["metadata"]
    assert metadata["coverage"]["missing_trading_dates"] == ["2026-10-13"]
    assert not metadata["coverage_complete"]
    assert "2026-10-13" not in result["sh600519"]["by_date"]
    feed.fetch_stock_kline("sh600519", "2026-10-12", "2026-10-14")
    assert feed._download.call_count == 1


def test_partial_cache_with_missing_latest_session_still_requires_repair(feed):
    mock_download(feed, [raw_bar("2026-10-12")])
    feed.fetch_stock_kline("sh600519", "2026-10-12", "2026-10-13")
    feed.fetch_stock_kline("sh600519", "2026-10-12", "2026-10-13")
    assert feed._download.call_count == 2
    assert feed.metadata["sh600519"]["coverage"]["end"] == "2026-10-12"


def test_cache_only_peek_is_network_free_and_requires_freshness_evidence(feed):
    mock_download(feed, [raw_bar("2026-10-13")])
    feed.fetch_stock_kline("sh600519", "2026-10-13", "2026-10-13")
    result = feed.peek_universe(["sh600519", "sh600001"], "2026-10-13", "2026-10-13")
    assert result["sh600519"]["metadata"]["cache_hit"]
    assert not result["sh600001"]["metadata"]["fresh"]
    path = feed._get_cache_path("sh600519")
    stored = json.loads(path.read_text())
    stored["metadata"].pop("fetched_at")
    path.write_text(json.dumps(stored))
    unverified = feed.peek_universe(["sh600519"], "2026-10-13", "2026-10-13")["sh600519"]
    assert unverified["bars"] and not unverified["metadata"]["fresh"]
    assert unverified["error"] == "cache_metadata_unverified"
    assert feed._download.call_count == 1
    feed.session.get.assert_not_called()


def test_cache_only_benchmark_and_industry_miss_never_request_or_write(feed):
    before = set(feed.cache_dir.iterdir())
    assert feed.peek_benchmark_kline("000300", "2026-10-13", "2026-10-13") == []
    assert feed.metadata["idx_000300"]["fresh"] is False
    assert feed.peek_industry_map() == {}
    assert set(feed.cache_dir.iterdir()) == before
    feed.session.get.assert_not_called()


def test_validation_sorts_deduplicates_and_rejects_bad_bars():
    bad_high = row("2026-10-15")
    bad_high["high"] = 1
    negative_volume = row("2026-10-16", volume=-1)
    bars, rejected, duplicates = HistoricalDataFeed._validate_bars([
        row("2026-10-14"), row("2026-10-13"), row("2026-10-13", close=12),
        row("2026-10-12", close=float("nan")), bad_high, negative_volume,
        row("invalid-date"), row("2026-10-17", volume=0),
    ])
    assert [b["date"] for b in bars] == ["2026-10-13", "2026-10-14", "2026-10-17"]
    assert bars[0]["close"] == 12
    assert rejected == 4 and duplicates == 1


def test_original_and_qfq_prices_align_by_date_and_do_not_use_adjusted_for_execution(feed):
    adjusted = [row("2026-10-13", 5), row("2026-10-12", 4)]
    original = [row("2026-10-12", 8), row("2026-10-13", 10)]
    original[1]["change_amount"] = 2
    feed._eastmoney = Mock(side_effect=[("测试", adjusted), ("测试", original)])
    _, _, bars = feed.fetch_stock_kline("600519", "2026-10-12", "2026-10-13", False)
    assert [b["date"] for b in bars] == ["2026-10-12", "2026-10-13"]
    assert bars[1]["close"] == 5 and bars[1]["raw_close"] == 10
    assert bars[1]["raw_open"] == 10 and bars[1]["adjustment_factor"] == 0.5
    assert bars[1]["raw_pre_close"] == 8
    assert feed.metadata["sh600519"]["raw_prices_available"]


def test_missing_original_prices_remain_explicit_and_are_not_cached_as_tradeable(feed):
    feed._eastmoney = Mock(side_effect=[("测试", [row("2026-10-13")]), ("测试", [])])
    feed._tencent = Mock(return_value=("测试", [], ["raw_source_unavailable"]))
    _, _, bars = feed.fetch_stock_kline("600519", "2026-10-13", "2026-10-13")
    assert "raw_close" not in bars[0]
    assert not feed.metadata["sh600519"]["raw_prices_available"]
    assert any("execution_prices_missing" in w for w in feed.metadata["sh600519"]["warning"])


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_eastmoney_real_start_end_and_original_adjustment_request(feed):
    feed.session.get = Mock(return_value=Response({"data": {"name": "测试", "klines": [
        "2020-01-02,10,11,12,9,123,135300,30,10,1,0.2"]}}))
    name, bars = feed._eastmoney("sh600519", "2018-01-01", "2026-10-13", "none")
    kwargs = feed.session.get.call_args.kwargs
    assert kwargs["params"]["beg"] == "20180101"
    assert kwargs["params"]["end"] == "20261013"
    assert kwargs["params"]["fqt"] == 0
    assert kwargs["timeout"] == 6
    assert name == "测试" and bars[0]["volume"] == 123


def test_tencent_fallback_is_paginated_by_year_and_does_not_call_latest_only(feed):
    def fake_get(url, params, timeout):
        symbol, period, begin, finish, count, adjustment = params["param"].split(",")
        assert symbol == "sh600519" and period == "day"
        assert begin and finish and begin[:4] == finish[:4]
        assert count == "640" and adjustment == "qfq"
        return Response({"data": {symbol: {"qfqday": [[begin, "10", "10", "11", "9", "100"]]}}})
    feed.session.get = Mock(side_effect=fake_get)
    _, bars, errors = feed._tencent("sh600519", "2018-06-01", "2020-10-13", "qfq")
    assert [b["date"] for b in bars] == ["2018-06-01", "2019-01-01", "2020-01-01"]
    assert feed.session.get.call_count == 3 and not errors


def test_tencent_raw_day_never_silently_substitutes_for_qfq(feed):
    feed.session.get = Mock(return_value=Response({"data": {"sh600519": {
        "day": [["2026-10-13", "10", "10", "11", "9", "100"]]}}}))
    _, bars, _ = feed._tencent("sh600519", "2026-10-13", "2026-10-13", "qfq")
    assert bars == []


def test_failed_download_is_present_with_error_and_empty_bars(feed):
    feed.session.get = Mock(side_effect=requests.Timeout("mocked timeout"))
    result = feed.fetch_universe_klines(["sh600519", "600519"], "2026-10-13", "2026-10-13")
    assert list(result) == ["sh600519"]
    item = result["sh600519"]
    assert item["bars"] == [] and item["by_date"] == {}
    assert item["error"] == "no_valid_bars_for_requested_range"
    assert not item["metadata"]["coverage_complete"]
    assert "eastmoney_adjusted_Timeout" in item["metadata"]["warning"]


def test_benchmark_retains_list_interface_and_metadata_has_no_adjustment(feed):
    mock_download(feed, [raw_bar("2026-10-13")])
    bars = feed.fetch_benchmark_kline("000300", "2026-10-13", "2026-10-13")
    assert bars[0]["date"] == "2026-10-13"
    assert feed.metadata["idx_000300"]["adjustment"] == "none"
    assert (feed.cache_dir / "idx_000300_none.json").exists()


def test_invalid_request_range_is_an_explicit_error(feed):
    with pytest.raises(ValueError, match="start_date"):
        feed.fetch_stock_kline("600519", "2026-10-14", "2026-10-13")


def test_partial_refresh_does_not_erase_wider_cache(feed):
    mock_download(feed, [raw_bar("2026-10-12"), raw_bar("2026-10-13")])
    feed.fetch_stock_kline("sh600519", "2026-10-12", "2026-10-13")
    mock_download(feed, [raw_bar("2026-10-14")])
    result = feed.fetch_universe_klines(["sh600519"], "2026-10-12", "2026-10-14")
    assert [b["date"] for b in result["sh600519"]["bars"]] == ["2026-10-14"]
    assert "partial_refresh_retained_wider_cache" in result["sh600519"]["metadata"]["warning"]
    cached = json.loads(feed._get_cache_path("sh600519").read_text())
    assert [b["date"] for b in cached["bars"]] == ["2026-10-12", "2026-10-13"]


def test_reference_close_comes_from_raw_timeline_even_if_adjusted_bar_missing(feed):
    responses = [("测试", [row("2026-10-12", 4), row("2026-10-14", 6)]),
                 ("测试", [row("2026-10-12", 8), row("2026-10-13", 10), row("2026-10-14", 12)])]
    # Simulate a provider without exchange reference change fields.
    for _, bars in responses:
        for bar in bars:
            bar.pop("change_amount", None)
    feed._eastmoney = Mock(side_effect=responses)
    _, _, bars = feed.fetch_stock_kline("sh600519", "2026-10-12", "2026-10-14", False)
    assert bars[-1]["raw_pre_close"] == 10


def test_symbol_is_validated_before_any_io(feed):
    with pytest.raises(ValueError, match="six digits"):
        feed.fetch_stock_kline("../../unsafe", "2026-10-13", "2026-10-13")
    feed.session.get.assert_not_called()


def industry_row(code="600519", market=1, industry="酿酒行业"):
    return {"f12": code, "f13": market, "f14": "测试股票", "f100": industry}


def test_industry_manual_values_win_and_file_is_never_modified(feed):
    supplied = {"industries": {"sh600519": "人工消费行业"}, "source": "manual"}
    original_text = json.dumps(supplied)
    feed.industry_file.write_text(original_text)
    feed.session.get = Mock(return_value=Response({"data": {"total": 2, "diff": [
        industry_row(), industry_row("000001", 0, "银行")]}}))
    output = feed.fetch_industry_map()
    assert output == {"sh600519": "人工消费行业", "sz000001": "银行"}
    assert feed.industry_file.read_text() == original_text
    stored = json.loads((feed.cache_dir / "industry_map_eastmoney.json").read_text())
    assert stored["industries"]["sh600519"] == "酿酒行业"
    assert feed.metadata["industry_map"]["manual_symbols"] == 1
    assert feed.metadata["industry_map"]["coverage_complete"]


def test_industry_seven_day_cache_reused_and_manual_overlay_reread(feed):
    feed.session.get = Mock(return_value=Response({"data": {"total": 1, "diff": [industry_row()]}}))
    first = feed.fetch_industry_map()
    feed.industry_file.write_text(json.dumps({"industries": {"sh600519": "新人工分类"}, "source": "manual"}))
    second = feed.fetch_industry_map()
    assert first["sh600519"] == "酿酒行业" and second["sh600519"] == "新人工分类"
    assert feed.session.get.call_count == 1
    assert feed.metadata["industry_map"]["cache_hit"]
    path = feed.cache_dir / "industry_map_eastmoney.json"
    stored = json.loads(path.read_text())
    stored["metadata"]["fetched_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
    path.write_text(json.dumps(stored))
    feed.fetch_industry_map()
    assert feed.session.get.call_count == 2


def test_industry_pagination_explicit_parameters_and_bounded_pages(feed):
    def fake_get(url, params, timeout):
        assert url == "https://push2.eastmoney.com/api/qt/clist/get"
        assert params["fs"] == "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:13"
        assert params["fields"] == "f12,f13,f14,f100"
        assert params["pz"] == 100 and params["pn"] <= 50 and timeout <= 10
        return Response({"data": {"total": 10000, "diff": [industry_row(str(600000 + params["pn"]))]}})
    feed.session.get = Mock(side_effect=fake_get)
    output = feed.fetch_industry_map(False)
    assert len(output) == 50 and feed.session.get.call_count == 50
    status = feed.metadata["industry_map"]
    assert not status["fresh"] and not status["coverage_complete"]
    assert status["coverage"]["truncated"]
    assert status["coverage"]["reported_total"] == 10000
    assert status["coverage"]["valid_industry_symbols"] == 50


def test_industry_failure_uses_manual_and_stale_provider_cache(feed):
    feed.session.get = Mock(return_value=Response({"data": {"total": 1, "diff": [industry_row()]}}))
    feed.fetch_industry_map()
    path = feed.cache_dir / "industry_map_eastmoney.json"
    stored = json.loads(path.read_text())
    stored["metadata"]["fetched_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(stored))
    feed.industry_file.write_text(json.dumps({"industries": {"sz000001": "银行"}, "source": "manual"}))
    feed.session.get = Mock(side_effect=requests.Timeout("mocked timeout"))
    output = feed.fetch_industry_map()
    assert output == {"sh600519": "酿酒行业", "sz000001": "银行"}
    status = feed.metadata["industry_map"]
    assert not status["fresh"] and status["error"] == "industry_fetch_Timeout"
    assert "existing_industry_mapping_may_be_stale" in status["warning"]
    assert json.loads(path.read_text()) == stored


def test_industry_failure_without_mapping_returns_empty_with_status(feed):
    feed.session.get = Mock(side_effect=requests.Timeout("mocked timeout"))
    assert feed.fetch_industry_map() == {}
    status = feed.metadata["industry_map"]
    assert status["source"] == "unavailable" and status["error"] == "industry_fetch_Timeout"
    assert not status["fresh"]


def test_industry_unknown_rows_are_not_invented_and_incomplete_cache_is_retried(feed):
    feed.session.get = Mock(return_value=Response({"data": {"total": 2, "diff": {
        "0": industry_row(), "1": industry_row("000001", 0, "-")}}}))
    assert feed.fetch_industry_map() == {"sh600519": "酿酒行业"}
    assert not feed.metadata["industry_map"]["coverage_complete"]
    assert feed.metadata["industry_map"]["coverage"]["missing_industry_rows"] == 1
    feed.fetch_industry_map()
    assert feed.session.get.call_count == 2


def test_industry_repeated_page_is_not_reported_as_complete_coverage(feed):
    feed.session.get = Mock(return_value=Response({"data": {"total": 2, "diff": [industry_row()]}}))
    assert feed.fetch_industry_map() == {"sh600519": "酿酒行业"}
    status = feed.metadata["industry_map"]
    assert status["coverage"]["duplicate_rows"] == 1
    assert not status["coverage_complete"]


def test_industry_empty_page_reports_incomplete_coverage(feed):
    feed.session.get = Mock(return_value=Response({"data": {"total": 2, "diff": []}}))
    assert feed.fetch_industry_map() == {}
    status = feed.metadata["industry_map"]
    assert status["error"] == "industry_empty_page_before_reported_total"
    assert not status["coverage_complete"]


def test_industry_provider_cap_100_continues_until_actual_total_received(feed):
    data = [industry_row(str(600000 + i), 1, "行业" + str(i % 5)) for i in range(250)]
    def fake_get(url, params, timeout):
        # Reproduce the real server's hard cap, even if the client were to
        # accidentally request a larger pz again. No assumed 500-row progress.
        actual_size = min(params["pz"], 100)
        start = (params["pn"] - 1) * actual_size
        return Response({"data": {"total": len(data), "diff": data[start:start + actual_size]}})
    feed.session.get = Mock(side_effect=fake_get)
    output = feed.fetch_industry_map(False)
    assert len(output) == 250
    assert output["sh600249"] == "行业4"
    assert feed.session.get.call_count == 3
    status = feed.metadata["industry_map"]
    assert status["coverage"]["received_rows"] == 250
    assert status["coverage"]["reported_total"] == 250
    assert status["coverage_complete"] and not status["coverage"]["truncated"]


def test_industry_non_main_board_rows_are_counted_for_pagination_and_excluded(feed):
    feed.session.get = Mock(return_value=Response({"data": {"total": 3, "diff": [
        industry_row(), industry_row("688001", 1, "半导体"), industry_row("300750", 0, "电池")]}}))
    assert feed.fetch_industry_map() == {"sh600519": "酿酒行业"}
    status = feed.metadata["industry_map"]
    assert status["coverage"]["received_rows"] == 3
    assert status["coverage"]["excluded_non_main_board_rows"] == 2
    assert status["coverage"]["missing_industry_rows"] == 0
    assert status["coverage_complete"]
