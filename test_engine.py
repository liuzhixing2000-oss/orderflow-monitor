import unittest
import sys
import types

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


class EngineTest(unittest.TestCase):
    def test_trade_delta_and_book(self):
        s = MarketState("BTCUSDT")
        import time
        now = time.time()
        s.add_trade(now, "Buy", 100, 2)
        s.add_trade(now, "Sell", 100, 1)
        w = s.window(60)
        self.assertEqual(w["delta_usd"], 100)
        self.assertEqual(w["buy_ratio"], 0.6667)
        s.update_book("snapshot", [["99", "10"]], [["101", "5"]])
        s.price = 100
        self.assertGreater(s.book_imbalance(.02)["imbalance"], 0)

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
