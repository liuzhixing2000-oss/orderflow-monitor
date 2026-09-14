import copy
import csv
import json
import unittest
from pathlib import Path

import run_audit as audit


class AuditTests(unittest.TestCase):
    def test_legacy_minute_jitter_is_not_interpolated(self):
        stat=audit.legacy.calc_price_stats([0,60.2,120.4],[100,101,102],2,60)
        self.assertIsNone(stat['return_pct'])
        self.assertEqual(stat['samples'],1)

    def test_future_prices_cannot_change_features_or_events(self):
        rows=[]
        for i in range(300):
            rows.append(dict(timestamp=i*60.1,price=100+i*.01,feed_status='live',last_update_age_seconds=0,
                             trade_flow={k:{'buy_usd':120,'sell_usd':100} for k in ('1m','5m','15m')},
                             open_interest={'change_5m_pct':.1}))
        first=audit.compute_rows(rows)
        changed=copy.deepcopy(rows)
        for r in changed[260:]:
            r['price']*=4
            r['trade_flow']={}
        second=audit.compute_rows(changed)
        # Payload hashes change only for deliberately modified future rows.
        self.assertEqual(first[:260],second[:260])
        for side in ('long','short'):
            for t in (50,60,70,80,90):
                a=[e for e in audit.events_for(first,side,t) if e['row']['i']<260]
                b=[e for e in audit.events_for(second,side,t) if e['row']['i']<260]
                self.assertEqual(a,b)

    def test_horizon_does_not_take_price_beyond_exit_tolerance(self):
        rows=[dict(i=0,ts=0,price=100),dict(i=1,ts=1081,price=110)]
        self.assertIsNone(audit.evaluate(rows,rows[0],15,'long')['net'])

    def test_gap_exclusion_preserves_reported_endpoint_return(self):
        rows=[dict(i=0,ts=0,price=100),dict(i=1,ts=900,price=101)]
        outcome=audit.evaluate(rows,rows[0],15,'long')
        self.assertAlmostEqual(outcome['net'],.88)
        self.assertFalse(outcome['quality'])

    def test_price_gate_does_not_read_flow(self):
        stats={'1h':{'return_pct':.1},'4h':{'return_pct':.2}}
        self.assertTrue(audit.price_gate(stats,'long'))
        self.assertFalse(audit.price_gate(stats,'short'))

    def test_beta_is_estimated_before_event(self):
        btc=[];eth=[];bp=100;ep=200
        for i in range(110):
            if i:
                ret=(i%7-3)*.001
                bp*=1+ret
                ep*=1+2*ret
            btc.append(dict(ts=i*900,price=bp))
            eth.append(dict(ts=i*900,price=ep))
        event=dict(event_id='known-beta',symbol='ETHUSDT',side='long',threshold=70,horizon=240,
                   day='1970-01-02',net=.5,quality=True,entry_ts=96*900+1,exit_ts=108*900+1)
        a=audit.btc_attribution({'BTCUSDT':btc,'ETHUSDT':eth},[event])[0]
        self.assertAlmostEqual(a['beta'],2)
        for r in btc[97:]:r['price']*=10
        b=audit.btc_attribution({'BTCUSDT':btc,'ETHUSDT':eth},[event])[0]
        self.assertEqual(a['beta'],b['beta'])
        self.assertNotEqual(a['btc_directional_gross'],b['btc_directional_gross'])

    def test_real_controls_mature_before_signal_and_are_separated(self):
        root=Path(__file__).resolve().parent/'results'
        signals={r['event_id']:r for r in csv.DictReader((root/'events.csv').read_text().splitlines())}
        groups={}
        for r in csv.DictReader((root/'matched_controls.csv').read_text().splitlines()):
            signal=signals[r['event_id']]
            self.assertEqual(r['symbol'],signal['symbol'])
            self.assertLessEqual(float(r['control_exit_ts']),float(signal['entry_ts']))
            self.assertLessEqual(float(r['distance']),1)
            groups.setdefault(r['event_id'],[]).append(float(r['control_ts']))
        self.assertTrue(groups)
        for ts in groups.values():
            self.assertLessEqual(len(ts),5)
            for a,b in zip(sorted(ts),sorted(ts)[1:]):
                self.assertGreaterEqual(b-a,14400)

    def test_source_reconciliation_does_not_label_subset_complete(self):
        root=Path(__file__).resolve().parent
        self.assertFalse(json.loads((root/'evidence/manifest.json').read_text())['full_sqlite_sample_reproduced'])
        rows=list(csv.DictReader((root/'results/original_report_reconciliation.csv').read_text().splitlines()))
        eth=[r for r in rows if r['symbol']=='ETHUSDT' and r['side']=='long' and r['threshold']=='70']
        self.assertEqual(len(eth),4)
        self.assertTrue(all(r['agrees']=='True' for r in eth))


if __name__=='__main__':
    unittest.main()
