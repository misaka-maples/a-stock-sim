"""Offline execution boundary tests: no market API or persisted accounts."""

import copy
import datetime as dt
import unittest

from execution import match_daily, match_quote, quote_key, quote_time_key
from trade_calendar import BEIJING


NOW = dt.datetime(2026, 10, 13, 10, 0, 0, tzinfo=BEIJING)


def quote(**changes):
    result = {
        "symbol": "sh600000", "name": "浦发银行", "time": "20261013100000",
        "current": 10, "pre_close": 10, "volume_lots": 5000, "fresh": True,
        "asks": [{"price": 10.01, "volume": 100}],
        "bids": [{"price": 9.99, "volume": 100}],
    }
    result.update(changes)
    return result


def bar(**changes):
    result = {
        "symbol": "sh600000", "name": "浦发银行", "date": "2026-10-13",
        "raw_open": 10, "raw_close": 10.1, "raw_high": 10.2, "raw_low": 9.8,
        "volume": 1000,
    }
    result.update(changes)
    return result


class QuoteExecutionTests(unittest.TestCase):
    def test_default_capacity_uses_visible_depth_and_lot_units(self):
        result = match_quote("BUY", 1000, quote(), now=NOW)
        self.assertTrue(result["success"])
        self.assertEqual(result["shares"], 500)
        self.assertEqual(result["capacity_shares"], 500)
        self.assertEqual(result["status"], "PARTIALLY_FILLED")
        self.assertEqual(result["price"], 10.01)

    def test_sweep_vwap_and_repeated_snapshot_cannot_reuse_depth(self):
        q = quote(asks=[{"price": 10.01, "volume": 2}, {"price": 10.02, "volume": 8}])
        first = match_quote("BUY", 500, q, now=NOW, participation_rate=1)
        second = match_quote("BUY", 500, q, now=NOW, participation_rate=1, consumed_shares=500)
        exhausted = match_quote("BUY", 100, q, now=NOW, participation_rate=1, consumed_shares=1000)
        self.assertAlmostEqual(first["price"], 10.016)
        self.assertEqual(second["price"], 10.02)
        self.assertEqual(first["quote_key"], second["quote_key"])
        self.assertFalse(exhausted["success"])

    def test_only_first_five_levels_can_trade(self):
        asks = [{"price": 10.01 + i / 100, "volume": 1} for i in range(5)]
        asks.append({"price": 10.06, "volume": 1000})
        # Decimal-friendly exact quoted prices, not binary-float arithmetic.
        for i, entry in enumerate(asks):
            entry["price"] = f"{10.01 + i / 100:.2f}"
        result = match_quote("BUY", 1000, quote(asks=asks), now=NOW, participation_rate=1)
        self.assertEqual(result["shares"], 500)
        self.assertAlmostEqual(result["price"], 10.03)

    def test_limit_orders_only_match_eligible_levels_and_keep_identity(self):
        q = quote(asks=[{"price": 10.01, "volume": 2}, {"price": 10.02, "volume": 8}])
        limited = match_quote("BUY", 500, q, now=NOW, participation_rate=1, limit_price=10.01)
        market = match_quote("BUY", 500, q, now=NOW, participation_rate=1)
        self.assertEqual(limited["shares"], 200)
        self.assertEqual(limited["price"], 10.01)
        self.assertEqual(limited["quote_key"], market["quote_key"])
        self.assertFalse(match_quote("BUY", 100, q, now=NOW, limit_price=10)["success"])
        self.assertFalse(match_quote("SELL", 100, quote(), now=NOW, limit_price=10)["success"])
        self.assertEqual(match_quote("SELL", 75, quote(), now=NOW, limit_price=9.99)["shares"], 75)

    def test_consumed_depth_is_skipped_before_limit_filter(self):
        q = quote(asks=[{"price": 10.01, "volume": 2}, {"price": 10.02, "volume": 8}])
        remaining = match_quote("BUY", 500, q, now=NOW, participation_rate=1,
                                consumed_shares=200, limit_price=10.02)
        exhausted_price = match_quote("BUY", 100, q, now=NOW, participation_rate=1,
                                      consumed_shares=200, limit_price=10.01)
        self.assertEqual(remaining["price"], 10.02)
        self.assertFalse(exhausted_price["success"])

    def test_timestamp_identity_survives_book_revisions(self):
        original = quote()
        revised = quote(asks=[{"price": 10.02, "volume": 200}])
        self.assertNotEqual(quote_key(original), quote_key(revised))
        self.assertEqual(quote_time_key(original), quote_time_key(revised))
        result = match_quote("BUY", 100, revised, now=NOW)
        self.assertEqual(result["snapshot_key"], quote_time_key(original))

    def test_iso_timezone_and_fresh_field_are_not_required_for_valid_time(self):
        q = quote(time="2026-10-13T02:00:00Z")
        q.pop("fresh")
        self.assertTrue(match_quote("BUY", 100, q, now=NOW)["success"])

    def test_bad_old_future_and_missing_time_are_rejected(self):
        for timestamp in (None, "2026-10-13", "invalid", "20261012095959", "20261013095759", "20261013100001"):
            with self.subTest(time=timestamp):
                result = match_quote("BUY", 100, quote(time=timestamp), now=NOW)
                self.assertFalse(result["success"])
        self.assertTrue(match_quote("BUY", 100, quote(time="20261013095800"), now=NOW)["success"])
        self.assertFalse(match_quote("BUY", 100, quote(fresh=False), now=NOW)["success"])

    def test_non_continuous_times_are_rejected(self):
        for value in ((2026, 10, 13, 9, 20), (2026, 10, 13, 12, 0),
                      (2026, 10, 13, 14, 57), (2026, 10, 17, 10, 0), (2026, 10, 5, 10, 0)):
            with self.subTest(now=value):
                now = dt.datetime(*value, tzinfo=BEIJING)
                q = quote(time=now.strftime("%Y%m%d%H%M%S"))
                self.assertFalse(match_quote("BUY", 100, q, now=now)["success"])

    def test_no_counterparty_zero_lots_and_suspension_reject(self):
        for changes in ({"asks": []}, {"asks": [{"price": 10.01, "volume": 0}]},
                        {"volume_lots": 0}, {"suspended": True}, {"is_suspended": True}):
            with self.subTest(changes=changes):
                self.assertFalse(match_quote("BUY", 100, quote(**changes), now=NOW)["success"])
        self.assertFalse(match_quote("SELL", 100, quote(bids=[]), now=NOW)["success"])

    def test_limit_board_is_not_blanket_blocked_when_counterparty_exists(self):
        up = quote(current=11, asks=[{"price": 11, "volume": 100}])
        down = quote(current=9, bids=[{"price": 9, "volume": 100}])
        self.assertTrue(match_quote("BUY", 100, up, now=NOW)["success"])
        self.assertTrue(match_quote("SELL", 100, down, now=NOW)["success"])
        self.assertFalse(match_quote("BUY", 100, {**up, "asks": []}, now=NOW)["success"])
        self.assertFalse(match_quote("SELL", 100, {**down, "bids": []}, now=NOW)["success"])

    def test_half_up_limit_rounding_and_explicit_metadata(self):
        q = quote(pre_close=2.15, current=2.37, asks=[{"price": 2.37, "volume": 100}])
        self.assertTrue(match_quote("BUY", 100, q, now=NOW)["success"])
        q["asks"][0]["price"] = 2.38
        self.assertFalse(match_quote("BUY", 100, q, now=NOW)["success"])
        q = quote(limit_up=10.5, limit_down=9.5, asks=[{"price": 10.51, "volume": 100}])
        self.assertFalse(match_quote("BUY", 100, q, now=NOW)["success"])

    def test_non_finite_and_invalid_market_inputs_reject(self):
        invalid = [{"current": float("nan")}, {"pre_close": float("inf")}, {"pre_close": 0},
                   {"symbol": "sz300750"}, {"symbol": "sh000001"}, {"name": "*ST测试"},
                   {"name": "退市测试"}, {"is_st": True},
                   {"asks": [{"price": float("nan"), "volume": 100}]},
                   {"asks": [{"price": 10.011, "volume": 100}]},
                   {"asks": [{"price": 10.01, "volume": -1}]}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertFalse(match_quote("BUY", 100, quote(**changes), now=NOW)["success"])

    def test_order_and_participation_boundaries(self):
        for changes in ({"side": "BAD"}, {"requested_shares": 0}, {"requested_shares": 150},
                        {"requested_shares": 100.5}, {"participation_rate": 0},
                        {"participation_rate": 1.1}, {"participation_rate": float("nan")},
                        {"consumed_shares": -1}, {"max_age_seconds": -1}):
            with self.subTest(changes=changes):
                params = {"side": "BUY", "requested_shares": 100, "quote": quote(), "now": NOW, **changes}
                self.assertFalse(match_quote(**params)["success"])
        self.assertEqual(match_quote("SELL", 75, quote(), now=NOW)["shares"], 75)

    def test_input_snapshot_not_mutated(self):
        original = quote()
        unchanged = copy.deepcopy(original)
        match_quote("BUY", 100, original, now=NOW, consumed_shares=100)
        self.assertEqual(original, unchanged)


class DailyExecutionTests(unittest.TestCase):
    def test_open_slippage_and_daily_aggregate_capacity(self):
        result = match_daily("BUY", 6000, bar(), 10)
        self.assertEqual(result["shares"], 5000)
        self.assertEqual(result["price"], 10.01)
        self.assertEqual(result["status"], "PARTIALLY_FILLED")
        self.assertIn("聚合", result["model"])
        remainder = match_daily("BUY", 1000, bar(), 10, consumed_shares=4500)
        self.assertEqual(remainder["shares"], 500)
        self.assertEqual(remainder["quote_key"], result["quote_key"])
        self.assertFalse(match_daily("BUY", 100, bar(), 10, consumed_shares=5000)["success"])

    def test_missing_unadjusted_ohlc_cannot_trade(self):
        b = {"symbol": "sh600000", "date": "2026-10-13", "open": 10, "close": 10.1,
             "high": 10.2, "low": 9.8, "volume": 1000}
        self.assertFalse(match_daily("BUY", 100, b, 10)["success"])
        self.assertFalse(match_daily("BUY", 100, {**b, "adjustment": "qfq"}, 10)["success"])
        self.assertTrue(match_daily("BUY", 100, {**b, "adjustment": "none"}, 10)["success"])
        self.assertFalse(match_daily("BUY", 100, {**b, "adjustment": "none", "raw_open": 10}, 10)["success"])

    def test_one_price_limit_boards_reject_both_sides(self):
        for price in (9, 11):
            for side in ("BUY", "SELL"):
                with self.subTest(price=price, side=side):
                    b = bar(raw_open=price, raw_close=price, raw_high=price, raw_low=price)
                    self.assertFalse(match_daily(side, 100, b, 10)["success"])

    def test_touching_limit_and_nonlimit_one_price_can_trade(self):
        b = bar(raw_open=10.9, raw_close=10.95, raw_high=11, raw_low=10.8)
        self.assertTrue(match_daily("BUY", 100, b, 10)["success"])
        flat = bar(raw_open=10, raw_close=10, raw_high=10, raw_low=10)
        self.assertTrue(match_daily("BUY", 100, flat, 10)["success"])

    def test_slippage_clips_to_ohlc_and_exchange_limits(self):
        b = bar(raw_open=10.99, raw_close=10.99, raw_high=11, raw_low=10.9)
        self.assertEqual(match_daily("BUY", 100, b, 10, slippage_rate=.02)["price"], 11)
        self.assertEqual(match_daily("SELL", 100, b, 10, slippage_rate=.02)["price"], 10.9)
        b = bar(raw_open=10, raw_close=10, raw_high=10.01, raw_low=9.99)
        self.assertEqual(match_daily("BUY", 100, b, 10, slippage_rate=.1)["price"], 10.01)
        self.assertEqual(match_daily("SELL", 100, b, 10, slippage_rate=.1)["price"], 9.99)

    def test_no_legal_cent_tick_or_bad_ohlc_rejects(self):
        invalid = [bar(raw_open=11.1, raw_close=11.1, raw_high=11.2, raw_low=11.0),
                   bar(raw_open=10.002, raw_close=10.002, raw_high=10.004, raw_low=10.001),
                   bar(raw_low=10.3), bar(raw_high=float("nan")), bar(raw_close=float("inf"))]
        for b in invalid:
            with self.subTest(bar=b):
                self.assertFalse(match_daily("BUY", 100, b, 10)["success"])

    def test_zero_volume_risk_warning_invalid_symbol_and_date(self):
        invalid = [{"volume": 0}, {"volume": -1}, {"suspended": True}, {"name": "ST测试"},
                   {"symbol": "sz300750"}, {"date": "2026-10-17"}, {"date": ""}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertFalse(match_daily("BUY", 100, bar(**changes), 10)["success"])
        self.assertFalse(match_daily("BUY", 100, bar(), 0)["success"])
        self.assertFalse(match_daily("BUY", 100, bar(), 10, slippage_rate=-.01)["success"])
        self.assertFalse(match_daily("BUY", 100, bar(), 10, slippage_rate=1)["success"])

    def test_odd_share_sale_and_snapshot_immutability(self):
        b = bar()
        unchanged = copy.deepcopy(b)
        result = match_daily("SELL", 75, b, 10)
        self.assertEqual(result["shares"], 75)
        self.assertEqual(result["price"], 9.99)
        self.assertEqual(b, unchanged)


if __name__ == "__main__":
    unittest.main()


def test_risk_warning_limit_version_uses_trade_date_and_dated_metadata():
    before = dt.datetime(2026, 6, 30, 10, tzinfo=BEIJING)
    after = dt.datetime(2026, 7, 6, 10, tzinfo=BEIJING)
    q = quote(name='ST测试', current=9.3, bids=[dict(price=9.3, volume=100)])
    q['time'] = before.strftime('%Y%m%d%H%M%S')
    assert not match_quote('SELL', 100, q, now=before)['success']
    q['time'] = after.strftime('%Y%m%d%H%M%S')
    assert match_quote('SELL', 100, q, now=after)['success']
    assert not match_quote('BUY', 100, q, now=after)['success']
    b = bar(name='ST测试', raw_open=9.3, raw_close=9.3, raw_low=9.2, raw_high=9.4)
    b['date'] = '2026-06-30'
    assert not match_daily('SELL', 100, b, 10)['success']
    b['date'] = '2026-07-06'
    assert match_daily('SELL', 100, b, 10)['success']
    b.update(date='2026-06-30', is_st=False)
    assert match_daily('BUY', 100, b, 10)['success']
    b.update(limit_down=9.5, limit_up=10.5)
    assert not match_daily('SELL', 100, b, 10)['success']
