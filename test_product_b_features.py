import unittest
from strategy_rules import MomentumConfig, target_weight

class TestProductBFeatures(unittest.TestCase):
    def test_config_validation(self):
        cfg = MomentumConfig(
            dynamic_universe_enabled=True,
            dynamic_top_industries=4,
            dynamic_stocks_per_industry=12,
            regime_breadth_confirmation=True,
            regime_strong_breadth_min=0.60,
            momentum_tiered_sizing=True,
            tier_a_multiplier=1.0,
            tier_b_multiplier=0.75,
            tier_c_multiplier=0.50
        )
        self.assertTrue(cfg.dynamic_universe_enabled)
        self.assertTrue(cfg.regime_breadth_confirmation)
        self.assertEqual(cfg.regime_strong_breadth_min, 0.60)
        self.assertTrue(cfg.momentum_tiered_sizing)

        # Invalid breadth
        with self.assertRaises(ValueError):
            MomentumConfig(regime_strong_breadth_min=1.5)
        with self.assertRaises(ValueError):
            MomentumConfig(regime_strong_breadth_min=-0.1)

        # Invalid multipliers
        with self.assertRaises(ValueError):
            MomentumConfig(tier_a_multiplier=0.0)
        with self.assertRaises(ValueError):
            MomentumConfig(tier_b_multiplier=5.0)

    def test_momentum_tiered_sizing(self):
        cfg_flat = MomentumConfig(
            max_positions=4,
            max_stock_weight=0.35,
            risk_per_trade=0.02,
            momentum_tiered_sizing=False
        )
        sig_a = {"atr_pct": 0.04, "score": 150.0}
        sig_b = {"atr_pct": 0.04, "score": 85.0}
        sig_c = {"atr_pct": 0.04, "score": 40.0}

        # Without tiered sizing, all identical atr_pct get identical target_weight
        w_flat_a = target_weight(cfg_flat, sig_a, rank=1)
        w_flat_c = target_weight(cfg_flat, sig_c, rank=10)
        self.assertEqual(w_flat_a, w_flat_c)

        # With tiered sizing
        cfg_tiered = MomentumConfig(
            max_positions=4,
            max_stock_weight=0.35,
            risk_per_trade=0.02,
            momentum_tiered_sizing=True,
            tier_a_multiplier=1.0,
            tier_b_multiplier=0.75,
            tier_c_multiplier=0.50
        )
        w_tiered_a = target_weight(cfg_tiered, sig_a, rank=1)   # Tier A: 1.0x
        w_tiered_b = target_weight(cfg_tiered, sig_b, rank=6)   # Tier B: 0.75x
        w_tiered_c = target_weight(cfg_tiered, sig_c, rank=12)  # Tier C: 0.50x

        self.assertAlmostEqual(w_tiered_a, w_flat_a * 1.0)
        self.assertAlmostEqual(w_tiered_b, w_flat_a * 0.75)
        self.assertAlmostEqual(w_tiered_c, w_flat_a * 0.50)
        self.assertGreater(w_tiered_a, w_tiered_b)
        self.assertGreater(w_tiered_b, w_tiered_c)

if __name__ == "__main__":
    unittest.main()
