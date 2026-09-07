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
        "assessment": {
            "long_score": 50,
            "short_score": 50,
            "stance": "mixed",
        },
        "models": {
            "version": "test-model",
            "data_quality": {"state": "OK" if eligible else "WARMUP_4H", "eligible": eligible},
            "regime": {"label": regime if eligible else "WARMUP", "eligible": eligible},
            "continuation": {
                "long": score,
                "short": 0,
                "long_state": "NONE",
                "short_state": "DISABLED_BY_REGIME",
            },
        },
    }


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

    def test_event_results_include_net_return_mfe_and_mae(self):
        event_ts = time.time() - 4_000
        self.storage.insert(snapshot(event_ts - 60, 40, price=100.0, eligible=True))
        self.storage.insert(snapshot(event_ts, 65, price=100.0, eligible=True))

        # Disable research eligibility for follow-up snapshots so they only act
        # as the future price path for evaluation.
        self.storage.insert(snapshot(event_ts + 300, 0, price=98.0, eligible=False))
        self.storage.insert(snapshot(event_ts + 600, 0, price=103.0, eligible=False))
        self.storage.insert(snapshot(event_ts + 900, 0, price=102.0, eligible=False))

        result = self.storage.event_results("BTCUSDT", 15, "long", 60)
        self.assertEqual(result["events_evaluated"], 1)
        self.assertAlmostEqual(result["avg_net_return_pct"], 1.88, places=4)
        self.assertAlmostEqual(result["avg_mfe_pct"], 3.0, places=4)
        self.assertAlmostEqual(result["avg_mae_pct"], -2.0, places=4)

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
