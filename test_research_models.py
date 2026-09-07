import unittest

from research_models import (
    book_shape,
    build_research_models,
    liquidation_features,
    normalized_delta,
    response_efficiency,
)


def window(buy, sell, large_buy=0, large_sell=0):
    return {
        "buy_usd": buy,
        "sell_usd": sell,
        "delta_usd": buy - sell,
        "buy_ratio": buy / (buy + sell) if buy + sell else None,
        "trade_count": 100,
        "large_buy_count": large_buy,
        "large_sell_count": large_sell,
    }


def base_snapshot():
    return {
        "feed_status": "live",
        "last_update_age_seconds": 0.5,
        "trade_flow": {
            "1m": window(600, 400),
            "5m": window(450, 550),
            "15m": window(650, 350, 3, 1),
        },
        "price_stats": {
            "1m": {"return_pct": 0.03, "efficiency": 0.25, "samples": 6, "span_seconds": 59},
            "5m": {"return_pct": -0.05, "efficiency": 0.20, "samples": 30, "span_seconds": 299},
            "15m": {"return_pct": 0.12, "efficiency": 0.25, "samples": 90, "span_seconds": 899},
            "1h": {"return_pct": 0.40, "efficiency": 0.50, "samples": 360, "span_seconds": 3590},
            "4h": {"return_pct": 1.00, "efficiency": 0.50, "samples": 1440, "span_seconds": 14390},
        },
        "open_interest": {"change_5m_pct": 0.10, "change_15m_pct": 0.15},
        "liquidations_5m": {"long_usd": 100, "short_usd": 300},
        "order_book": {
            "0.1pct": {"imbalance": 0.20, "coverage_complete": True},
            "0.5pct": {"imbalance": 0.05, "coverage_complete": False},
        },
    }


class ResearchModelTest(unittest.TestCase):
    def test_normalized_delta_is_scale_free(self):
        self.assertAlmostEqual(normalized_delta(window(60, 40)), 0.2)
        self.assertAlmostEqual(normalized_delta(window(6_000_000, 4_000_000)), 0.2)

    def test_liquidation_intensity_is_normalized_by_volume(self):
        snap = base_snapshot()
        features = liquidation_features(snap, snap["trade_flow"]["5m"])
        self.assertAlmostEqual(features["intensity_vs_5m_volume"], 0.4)
        self.assertAlmostEqual(features["directional_skew"], 0.5)

        scaled = base_snapshot()
        scaled["liquidations_5m"] = {"long_usd": 1_000, "short_usd": 3_000}
        scaled["trade_flow"]["5m"] = window(4_500, 5_500)
        scaled_features = liquidation_features(scaled, scaled["trade_flow"]["5m"])
        self.assertEqual(features["intensity_vs_5m_volume"], scaled_features["intensity_vs_5m_volume"])

    def test_book_shape_requires_complete_near_and_far_depth(self):
        snap = base_snapshot()
        incomplete = book_shape(snap["order_book"])
        self.assertFalse(incomplete["usable_for_shape"])
        self.assertIsNone(incomplete["near_minus_far"])

        snap["order_book"]["0.5pct"]["coverage_complete"] = True
        complete = book_shape(snap["order_book"])
        self.assertTrue(complete["usable_for_shape"])
        self.assertAlmostEqual(complete["near_minus_far"], 0.15)

    def test_response_efficiency_is_scale_free_flow_response(self):
        self.assertAlmostEqual(response_efficiency(0.05, 0.25), 0.2)
        self.assertIsNone(response_efficiency(0.05, 0.01))

    def test_trend_continuation_long_can_form_after_absorbed_pullback(self):
        snap = base_snapshot()
        # Make 5m counter-flow strong enough to qualify as absorption while
        # keeping 1m flow positive as the re-acceleration trigger.
        snap["trade_flow"]["5m"] = window(400, 600)
        model = build_research_models(snap)
        self.assertEqual(model["data_quality"]["state"], "OK")
        self.assertEqual(model["regime"]["label"], "TREND_UP")
        self.assertGreaterEqual(model["continuation"]["long"], 65)
        self.assertTrue(model["continuation"]["long_absorption"])
        self.assertIn(model["continuation"]["long_state"], ("FORMING_LONG", "WATCH_LONG"))

    def test_positive_flow_with_falling_oi_is_not_fresh_long_expansion(self):
        snap = base_snapshot()
        snap["trade_flow"]["5m"] = window(700, 300)
        snap["price_stats"]["5m"]["return_pct"] = 0.20
        snap["open_interest"]["change_5m_pct"] = -0.20
        model = build_research_models(snap)
        self.assertEqual(model["features"]["positioning_5m"], "SHORT_COVERING_OR_DELEVERAGING")

    def test_stale_feed_blocks_action_state(self):
        snap = base_snapshot()
        snap["last_update_age_seconds"] = 8.0
        model = build_research_models(snap)
        self.assertEqual(model["data_quality"]["state"], "STALE_FEED")
        self.assertEqual(model["regime"]["label"], "UNAVAILABLE")
        self.assertEqual(model["continuation"]["long_state"], "DATA_QUALITY_BLOCK")
        self.assertEqual(model["continuation"]["short_state"], "DATA_QUALITY_BLOCK")

    def test_dense_few_minutes_cannot_pass_4h_warmup(self):
        snap = base_snapshot()
        snap["price_stats"]["1h"].update(samples=500, span_seconds=360)
        snap["price_stats"]["4h"].update(samples=2000, span_seconds=360)
        model = build_research_models(snap)
        self.assertEqual(model["data_quality"]["state"], "WARMUP_4H")
        self.assertFalse(model["data_quality"]["eligible"])
        self.assertAlmostEqual(model["data_quality"]["warmup_progress_4h_pct"], 2.6, places=1)
        self.assertEqual(model["regime"]["label"], "WARMUP")
        self.assertEqual(model["continuation"]["long_state"], "DATA_QUALITY_BLOCK")
        self.assertEqual(model["continuation"]["short_state"], "DATA_QUALITY_BLOCK")

    def test_new_diagnostics_do_not_change_score_weighting(self):
        snap = base_snapshot()
        model_a = build_research_models(snap)
        snap["liquidations_5m"] = {"long_usd": 5_000_000, "short_usd": 0}
        snap["order_book"]["0.5pct"] = {"imbalance": -0.9, "coverage_complete": True}
        model_b = build_research_models(snap)
        self.assertEqual(model_a["continuation"]["long"], model_b["continuation"]["long"])
        self.assertEqual(model_a["continuation"]["short"], model_b["continuation"]["short"])


if __name__ == "__main__":
    unittest.main()
