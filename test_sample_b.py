import json
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
import asyncio
from pathlib import Path
from sample_b import SampleB, MODEL
from storage import Storage
from test_research_events import snapshot


class SampleBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.tmp.name) / 'test.db'))
        self.storage.init()
        self.b = SampleB(self.storage)
        self.b.init(now=1000, provenance={'fingerprint': 'fixed'})

    def tearDown(self):
        self.tmp.cleanup()

    def obs(self, ts, score=60, **kwargs):
        s = snapshot(ts, score, **kwargs)
        s['symbol'] = 'ETHUSDT'
        s['last_update_age_seconds'] = 1
        s['models']['version'] = MODEL
        s['price_stats'].update({'1h': {'return_pct': .1}, '4h': {'return_pct': .2}})
        return s

    def rows(self):
        return self.b.export()['rows']

    def test_warmup_gap_and_restart_cannot_create_crossing(self):
        for t, score in [(15000, 60), (15400, 80), (15460, 60), (15600, 80), (15660, 60), (15720, 80)]:
            self.b.observe(self.obs(t, score))
        self.assertEqual(sum(r['primary_event'] for r in self.rows()), 1)
        self.b = SampleB(self.storage)
        self.b.init(now=16000, provenance={'fingerprint': 'fixed'})
        self.b.observe(self.obs(16010, 60))
        self.b.observe(self.obs(16070, 80))
        self.assertEqual(sum(r['primary_event'] for r in self.rows()), 1)
        self.assertEqual(self.b.status()['started_ts'], 1000)

    def test_cooldown_duplicate_and_quality(self):
        for t, score in [(15400, 60), (15460, 80), (15520, 60), (15580, 80)]:
            self.b.observe(self.obs(t, score))
        self.b.observe(self.obs(15460, 10))
        stale = self.obs(15640, 60); stale['last_update_age_seconds'] = 10
        self.b.observe(stale)
        self.b.observe(self.obs(15700, 80))
        self.assertEqual(len(self.rows()), 6)
        self.assertEqual(sum(r['primary_event'] for r in self.rows()), 1)
        self.assertIn('STALE_PRICE', self.rows()[-2]['reason'])

    def test_price_control_does_not_require_flow_regime(self):
        self.b.observe(self.obs(15400, 20, regime='RANGE'))
        row = self.rows()[0]
        self.assertEqual((row['price_gate'], row['price_event'], row['primary_event']), (1, 1, 0))

    def test_drift_permanently_blocks_enrollment(self):
        self.b.init(now=2000, provenance={'fingerprint': 'different'})
        self.b.observe(self.obs(20000))
        self.assertEqual(self.rows(), [])
        self.b.init(now=3000, provenance={'fingerprint': 'fixed'})
        self.assertEqual(self.b.status()['state'], 'VERSION_DRIFT')

    def test_complete_path_and_missing_exit_are_preserved(self):
        self.b.observe(self.obs(15400, 60))
        self.b.observe(self.obs(15460, 80))
        with self.storage.connect() as db:
            db.executemany('INSERT INTO price_path VALUES(?,?,?)', [(15400+i*10, 'ETHUSDT', 100+i/1000) for i in range(1441)])
        self.assertEqual(self.b.evaluate(now=15400+14400+179), 0)
        self.assertEqual(self.b.evaluate(now=31000), 2)
        first, second = [json.loads(r['outcome']) for r in self.rows()]
        self.assertTrue(first['quality_ok'])
        self.assertEqual(second['exclusion_reason'], 'MISSING_EXIT_PRICE')
        self.assertEqual(self.b.evaluate(now=32000), 0)
        self.assertFalse(self.b.status()['minimum_evidence_gate_met'])

    def test_payload_is_causal_and_export_is_paginated(self):
        self.b.observe(self.obs(15400))
        before = self.rows()[0]['payload']
        self.b.observe(self.obs(15460, 80))
        self.assertEqual(before, self.rows()[0]['payload'])
        page = self.b.export(limit=1)
        self.assertEqual(self.b.export(after_id=page['next_after_id'])['rows'][0]['ts'], 15460)

    def test_model_version_drift_blocks(self):
        s = self.obs(15400); s['models']['version'] = 'changed'
        self.b.observe(s)
        self.assertEqual(self.b.status()['state'], 'MODEL_VERSION_DRIFT')
        self.assertEqual(self.rows(), [])


class PathFreshnessTest(unittest.IsolatedAsyncioTestCase):
    async def test_connected_stale_price_is_not_recorded(self):
        import main
        states = {name: SimpleNamespace(price=100, connected=True, last_update=ts)
                  for name, ts in (("fresh", 999), ("stale", 990), ("missing", None))}
        with patch.object(main.engine, 'states', states), patch.object(main.time, 'time', return_value=1000), \
             patch.object(main.storage, 'insert_price_points') as insert, \
             patch.object(main.asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await main.price_path_sampler()
        self.assertEqual(insert.call_args.args[0], [(1000, 'fresh', 100.0)])


if __name__ == '__main__':
    unittest.main()
