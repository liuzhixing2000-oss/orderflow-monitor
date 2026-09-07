import time
import unittest

from engine import MarketState


class ResearchEngineWarmupTest(unittest.TestCase):
    def test_local_structure_stays_warmup_with_dense_short_history(self):
        state = MarketState("BTCUSDT")
        now = time.time()
        # Many samples but only five minutes of clock time.
        for i in range(61):
            ts = now - 300 + i * 5
            state.price_samples.append((ts, 100 + i * 0.01))
        state.price = 100.60
        structure = state.local_structure()
        self.assertEqual(structure["1h"]["trend"], "warmup")
        self.assertEqual(structure["4h"]["trend"], "warmup")
        self.assertFalse(structure["1h"]["eligible"])
        self.assertFalse(structure["4h"]["eligible"])

    def test_local_structure_can_activate_after_real_clock_span(self):
        state = MarketState("BTCUSDT")
        now = time.time()
        # 241 points across 4h. The final hour contains ~61 points, satisfying
        # both clock-span and sample-density gates.
        for i in range(241):
            ts = now - 14_400 + i * 60
            state.price_samples.append((ts, 100 + i * 0.02))
        state.price = 104.80
        structure = state.local_structure()
        self.assertTrue(structure["1h"]["eligible"])
        self.assertTrue(structure["4h"]["eligible"])
        self.assertEqual(structure["1h"]["trend"], "up")
        self.assertEqual(structure["4h"]["trend"], "up")


if __name__ == "__main__":
    unittest.main()
