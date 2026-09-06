import unittest

from research_models import build_research_models, normalized_delta


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
            "1m": {"return_pct": 0.03, "efficiency": 0.25, "samples": 6},
            "5m": {"return_pct": -0.05, "efficiency": 0.20, "samples": 30},
            "15m": {"return_pct": 0.12, "efficiency": 0.25, "samples": 90},
            "1h": {"return_pct": 0.40, "efficiency": 0.50, "samples": 360},
            "4h": {"return_pct": 1.00, "efficiency": 0.50, "samples": 1440},
        },
        "open_interest": {"change_5m_pct": 0.10, "change_15m_pct": 0.15},
        "order_book": {
            "0.1pct": {"imbalance": 0.20, "coverage_complete": True},
            "0.5pct": {"imbalance": 0.05, "coverage_complete": False},
        },
    }


class ResearchModelTest(unittest.TestCase):
    def test_normalized_delta_is_scale_free(self):
        self.assertAlmostEqual(normalized_delta(window(60, 40)), 0.2)
        self.assertAlmostEqual(normalized_delta(window(6_000_000, 4_000_000)), 0.2)

    def test_trend_continuation_long_can_form_after_absorbed_pullback(self):
        snap = base_snapshot()
        # Make 5m counter-flow strong enough to qualify as absorption while
        # keeping 1m flow positive as the re-acceleration trigger.
        snap["trade_flow"]["5m"] = window(400, 600)
        model = build_research_models(snap)
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
        self.assertEqual(model["continuation"]["long_state"], "DATA_QUALITY_BLOCK")
        self.assertEqual(model["continuation"]["short_state"], "DATA_QUALITY_BLOCK")


if __name__ == "__main__":
    unittest.main()
