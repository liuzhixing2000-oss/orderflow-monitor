import tempfile
import time
import unittest
from pathlib import Path

from storage import Storage


def snapshot(ts, score, price=100.0, eligible=True, regime="TREND_UP"):
    return {
        "timestamp": ts,
        "symbol": "BTCUSDT",
        "price": price,
        "trade_flow": {
            "1m": {
                "buy_usd": 60.0,
                "sell_usd": 40.0,
                "delta_usd": 20.0,
                "buy_ratio": 0.6,
                "trade_count": 10,
                "large_buy_count": 0,
                "large_sell_count": 0,
            }
        },
        "open_interest": {"change_5m_pct": 0.01, "change_15m_pct": 0.02},
        "order_book": {
            "0.1pct": {"imbalance": 0.1, "coverage_complete": True},
            "0.5pct": {"imbalance": 0.05, "coverage_complete": True},
        },
        "price_stats": {
            "5m": {"return_pct": 0.05},
        },
        "assessment": {
            "long_score": 50,
            "short_score": 50,
            "stance": "mixed",
        },
        "models": {
            "version": "test-model",
            "data_quality": {"state": "OK" if eligible else "WARMUP_4H", "eligible": eligible},
            "regime": {"label": regime if eligible else "WARMUP", "eligible": eligible},
            "features": {
                "normalized_delta": {"1m": 0.2, "5m": 0.1, "15m": 0.05},
                "flow_acceleration_1m_minus_5m": 0.1,
                "flow_acceleration_5m_minus_15m": 0.05,
                "activity_burst_1m_vs_5m": 1.1,
                "activity_burst_5m_vs_15m": 1.0,
                "liquidations": {"intensity_vs_5m_volume": 0.01, "directional_skew": 0.2},
                "book_shape": {"near_minus_far": 0.05},
                "price_response_efficiency": {"5m": 0.5},
            },
            "continuation": {
                "long": score,
                "short": 0,
                "long_state": "NONE",
                "short_state": "DISABLED_BY_REGIME",
            },
        },
    }


def dense_15m_path(event_ts):
    """91 ten-second points: 100 -> 98 -> 103 -> 102."""
    points = []
    for i in range(91):
        if i <= 30:
            price = 100.0 - 2.0 * (i / 30)
        elif i <= 60:
            price = 98.0 + 5.0 * ((i - 30) / 30)
        else:
            price = 103.0 - 1.0 * ((i - 60) / 30)
        points.append((event_ts + i * 10, "BTCUSDT", price))
    return points


class ResearchEventStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "research.db")
        self.storage = Storage(self.db_path)
        self.storage.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_threshold_crossing_requires_previous_eligible_state(self):
        now = time.time() - 5_000
        # Warmup-to-live should not create a fake event even if the first live
        # observation is already above the threshold.
        self.storage.insert(snapshot(now, 40, eligible=False))
        events = self.storage.insert(snapshot(now + 60, 85, eligible=True))
        self.assertEqual(events, [])

        # Once the live/eligible series moves below and then crosses, events are valid.
        self.storage.insert(snapshot(now + 120, 40, eligible=True))
        events = self.storage.insert(snapshot(now + 180, 85, eligible=True))
        thresholds = sorted(x["threshold"] for x in events)
        self.assertEqual(thresholds, [50, 60, 70, 80])

    def test_cooldown_blocks_repeated_crossings(self):
        now = time.time() - 30_000
        self.storage.insert(snapshot(now, 40, eligible=True))
        first = self.storage.insert(snapshot(now + 60, 65, eligible=True))
        self.assertIn(60, [x["threshold"] for x in first])

        # Dip and recross one hour later: still inside the 4h cooldown.
        self.storage.insert(snapshot(now + 3_000, 40, eligible=True))
        blocked = self.storage.insert(snapshot(now + 3_600, 65, eligible=True))
        self.assertNotIn(60, [x["threshold"] for x in blocked])

        # A fresh dip and recross after the cooldown is eligible again.
        self.storage.insert(snapshot(now + 15_000, 40, eligible=True))
        allowed = self.storage.insert(snapshot(now + 15_060, 65, eligible=True))
        self.assertIn(60, [x["threshold"] for x in allowed])

    def test_event_results_use_dense_path_for_net_return_mfe_and_mae(self):
        # Old enough for 15m + the 3m materialization tolerance, but not old
        # enough to accidentally mature every longer horizon in this test.
        event_ts = time.time() - 1_500
        self.storage.insert(snapshot(event_ts - 60, 40, price=100.0, eligible=True))
        self.storage.insert(snapshot(event_ts, 65, price=100.0, eligible=True))
        self.storage.insert_price_points(dense_15m_path(event_ts))

        materialized = self.storage.evaluate_pending_outcomes()
        self.assertGreaterEqual(materialized["quality_ok"], 1)

        result = self.storage.event_results("BTCUSDT", 15, "long", 60)
        self.assertEqual(result["events_evaluated"], 1)
        self.assertEqual(result["outcomes_excluded_data_quality"], 0)
        self.assertAlmostEqual(result["avg_net_return_pct"], 1.88, places=4)
        self.assertAlmostEqual(result["avg_mfe_pct"], 3.0, places=4)
        self.assertAlmostEqual(result["avg_mae_pct"], -2.0, places=4)
        self.assertGreaterEqual(result["avg_path_coverage_ratio"], 0.99)
        self.assertLessEqual(result["max_gap_seconds"], 10.1)

    def test_low_coverage_event_is_materialized_but_excluded(self):
        event_ts = time.time() - 1_500
        self.storage.insert(snapshot(event_ts - 60, 40, eligible=True))
        self.storage.insert(snapshot(event_ts, 65, eligible=True))
        sparse = [
            (event_ts, "BTCUSDT", 100.0),
            (event_ts + 450, "BTCUSDT", 101.0),
            (event_ts + 900, "BTCUSDT", 102.0),
        ]
        self.storage.insert_price_points(sparse)
        self.storage.evaluate_pending_outcomes()
        result = self.storage.event_results("BTCUSDT", 15, "long", 60)
        self.assertEqual(result["outcomes_materialized"], 1)
        self.assertEqual(result["outcomes_excluded_data_quality"], 1)
        self.assertEqual(result["events_evaluated"], 0)

    def test_event_payload_contains_past_only_historical_context(self):
        base_ts = time.time() - 5_000
        # Seed prior snapshots before the valid crossing.
        for i in range(35):
            self.storage.insert(snapshot(base_ts + i * 60, 40, eligible=True))
        event_ts = base_ts + 36 * 60
        self.storage.insert(snapshot(event_ts - 60, 40, eligible=True))
        self.storage.insert(snapshot(event_ts, 65, eligible=True))

        with self.storage.connect() as db:
            row = db.execute(
                "SELECT payload FROM research_events WHERE threshold=60 ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        payload = __import__("json").loads(row["payload"])
        context = payload["historical_context"]
        self.assertTrue(context["strictly_past_only"])
        self.assertGreaterEqual(context["past_snapshots_considered"], 30)
        self.assertTrue(context["distributions"]["delta_norm_1m"]["eligible"])

    def test_price_path_can_be_restored(self):
        now = time.time()
        self.storage.insert_price_points([
            (now - 20, "BTCUSDT", 100.0),
            (now - 10, "BTCUSDT", 101.0),
            (now - 10, "ETHUSDT", 20.0),
        ])
        history = self.storage.load_price_history(60)
        self.assertEqual([p for _, p in history["BTCUSDT"]], [100.0, 101.0])
        self.assertEqual([p for _, p in history["ETHUSDT"]], [20.0])


if __name__ == "__main__":
    unittest.main()
