"""Next-session entry risk boundaries, independent of matching and accounts."""
import unittest

from strategy_rules import (MomentumConfig, entry_guard_reason,
                            compute_risk_sizing_row, format_position_sizing_markdown_table)


class EntryGuardTests(unittest.TestCase):
    def test_vacancy_refill_is_enabled_by_default_and_strictly_boolean(self):
        self.assertIs(MomentumConfig().refill_vacancies, True)
        self.assertIs(MomentumConfig(refill_vacancies=False).refill_vacancies, False)
        for value in (0, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MomentumConfig(refill_vacancies=value)

    def test_stop_loss_mode_is_explicit_and_defaults_to_intraday(self):
        self.assertEqual(MomentumConfig().stop_loss_mode, "intraday")
        self.assertEqual(MomentumConfig(stop_loss_mode="daily_close").stop_loss_mode, "daily_close")
        for mode in ("", "realtime", None, True):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                MomentumConfig(stop_loss_mode=mode)

    def setUp(self):
        self.config = MomentumConfig()
        self.signal = {"as_of": "2026-10-13", "raw_signal_price": 10.0,
                       "adjustment_factor": 1.0, "ma20": 9.0}

    def guard(self, **values):
        args = dict(signal=self.signal, config=self.config, pre_close=10.0,
                    open_price=10.0, execution_price=10.0)
        args.update(values)
        return entry_guard_reason(**args)

    def test_ordinary_next_day_entry_passes(self):
        self.assertIsNone(self.guard())

    def test_longban_limit_down_case(self):
        signal = dict(self.signal, raw_signal_price=19.60, ma20=18.0)
        self.assertEqual(self.guard(signal=signal, pre_close=19.60, open_price=17.64,
                                    execution_price=17.64), "entry_limit_down")

    def test_lower_limit_is_always_active_when_optional_guards_disabled(self):
        self.config = MomentumConfig(max_entry_gap_down_pct=0, max_entry_gap_up_pct=0,
                                     max_entry_intraday_drop_pct=0, entry_require_ma20=False)
        self.assertEqual(self.guard(open_price=9, execution_price=9), "entry_limit_down")
        self.assertEqual(self.guard(execution_price=8.99), "entry_limit_down")
        self.assertIsNone(self.guard(open_price=9.01, execution_price=9.01))

    def test_limit_rounding_is_decimal_half_up(self):
        self.signal.update(raw_signal_price=10.05, ma20=8)
        self.assertEqual(self.guard(pre_close=10.05, open_price=9.05,
                                    execution_price=9.05), "entry_limit_down")

    def test_explicit_quote_limit_supports_special_price_limits(self):
        self.assertEqual(self.guard(open_price=9.5, execution_price=9.5, limit_down=9.5),
                         "entry_limit_down")
        self.config.max_entry_gap_down_pct = 0
        self.assertIsNone(self.guard(open_price=9.01, execution_price=9.01, limit_down=8))

    def test_open_gap_down_boundary(self):
        self.assertIsNone(self.guard(open_price=9.7, execution_price=9.7))
        self.assertEqual(self.guard(open_price=9.699, execution_price=9.699), "entry_gap_down")
        # An initial bad gap remains disallowed even after a rebound.
        self.assertEqual(self.guard(open_price=9.6, execution_price=10), "entry_gap_down")

    def test_gap_up_and_chase_boundary(self):
        self.assertIsNone(self.guard(open_price=10.5, execution_price=10.5))
        self.assertEqual(self.guard(open_price=10.501, execution_price=10.501), "entry_gap_up")
        self.assertEqual(self.guard(open_price=10, execution_price=10.501), "entry_gap_up")

    def test_intraday_drop_uses_open_only(self):
        self.assertIsNone(self.guard(execution_price=9.7))
        self.assertEqual(self.guard(execution_price=9.699), "entry_intraday_drop")

    def test_individual_zero_thresholds_disable_only_that_check(self):
        self.config.max_entry_gap_down_pct = 0
        self.assertIsNone(self.guard(open_price=9.6, execution_price=9.6))
        self.config.max_entry_gap_up_pct = 0
        self.assertIsNone(self.guard(open_price=10.6, execution_price=10.6))
        self.config.max_entry_intraday_drop_pct = 0
        self.assertIsNone(self.guard(open_price=10, execution_price=9.6))

    def test_ma20_uses_yesterdays_adjustment_basis(self):
        self.signal.update(adjustment_factor=0.5, ma20=5)
        self.assertEqual(self.guard(), "entry_below_ma20")
        self.assertIsNone(self.guard(execution_price=10.01))
        self.config.entry_require_ma20 = False
        self.assertIsNone(self.guard(signal=dict(self.signal, ma20=None)))

    def test_reference_mismatch_blocks_unverified_corporate_action(self):
        self.assertIsNone(self.guard(pre_close=10.1, open_price=10.1, execution_price=10.1))
        self.assertEqual(self.guard(pre_close=10.101, open_price=10.101,
                                    execution_price=10.101), "entry_reference_mismatch")
        self.assertEqual(self.guard(pre_close=9.899, open_price=9.899,
                                    execution_price=9.899), "entry_reference_mismatch")

    def test_missing_invalid_price_evidence_fails_closed(self):
        for key in ("pre_close", "open_price", "execution_price", "limit_down"):
            for value in (0, -1, float("nan"), float("inf"), -float("inf"), True, False, "10"):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.guard(**{key: value}), "entry_unverified_prices")
        for key in ("raw_signal_price", "adjustment_factor", "ma20"):
            for value in (None, 0, -1, float("nan"), float("inf"), True, "10"):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.guard(signal=dict(self.signal, **{key: value})),
                                     "entry_unverified_prices")
        self.assertEqual(self.guard(signal=None), "entry_unverified_prices")

    def test_no_current_day_future_bar_values_or_mutations(self):
        signal = dict(self.signal, today_high=float("nan"), today_low=1,
                      today_close=0, today_volume=0)
        before = dict(signal)
        self.assertIsNone(self.guard(signal=signal))
        self.assertEqual(signal, before)
        # Changing a known execution quote changes the verdict without altering
        # yesterday's dated signal or requiring a completed current-day bar.
        self.assertEqual(self.guard(signal=signal, execution_price=9.6), "entry_intraday_drop")
        self.assertEqual(signal["as_of"], "2026-10-13")


