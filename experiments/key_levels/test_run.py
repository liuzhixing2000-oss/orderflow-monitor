import copy
import unittest
from run import Series, crossings, HOLD

def row(ts, price=100, score=40):
    return {'ts':ts,'price':price,'symbol':'ETHUSDT','live':True,'quality':True,
            'age':1,'version':'0.1.2-research','score':score,'regime':'TREND_UP',
            'r5':.1,'r1h':.1,'r4h':.2,'source':'synthetic'}

class CausalityTest(unittest.TestCase):
    def test_future_extremes_cannot_change_labels(self):
        rows=[row(i*60,100+(i%30)/30) for i in range(2100)]
        i=1900
        expected=Series(rows[:i+1]).labels(i)
        self.assertTrue(all(v is not None for v in expected['flags'].values()))
        changed=copy.deepcopy(rows)
        for r in changed[i+1:]:r['price']=10000
        self.assertEqual(expected,Series(changed).labels(i))

    def test_current_touch_cannot_create_historical_support(self):
        rows=[row(i*60,100+(i%30)/30) for i in range(2000)]
        i=1900
        before=Series(rows).labels(i)['levels']
        rows[i]['price']=1
        self.assertEqual(before,Series(rows).labels(i)['levels'])

    def test_atr_uses_only_completed_bars(self):
        rows=[row(i*60,100+(i%30)/30) for i in range(2000)]
        i=1904; t=rows[i]['ts']; boundary=int(t//900)*900
        expected=Series(rows).atr(t)
        self.assertIsNotNone(expected)
        for r in rows:
            if r['ts']>=boundary:r['price']=10000
        self.assertEqual(expected,Series(rows).atr(t))

    def test_cooldown_and_gap_crossings(self):
        rows=[row(0,score=40),row(60,score=60),row(120,score=40),row(180,score=60),
              row(14500,score=40),row(14560,score=60),row(14620,score=40),row(14800,score=60)]
        self.assertEqual(crossings(Series(rows),60),[1,5])

    def test_gap_is_unknown_not_failed_level(self):
        rows=[row(i*60,100+(i%30)/30) for i in range(2000)]
        rows=rows[:1850]+rows[1890:]
        labels=Series(rows).labels(len(rows)-1)
        self.assertIsNone(labels['atr'])
        self.assertTrue(all(v is None for v in labels['flags'].values()))

    def test_outcome_and_missing_path(self):
        rows=[row(i*60,100+i/100) for i in range(245)]
        result=Series(rows).outcome(0)
        self.assertAlmostEqual(result['net'],2.4-.12)
        self.assertEqual(result['holding_seconds'],HOLD)
        missing=rows[:100]+rows[110:]
        self.assertEqual(Series(missing).outcome(0)['reason'],'PATH_GAP_OR_LOW_COVERAGE')
        self.assertEqual(Series(rows[:100]).outcome(0)['reason'],'IMMATURE_OR_MISSING_EXIT')

if __name__=='__main__':unittest.main()
