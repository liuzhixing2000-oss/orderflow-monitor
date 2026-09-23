"""Fixed-rule paired exits; Python standard library, offline only."""
import bisect
import csv
import hashlib
import importlib.util
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

ROOT=Path(__file__).parent
spec=importlib.util.spec_from_file_location('levels',ROOT.parent/'key_levels/run.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
POLICIES=('fixed','price_trail','score_trail')

def entries(s):
    out=[];last=-float('inf');diag=Counter()
    for i in range(1,len(s.rows)-1):
        prev,r=s.rows[i-1:i+1]
        if not base.quality(prev) or not base.quality(r) or r['ts']-prev['ts']>90:continue
        if r['ts']-last<base.HOLD:continue
        lab=s.labels(i);a=lab['atr']
        if a is None:continue
        candidates=[(v,k) for k,v in lab['levels'].items() if v is not None and v<=r['price']<=v+.25*a and prev['price']>v+.25*a]
        if not candidates:continue
        v,k=max(candidates,key=lambda x:(x[0],-base.LEVELS.index(x[1])))
        nxt=s.rows[i+1];stop=v-.5*a
        if not base.quality(nxt) or nxt['ts']-r['ts']>90 or nxt['price']<=stop:
            diag['cancelled_next_entry']+=1;continue
        last=nxt['ts']
        out.append({'i':i+1,'detected':r['ts'],'support':v,'level':k,'atr':a,'stop':stop,'risk':nxt['price']-stop})
    return out,dict(diag)

def simulate(s,e,short,policy):
    i=e['i'];entry=s.px[i];risk=e['risk'];stop=e['stop'];peak=entry
    end=bisect.bisect_left(s.ts,s.ts[i]+base.HOLD)
    previous_bad=False;pending=False;path=[];signal=None
    for j in range(i+1,end+1):
        r=s.rows[j];px=s.px[j]
        # Mark the open position, including round-trip cost, at every observation.
        path.append((j,(px-entry)/risk-base.COST/100*entry/risk))
        reason=None
        if px<=stop:reason='stop'
        elif pending:reason='score'
        elif policy=='fixed' and px>=entry+2*risk:reason='target'
        elif j==end:reason='time'
        if reason:
            return {'policy':policy,'entry_utc':base.iso(s.ts[i]),'exit_utc':base.iso(s.ts[j]),
                    'entry_index':i,'exit_index':j,'level':e['level'],'support':e['support'],
                    'entry':entry,'exit':px,'initial_stop':e['stop'],'risk_price':risk,
                    'risk_pct':risk/entry*100,'net_pct':(px/entry-1)*100-base.COST,
                    'net_r':path[-1][1],'cost_r':base.COST/100*entry/risk,
                    'reason':reason,'score_signal_utc':base.iso(signal) if signal else None,
                    'hold_minutes':(s.ts[j]-s.ts[i])/60,'path':path}
        peak=max(peak,px)
        if policy!='fixed' and peak>=entry+risk:stop=max(stop,peak-risk)
        if policy=='score_trail':
            bad=bool(base.quality(r) and r['score'] is not None and short[j] is not None and short[j]>r['score'] and r['r5'] is not None and r['r5']<0)
            if bad and previous_bad and s.ts[j]-s.ts[j-1]<=90:
                pending=True;signal=s.ts[j]
            previous_bad=bad
    raise AssertionError('missing exit')

def summary(trades):
    v=[t['net_r'] for t in trades];days=defaultdict(list)
    for t in trades:days[t['entry_utc'][:10]].append(t['net_r'])
    best=max(days,key=lambda k:sum(days[k])) if days else None
    without=[t['net_r'] for t in trades if t['entry_utc'][:10]!=best]
    equity=0;peak=0;dd=0
    for t in trades:
        for _,r in t['path']:
            value=equity+r;peak=max(peak,value);dd=max(dd,peak-value)
        equity+=t['net_r']
    return {'n':len(v),'mean_r':mean(v) if v else None,'median_r':median(v) if v else None,
            'mean_pct':mean(t['net_pct'] for t in trades) if v else None,
            'win_rate':sum(x>0 for x in v)/len(v) if v else None,'sum_r':sum(v),
            'observed_mark_to_market_drawdown_r':dd,'mean_cost_r':mean(t['cost_r'] for t in trades) if v else None,
            'entry_days':len(days),'best_day':best,'mean_without_best_day_r':mean(without) if without else None,
            'reasons':dict(Counter(t['reason'] for t in trades))}

def main():
    rows=base.load_data();s=base.Series(rows)
    raw=(ROOT/'short_scores.json').read_bytes();extra=json.loads(raw)
    assert [x[0] for x in extra]==s.ts
    short=[x[1] for x in extra];ent,diag=entries(s)
    accepted=[];excluded=[]
    for e in ent:
        why=s.outcome(e['i'])['reason']
        if why:excluded.append({'entry_utc':base.iso(s.ts[e['i']]),'reason':why})
        else:accepted.append(e)
    results={p:[simulate(s,e,short,p) for e in accepted] for p in POLICIES}
    midpoint=(s.ts[0]+s.ts[-1])/2
    summaries={p:{period:summary([t for t in ts if period=='all' or (s.ts[t['entry_index']]<midpoint)==(period=='first_half')]) for period in ('all','first_half','second_half')} for p,ts in results.items()}
    pairs=[]
    for a,b in zip(results['price_trail'],results['score_trail']):
        pairs.append({'entry_utc':a['entry_utc'],'price_r':a['net_r'],'score_r':b['net_r'],
                      'difference_r':b['net_r']-a['net_r'],'earlier':b['exit_index']<a['exit_index'],
                      'price_winner_cut':a['net_r']>0 and b['net_r']<a['net_r'],
                      'price_loser_improved':a['net_r']<0 and b['net_r']>a['net_r']})
    delta=[x['difference_r'] for x in pairs];pd=defaultdict(list)
    for x in pairs:pd[x['entry_utc'][:10]].append(x['difference_r'])
    best=max(pd,key=lambda k:sum(pd[k])) if pd else None
    rest=[x['difference_r'] for x in pairs if x['entry_utc'][:10]!=best]
    paired={'mean_difference_r':mean(delta) if delta else None,'improved':sum(x>1e-12 for x in delta),
            'worsened':sum(x< -1e-12 for x in delta),'unchanged':sum(abs(x)<=1e-12 for x in delta),
            'price_winners_cut':sum(x['price_winner_cut'] for x in pairs),'price_losers_improved':sum(x['price_loser_improved'] for x in pairs),
            'best_increment_day':best,'mean_difference_without_best_increment_day_r':mean(rest) if rest else None}
    report={'recorded':len(ent),'evaluated':len(accepted),'excluded':excluded,'entry_diagnostics':diag,
            'short_scores_sha256':hashlib.sha256(raw).hexdigest(),'protocol_sha256':hashlib.sha256((ROOT/'PROTOCOL.md').read_bytes()).hexdigest(),
            'data_manifest':json.loads((ROOT.parent/'key_levels/manifest.json').read_text()),'summary':summaries,'paired':paired}
    (ROOT/'results.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    for name,items in [('trades.csv',[{k:v for k,v in t.items() if k!='path'} for ts in results.values() for t in ts]),('paired.csv',pairs)]:
        if items:
            with (ROOT/name).open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(items[0]));w.writeheader();w.writerows(items)
    print(json.dumps({k:v for k,v in report.items() if k!='data_manifest'},indent=2))

if __name__=='__main__':main()