class EntryConfigTests(unittest.TestCase):
    def test_threshold_validation(self):
        for key in ("max_entry_gap_down_pct", "max_entry_gap_up_pct", "max_entry_intraday_drop_pct"):
            for value in (None, -0.01, 20.01, True, False, "3", float("nan"), float("inf")):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        MomentumConfig.from_dict({key: value})
            for value in (0, 3, 20, 3.5):
                with self.subTest(key=key, value=value):
                    self.assertEqual(getattr(MomentumConfig.from_dict({key: value}), key), value)

    def test_ma20_bool_validation(self):
        for value in (None, 0, 1, "true", [], float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MomentumConfig(entry_require_ma20=value)
        self.assertTrue(MomentumConfig(entry_require_ma20=True).entry_require_ma20)
        self.assertFalse(MomentumConfig(entry_require_ma20=False).entry_require_ma20)


class PositionSizingPrecisionTests(unittest.TestCase):
    def test_compute_risk_sizing_row_and_markdown(self):
        config = MomentumConfig(max_positions=3, max_stock_weight=0.33, stop_loss_pct=-6.0,
                                risk_per_trade=0.01, volatility_target=0.25)
        # Qingdao Food test values
        row1 = compute_risk_sizing_row(
            symbol="sz001219", name="青岛食品", shares=600, cost_price=20.69,
            total_equity=96639.44, config=config, atr_pct=0.038329, volatility=0.4914,
            cost_basis=12414.0
        )
        self.assertEqual(row1["shares"], 600)
        self.assertEqual(row1["shares_str"], "600 股")
        self.assertEqual(row1["atr_pct_str"], "3.83%")
        self.assertEqual(row1["risk_distance_str"], "7.67%")
        self.assertEqual(row1["risk_weight_cap_str"], "13.04%")
        self.assertEqual(row1["cost_str"], "¥12,414")
        self.assertEqual(row1["actual_pct_str"], "12.8%")

        # PetroChina
        row2 = compute_risk_sizing_row(
            symbol="sh601857", name="中国石油", shares=1100, cost_price=13.85,
            total_equity=96639.44, config=config, atr_pct=0.0306, volatility=0.2629,
            cost_basis=15235.0
        )
        self.assertEqual(row2["atr_pct_str"], "3.06%")
        self.assertEqual(row2["risk_distance_str"], "6.12%")
        self.assertEqual(row2["risk_weight_cap_str"], "16.34%")

        # Yixintang
        row3 = compute_risk_sizing_row(
            symbol="sz002727", name="一心堂", shares=800, cost_price=13.30375,
            total_equity=96639.44, config=config, atr_pct=0.0439, volatility=0.3623,
            cost_basis=10643.0
        )
        self.assertEqual(row3["atr_pct_str"], "4.39%")
        self.assertEqual(row3["risk_distance_str"], "8.78%")
        self.assertEqual(row3["risk_weight_cap_str"], "11.39%")

        # Markdown table
        table = format_position_sizing_markdown_table([row1, row2, row3], total_weight_cap=0.4077,
                                                      total_cost=38292.0, total_actual_pct=40.1)
        self.assertIn("青岛食品 (sz001219)", table)
        self.assertIn("中国石油 (sh601857)", table)
        self.assertIn("一心堂 (sz002727)", table)
        self.assertIn("40.77%", table)
        self.assertIn("¥38,292", table)
        self.assertIn("~40.1%", table)


if __name__ == "__main__":
    unittest.main()
