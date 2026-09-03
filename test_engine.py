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
)
sys.modules["app.config"] = fake_config

from app.engine import MarketState  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
