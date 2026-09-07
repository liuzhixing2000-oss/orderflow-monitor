import json
import sqlite3
import unittest

from historical_context import (
    build_historical_context,
    distribution_position,
    extract_scalar_features,
    stored_cvd_slope,
)


def feature_snapshot(ts, delta_norm=0.2, book_complete=True):
    buy = 50 * (1 + delta_norm)
    sell = 50 * (1 - delta_norm)
    return {
        "timestamp": ts,
        "symbol": "BTCUSDT",
        "trade_flow": {
            "1m": {
                "buy_usd": buy,
                "sell_usd": sell,
                "delta_usd": buy - sell,
            }
        },
        "open_interest": {"change_5m_pct": 0.03, "change_15m_pct": 0.05},
        "order_book": {
            "0.1pct": {"imbalance": 0.2, "coverage_complete": book_complete},
            "0.5pct": {"imbalance": 0.1, "coverage_complete": book_complete},
        },
        "price_stats": {"5m": {"return_pct": 0.05}},
        "models": {
            "features": {
                "normalized_delta": {"1m": delta_norm, "5m": delta_norm / 2, "15m": delta_norm / 4},
                "flow_acceleration_1m_minus_5m": delta_norm / 2,
                "flow_acceleration_5m_minus_15m": delta_norm / 4,
                "activity_burst_1m_vs_5m": 1.1,
                "activity_burst_5m_vs_15m": 1.0,
                "liquidations": {
                    "intensity_vs_5m_volume": 0.01,
                    "directional_skew": -0.2,
                },
                "book_shape": {"near_minus_far": 0.1 if book_complete else None},
                "price_response_efficiency": {"5m": 0.5},
            }
        },
    }


class HistoricalContextTest(unittest.TestCase):
    def test_distribution_position_uses_robust_past_distribution(self):
        history = [float(i) for i in range(40)]
        result = distribution_position(30.0, history)
        self.assertTrue(result["eligible"])
        self.assertGreater(result["percentile"], 70)
        self.assertAlmostEqual(result["median"], 19.5)
        self.assertIsNotNone(result["robust_z"])

    def test_incomplete_book_depth_is_not_normalized(self):
        features = extract_scalar_features(feature_snapshot(1000, book_complete=False))
        self.assertIsNone(features["book_near_imbalance_complete"])
        self.assertIsNone(features["book_near_minus_far"])

    def test_stored_cvd_slope_is_scale_free_and_requires_time_span(self):
        start = 1_000.0
        rows = [(start + i * 60, feature_snapshot(start + i * 60, 0.2)) for i in range(14)]
        current = feature_snapshot(start + 14 * 60, 0.2)
        result = stored_cvd_slope(rows, current, 15)
        self.assertTrue(result["eligible"])
        self.assertAlmostEqual(result["normalized_slope"], 0.2, places=5)

        dense_rows = [(start + i * 5, feature_snapshot(start + i * 5, 0.2)) for i in range(14)]
        dense_current = feature_snapshot(start + 14 * 5, 0.2)
        dense = stored_cvd_slope(dense_rows, dense_current, 15)
        self.assertFalse(dense["eligible"])

    def test_build_context_never_reads_future_snapshot(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                long_score INTEGER NOT NULL,
                short_score INTEGER NOT NULL,
                stance TEXT NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        # 35 past observations make the distribution eligible.
        for i in range(35):
            snap = feature_snapshot(1_000 + i * 60, 0.1 + i * 0.001)
            db.execute(
                "INSERT INTO snapshots(ts,symbol,price,long_score,short_score,stance,payload) VALUES(?,?,?,?,?,?,?)",
                (snap["timestamp"], "BTCUSDT", 100.0, 50, 50, "mixed", json.dumps(snap)),
            )
        current = feature_snapshot(4_000, 0.30)
        # Extreme future value must be invisible to event-time normalization.
        future = feature_snapshot(4_060, 0.99)
        db.execute(
            "INSERT INTO snapshots(ts,symbol,price,long_score,short_score,stance,payload) VALUES(?,?,?,?,?,?,?)",
            (future["timestamp"], "BTCUSDT", 100.0, 50, 50, "mixed", json.dumps(future)),
        )
        db.commit()

        context = build_historical_context(db, current)
        self.assertTrue(context["strictly_past_only"])
        self.assertEqual(context["past_snapshots_considered"], 35)
        self.assertTrue(context["distributions"]["delta_norm_1m"]["eligible"])
        # Current 0.30 is above all 35 past values; the future 0.99 must not lower it.
        self.assertEqual(context["distributions"]["delta_norm_1m"]["percentile"], 100.0)
        db.close()


if __name__ == "__main__":
    unittest.main()
