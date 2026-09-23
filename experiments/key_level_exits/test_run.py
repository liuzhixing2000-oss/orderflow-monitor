import unittest
import run as m

def series(prices,adverse=False):
    px=prices+[prices[-1]]*(241-len(prices))
    return m.base.Series([{'ts':i*60,'price':p,'live':True,'quality':True,'age':0,
                           'version':'0.1.2-research','score':20,'r5':-1 if adverse else 1} for i,p in enumerate(px)])

E={'i':0,'risk':1,'stop':99,'support':99.5,'level':'prior_1h'}

class ExitTests(unittest.TestCase):
    def test_score_executes_next_snapshot(self):
        s=series([100,100.1,100.2,100.3],True)
        t=m.simulate(s,E,[30]*241,'score_trail')
        self.assertEqual(t['exit_index'],3)
        self.assertEqual(t['reason'],'score')

    def test_hard_stop_precedes_pending_signal_and_fills_gap(self):
        s=series([100,100.1,100.2,98],True)
        t=m.simulate(s,E,[30]*241,'score_trail')
        self.assertEqual(t['reason'],'stop')
        self.assertAlmostEqual(t['net_r'],-2.12)

    def test_ratchet_and_cost(self):
        s=series([100,101.5,100.4])
        t=m.simulate(s,E,[0]*241,'price_trail')
        self.assertEqual(t['exit_index'],2)
        self.assertAlmostEqual(t['net_r'],.28)

    def test_bad_quality_breaks_signal_streak(self):
        s=series([100,100.1,100.2,100.3,100.4,100.5],True)
        s.rows[2]['quality']=False
        t=m.simulate(s,E,[30]*241,'score_trail')
        self.assertEqual(t['exit_index'],5)

    def test_future_after_exit_cannot_change_trade(self):
        a=series([100,102.1,1]);b=series([100,102.1,10000])
        self.assertEqual(m.simulate(a,E,[0]*241,'fixed'),m.simulate(b,E,[0]*241,'fixed'))

if __name__=='__main__':unittest.main()
