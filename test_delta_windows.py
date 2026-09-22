"""Regression cases for rolling CVD expiry and long-window completeness."""
import unittest
from unittest.mock import patch
from engine import MarketState, settings


class DeltaWindowTest(unittest.TestCase):
    def test_expiring_old_trade_does_not_change_session_cvd(self):
        s = MarketState('ETHUSDT', connected=True)
        with patch('engine.time.time', return_value=100):
            s.add_trade(100, 'Buy', 100, 10)
        before = s.cvd_usd
        with patch('engine.time.time', return_value=86501):
            s.add_trade(86501, 'Sell', 100, 1)
            snap = s.snapshot()
        self.assertEqual(snap['cvd_since_start_usd'] - before, -100)
        self.assertEqual(snap['trade_flow']['5m']['delta_usd'], -100)
        self.assertEqual(len(s.trades), 1)

    def test_direct_windows_and_boundaries(self):
        s = MarketState('ETHUSDT', connected=True)
        with patch('engine.time.time', return_value=20000):
            for ts, side, qty in [(5600,'Buy',100), (5601,'Buy',10),
                                  (16400,'Sell',5), (16401,'Buy',2),
                                  (19700,'Buy',9), (19701,'Sell',1),
                                  (20001,'Buy',999)]:
                s.add_trade(ts, side, 1, qty)
            self.assertEqual(s.window(300)['delta_usd'], -1)
            self.assertEqual(s.window(3600)['delta_usd'], 10)
            self.assertEqual(s.window(14400)['delta_usd'], 15)
            self.assertTrue(s.window(14400)['complete'])

    def test_startup_is_not_full_four_hours(self):
        s = MarketState('ETHUSDT', connected=True)
        with patch('engine.time.time', return_value=20000):
            s.add_trade(19900, 'Buy', 100, 1)
            snap = s.snapshot()
        self.assertFalse(snap['trade_flow']['4h']['complete'])
        self.assertFalse(snap['trade_flow']['1h']['complete'])
        self.assertIn('change_4h_pct', snap['open_interest'])
        self.assertNotEqual(s.session_id, MarketState('ETHUSDT').session_id)

    def test_reconnect_marks_only_affected_windows_partial(self):
        s = MarketState('ETHUSDT', connected=True)
        with patch('engine.time.time', return_value=20000):
            s.add_trade(1000, 'Buy', 1, 1)
            s.mark_disconnected()
            self.assertEqual(s.window(300)['status'], 'disconnected')
            s.connected = True
            s.add_trade(19000, 'Buy', 1, 1)
            self.assertTrue(s.window(300)['complete'])
            self.assertFalse(s.window(3600)['complete'])

    def test_insufficient_retention(self):
        s = MarketState('ETHUSDT', connected=True, continuous_since=0)
        with patch.object(settings, 'history_seconds', 900):
            self.assertEqual(s.window(3600, 20000)['status'], 'insufficient_retention')

    def test_oi_requires_endpoint_history_and_freshness(self):
        s = MarketState('ETHUSDT', connected=True)
        s.oi.extend([(5600,100), (16400,105), (19990,110)])
        self.assertAlmostEqual(s.oi_change(14400,20000),10)
        self.assertAlmostEqual(s.oi_change(3600,20000),100*(110/105-1))
        self.assertIsNone(s.oi_change(300,20000))
        self.assertIsNone(s.oi_change(14400,20100))
        s.oi.clear()
        s.oi.extend([(19900,100),(20000,110)])
        self.assertIsNone(s.oi_change(3600,20000))


if __name__ == '__main__':
    unittest.main()
