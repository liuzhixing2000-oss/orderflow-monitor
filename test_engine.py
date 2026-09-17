import unittest
import sys
import types
import time

# Keep the unit test runnable before deployment dependencies are installed.
sys.modules.setdefault("httpx", types.ModuleType("httpx"))
sys.modules.setdefault("websockets", types.ModuleType("websockets"))
fake_config = types.ModuleType("app.config")
fake_config.settings = types.SimpleNamespace(
    history_seconds=86400, large_trade_usd=250000,
    symbol_list=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    bybit_ws_url="", bybit_rest_url="", book_depth=50,
    coinglass_api_key="", coinglass_api_url="", liquidation_map_range="1d",
    liquidation_map_refresh_seconds=300, liquidation_map_max_distance_pct=8.0,
    liquidation_map_cluster_band_pct=0.25,
)
sys.modules["config"] = fake_config

from engine import MarketState  # noqa: E402
from liquidation_map import assess_squeeze_path, summarize_liquidation_map  # noqa: E402
from liquidations import LiquidationAggregator, classify_liquidation_regime  # noqa: E402


class EngineTest(unittest.TestCase):
    def test_trade_delta_and_book(self):
        s = MarketState("BTCUSDT")
        now = time.time()
        s.add_trade(now, "Buy", 100, 2)
        s.add_trade(now, "Sell", 100, 1)
        w = s.window(60)
        self.assertEqual(w["delta_usd"], 100)
        self.assertEqual(w["buy_ratio"], 0.6667)
        s.update_book("snapshot", [["99", "10"]], [["101", "5"]])
        s.price = 100
        self.assertGreater(s.book_imbalance(.02)["imbalance"], 0)
        # Check new fields
        self.assertEqual(s.book_imbalance(.02)["bid_shares"], 10)
        self.assertEqual(s.book_imbalance(.02)["ask_shares"], 5)
        self.assertIsNotNone(s.book_imbalance(.02)["imbalance_shares"])

    def test_liquidation_aggregator(self):
        agg = LiquidationAggregator("BTCUSDT")
        now = time.time()
        agg.add_liquidation("bybit", now, "long", 100_000)
        agg.add_liquidation("bybit", now, "short", 50_000)
        agg.add_liquidation("binance", now, "short", 30_000)
        
        window = agg.window(60)
        self.assertEqual(window["total_long_usd"], 100_000)
        self.assertEqual(window["total_short_usd"], 80_000)
        self.assertEqual(window["by_exchange"]["bybit"]["long_usd"], 100_000)
        self.assertEqual(window["by_exchange"]["bybit"]["short_usd"], 50_000)
        self.assertEqual(window["by_exchange"]["binance"]["long_usd"], 0)
        self.assertEqual(window["by_exchange"]["binance"]["short_usd"], 30_000)

    def test_liquidation_regime_long_deleveraging(self):
        regime = classify_liquidation_regime(
            price_change_15m_pct=-1.5,
            oi_change_15m_pct=-15.0,
            trade_delta_15m_usd=-50_000,
            liquidations_15m={"long_usd": 500_000, "short_usd": 0},
        )
        self.assertEqual(regime["regime"], "long_deleveraging")
        self.assertGreater(regime["confidence"], 70)
        self.assertIn("Long liquidations", str(regime["evidence"]))

    def test_liquidation_regime_short_squeeze(self):
        regime = classify_liquidation_regime(
            price_change_15m_pct=1.2,
            oi_change_15m_pct=2.0,
            trade_delta_15m_usd=100_000,
            liquidations_15m={"long_usd": 0, "short_usd": 300_000},
        )
        self.assertEqual(regime["regime"], "short_squeeze")
        self.assertGreater(regime["confidence"], 60)

    def test_liquidation_regime_fresh_position_building(self):
        regime = classify_liquidation_regime(
            price_change_15m_pct=0.8,
            oi_change_15m_pct=12.0,
            trade_delta_15m_usd=80_000,
            liquidations_15m={"long_usd": 0, "short_usd": 200_000},
        )
        self.assertEqual(regime["regime"], "fresh_position_building")
        self.assertGreater(regime["confidence"], 60)

    def test_liquidation_map_and_squeeze_context(self):
        payload = {"code": "0", "data": {"data": {
            "104": [[104, 2_000_000, 25, None], [104.1, 1_000_000, 50, None]],
            "96": [[96, 1_000_000, 25, None]],
        }}}
        result = summarize_liquidation_map(payload, 100, cluster_band_pct=0.25)
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["upside_short_liquidations"]["total_nearby_usd"], 3_000_000)
        assessment = assess_squeeze_path(
            result,
            trade_flow={
                "1m": {"buy_ratio": .7, "delta_usd": 10},
                "5m": {"buy_ratio": .65, "delta_usd": 20},
                "15m": {"buy_ratio": .6, "delta_usd": 30},
            },
            structure={"1h": {"trend": "up"}, "4h": {"trend": "up"}},
            order_book={"0.1pct": {"imbalance": .3}},
            oi_change_15m=-.2,
            liquidations_5m={"long_usd": 0, "short_usd": 100_000},
            price_change_5m_pct=.4,
        )
        self.assertGreater(assessment["upside_reachability_score"], assessment["downside_reachability_score"])


if __name__ == "__main__":
    unittest.main()

