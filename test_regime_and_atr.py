import unittest
from strategy_rules import MomentumConfig
from position_guardian import compute_live_atr, evaluate_position_realtime_rules

class TestRegimeAndATR(unittest.TestCase):
    def test_config_validation(self):
        # Valid config
        cfg = MomentumConfig(
            trailing_stop_mode="atr", atr_trailing_multiplier=2.5,
            annual_cash_yield=2.0, regime_sizing_enabled=True,
            regime_neutral_positions=3, regime_neutral_risk=0.015, regime_neutral_weight=0.3
        )
        self.assertEqual(cfg.trailing_stop_mode, "atr")
        self.assertEqual(cfg.atr_trailing_multiplier, 2.5)
        self.assertEqual(cfg.annual_cash_yield, 2.0)
        self.assertTrue(cfg.regime_sizing_enabled)

        # Invalid trailing_stop_mode
        with self.assertRaises(ValueError):
            MomentumConfig(trailing_stop_mode="invalid_mode")

        # Invalid atr_trailing_multiplier
        with self.assertRaises(ValueError):
            MomentumConfig(atr_trailing_multiplier=-1.0)
        with self.assertRaises(ValueError):
            MomentumConfig(atr_trailing_multiplier=50.0)

        # Invalid annual_cash_yield
        with self.assertRaises(ValueError):
            MomentumConfig(annual_cash_yield=-0.5)
        with self.assertRaises(ValueError):
            MomentumConfig(annual_cash_yield=30.0)

        # Invalid regime positions
        with self.assertRaises(ValueError):
            MomentumConfig(regime_neutral_positions=0)

    def test_compute_live_atr(self):
        bars = [
            {"date": f"2026-01-{i:02d}", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5}
            for i in range(1, 25)
        ]
        history_cache = {"sh600000": {"bars": bars}}
        atr = compute_live_atr("sh600000", history_cache, period=14)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0.0)

    def test_position_guardian_atr_mode(self):
        bars = [
            {"date": f"2026-01-{i:02d}", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2}
            for i in range(1, 25)
        ]
        history_cache = {"sh600000": {"bars": bars}}
        cfg = MomentumConfig(
            trailing_stop_enabled=True,
            trailing_stop_mode="atr",
            trailing_stop_activation_pct=10.0,
            atr_trailing_multiplier=2.0
        )
        pos = {"cost_price": 10.0, "total_shares": 1000}
        monitor = {"peak_price": 12.0, "peak_profit_pct": 20.0}

        # Case 1: At 10.80, peak 12.0, ATR is ~0.70 (5.8%), drop threshold is ~11.6% (~10.60 stop price)
        # 10.80 > stop price -> Armed
        exit_order, info = evaluate_position_realtime_rules(
            symbol="sh600000",
            pos=pos,
            quote={"trade_price": 10.8},
            history_cache=history_cache,
            config=cfg,
            monitor=monitor,
            available_shares=1000,
            validated_price=10.8
        )
        self.assertTrue(info["trailing_stop_active"])
        self.assertEqual(info["status"], "trailing_stop_armed")

        # Case 2: At 10.30, price dropped below trailing stop price (<= 10.60) -> Triggered
        exit_order, info = evaluate_position_realtime_rules(
            symbol="sh600000",
            pos=pos,
            quote={"trade_price": 10.3},
            history_cache=history_cache,
            config=cfg,
            monitor=monitor,
            available_shares=1000,
            validated_price=10.3
        )
        self.assertIsNotNone(exit_order)
        self.assertEqual(info["status"], "triggered_trailing_stop")

if __name__ == "__main__":
    unittest.main()
