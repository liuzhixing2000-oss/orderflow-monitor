"""Frozen, stdlib-only, causal support-context backtest. Never imports live app."""
from __future__ import annotations
import bisect
import csv
import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).parent
CUTOFF = datetime(2026,9,23,2,19,tzinfo=timezone.utc).timestamp()
HOLD = 14400
COST = .12
LEVELS = ('prior_day', 'prior_1h', 'prior_4h')

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

def write_json(name, obj):
    (ROOT/name).write_text(json.dumps(obj, indent=2, allow_nan=False)+'\n')

def write_csv(name, rows):
    if not rows:
        return
    with (ROOT/name).open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

def canonicalize(r):
    m = r['models']
    return {'ts':r['timestamp'], 'price':r['price'], 'symbol':r['symbol'],
            'live':r.get('feed_status')=='live', 'age':r.get('age'),
            'quality':(m.get('data_quality') or {}).get('state')=='OK' and (m.get('data_quality') or {}).get('eligible', True),
            'version':m['version'], 'score':(m.get('continuation') or {}).get('long'),
            'regime':(m.get('regime') or {}).get('label'),
            'r5':(r['stats'].get('5m') or {}).get('return_pct'),
            'r1h':(r['stats'].get('1h') or {}).get('return_pct'),
            'r4h':(r['stats'].get('4h') or {}).get('return_pct'),
            'source':r['source']}

def load_data():
    frozen = ROOT/'inputs.jsonl.gz'
    if frozen.exists():
        with gzip.open(frozen,'rt') as f:
            return [json.loads(line) for line in f if line.strip()]
    rows = []
    with gzip.open(ROOT/'sample_b_snapshots.jsonl.gz', 'rt') as f:
        rows.extend(json.loads(line) for line in f if line.strip())
    for p in sorted((ROOT/'logs').glob('*.jsonl')):
        rows.extend(json.loads(line) for line in p.read_text().splitlines() if line.strip())
    unique = {}
    for record in rows:
        r = canonicalize(record)
        if r['symbol']!='ETHUSDT' or r['ts']>CUTOFF or not r['price'] or r['price']<=0:
            continue
        if r['ts'] in unique:
            a, b = dict(unique[r['ts']]), dict(r)
            a.pop('source'); b.pop('source')
            assert a==b, ('conflicting duplicate', r['ts'])
        unique[r['ts']] = r
    data = sorted(unique.values(), key=lambda r:r['ts'])
    raw = ''.join(json.dumps(r,sort_keys=True,separators=(',',':'))+'\n' for r in data).encode()
    frozen.write_bytes(gzip.compress(raw,mtime=0))
    return data

class Series:
    def __init__(self, rows):
        self.rows=rows
        self.ts=[r['ts'] for r in rows]
        self.px=[r['price'] for r in rows]
        self.atr_cache={}
        self.day_cache={}

    def window(self,start,end,inclusive=False,span=True):
        left=bisect.bisect_left(self.ts,start)
        right=(bisect.bisect_right if inclusive else bisect.bisect_left)(self.ts,end)
        t=self.ts[left:right]
        if not t or len(t)<math.ceil(.9*(end-start)/60):
            return None
        if span and t[-1]-t[0]<.95*(end-start):
            return None
        boundaries=[start]+t+[end]
        if max(b-a for a,b in zip(boundaries,boundaries[1:]))>180:
            return None
        return left,right

    def atr(self,ts):
        boundary=math.floor(ts/900)*900
        if boundary in self.atr_cache:
            return self.atr_cache[boundary]
        trs=[]
        for end in range(boundary-13*900,boundary+1,900):
            start=end-900
            w=self.window(start,end,span=False)
            j=bisect.bisect_left(self.ts,start)-1
            if w is None or j<0 or start-self.ts[j]>90:
                self.atr_cache[boundary]=None
                return None
            p=self.px[w[0]:w[1]]
            trs.append(max(max(p)-min(p),abs(max(p)-self.px[j]),abs(min(p)-self.px[j])))
        a=mean(trs)
        self.atr_cache[boundary]=a if a>0 else None
        return self.atr_cache[boundary]

    def labels(self,i):
        r=self.rows[i]; ts=r['ts']; atr=self.atr(ts)
        reaction=self.window(ts-900,ts,inclusive=True)
        out={name:None for name in LEVELS}
        values={name:None for name in LEVELS}
        if atr is None or reaction is None:
            return {'flags':out,'levels':values,'atr':atr,'reaction_low':None}
        low=min(self.px[reaction[0]:reaction[1]])
        day=math.floor(ts/86400)*86400
        intervals={'prior_day':(day-86400,day),'prior_1h':(ts-4500,ts-900),'prior_4h':(ts-15300,ts-900)}
        for name,(start,end) in intervals.items():
            if name=='prior_day' and day in self.day_cache:
                level=self.day_cache[day]
            else:
                w=self.window(start,end)
                level=min(self.px[w[0]:w[1]]) if w else None
                if name=='prior_day':self.day_cache[day]=level
            values[name]=level
            if level is not None:
                out[name]=bool(level-.25*atr<=low<=level+.25*atr and level<=r['price']<=level+.5*atr and r['r5'] is not None and r['r5']>0)
        return {'flags':out,'levels':values,'atr':atr,'reaction_low':low}

    def outcome(self,i):
        ts=self.ts[i];target=ts+HOLD
        j=bisect.bisect_left(self.ts,target)
        if j==len(self.ts):return {'reason':'IMMATURE_OR_MISSING_EXIT','net':None}
        if self.ts[j]-target>180:return {'reason':'LATE_EXIT','net':None}
        k=bisect.bisect_right(self.ts,target)
        t=self.ts[i:k]
        gaps=[ts]+t+[target]
        if len(t)<.9*HOLD/60 or max(b-a for a,b in zip(gaps,gaps[1:]))>180:
            return {'reason':'PATH_GAP_OR_LOW_COVERAGE','net':None}
        prices=self.px[i:k]+[self.px[j]]
        return {'reason':None,'net':(self.px[j]/self.px[i]-1)*100-COST,
                'exit_ts':self.ts[j],'holding_seconds':self.ts[j]-ts,
                'mae':(min(prices)/self.px[i]-1)*100,'mfe':(max(prices)/self.px[i]-1)*100}

def quality(r):
    return bool(r['live'] and r['quality'] and r['age'] is not None and 0<=r['age']<=5 and r['version']=='0.1.2-research')

def crossings(series,threshold):
    out=[];last=None
    for i in range(1,len(series.rows)):
        a,b=series.rows[i-1:i+1]
        if (quality(a) and quality(b) and a['regime']==b['regime']=='TREND_UP'
            and a['score'] is not None and b['score'] is not None
            and 0<b['ts']-a['ts']<=90 and a['score']<threshold<=b['score']
            and (last is None or b['ts']-last>=HOLD)):
            out.append(i);last=b['ts']
    return out

def summarize(events):
    good=[e for e in events if e['net'] is not None]
    v=[e['net'] for e in good]
    pos=sum(x for x in v if x>0);neg=-sum(x for x in v if x<0)
    days=defaultdict(list)
    for e in good:days[e['entry_utc'][:10]].append(e['net'])
    best=max(days,key=lambda d:sum(days[d])) if days else None
    without=[e['net'] for e in good if e['entry_utc'][:10]!=best]
    return {'recorded':len(events),'evaluated':len(v),'excluded':len(events)-len(v),
            'mean_pct':mean(v) if v else None,'median_pct':median(v) if v else None,
            'win_rate':sum(x>0 for x in v)/len(v) if v else None,'profit_factor':pos/neg if neg else None,
            'sum_equal_notional_pct':sum(v),'avg_mae_pct':mean(e['mae'] for e in good) if good else None,
            'avg_mfe_pct':mean(e['mfe'] for e in good) if good else None,
            'contributing_days':len(days),'best_day':best,'mean_without_best_day_pct':mean(without) if without else None}

def main():
    rows=load_data();s=Series(rows)
    assert len({r['ts'] for r in rows})==len(rows)
    all_events=[]; summaries=[];diagnostics={}
    labels=[s.labels(i) for i in range(len(rows))]
    midpoint=(s.ts[0]+s.ts[-1])/2
    def event(i,policy,threshold,level):
        r=rows[i]; lab=labels[i];o=s.outcome(i)
        return {'policy':policy,'threshold':threshold,'level_type':level,'entry_utc':iso(r['ts']),
                'ts':r['ts'],'price':r['price'],'score':r['score'],
                'atr_proxy':lab['atr'],'support':lab['levels'].get(level),
                'reaction_low':lab['reaction_low'], 'flags':json.dumps(lab['flags'],sort_keys=True),
                'net':o['net'],'exclusion':o['reason'],'mae':o.get('mae'),'mfe':o.get('mfe'),
                'exit_utc':iso(o['exit_ts']) if o.get('exit_ts') else None,'holding_seconds':o.get('holding_seconds')}
    def add(policy,threshold,level,indices):
        events=[event(i,policy,threshold,level) for i in indices];all_events.extend(events)
        for period in ('all','first_half','second_half'):
            subset=events if period=='all' else [e for e in events if (e['ts']<midpoint)==(period=='first_half')]
            summaries.append({'policy':policy,'threshold':threshold,'level_type':level,'period':period,**summarize(subset)})
    for threshold in (50,60,70):
        indices=crossings(s,threshold)
        add('score_only',threshold,'none',indices)
        diag={}
        for name in LEVELS+('any',):
            def known(i):
                f=labels[i]['flags']
                return (any(v is True for v in f.values()) or all(v is not None for v in f.values())) if name=='any' else f[name] is not None
            def passed(i):
                f=labels[i]['flags']
                return any(v is True for v in f.values()) if name=='any' else f[name] is True
            eligible=[i for i in indices if known(i)]
            kept=[i for i in eligible if passed(i)]
            removed=[i for i in eligible if not passed(i)]
            add('score_context_available',threshold,name,eligible)
            add('score_plus_level',threshold,name,kept)
            add('score_rejected_by_level',threshold,name,removed)
            diag[name]={'unknown_context':len(indices)-len(eligible),'kept':len(kept),'removed':len(removed),
                        'removed_winners':sum(s.outcome(i)['net'] is not None and s.outcome(i)['net']>0 for i in removed),
                        'removed_losers':sum(s.outcome(i)['net'] is not None and s.outcome(i)['net']<=0 for i in removed)}
        diagnostics[str(threshold)]=diag
    for name in LEVELS+('any',):
        indices=[];last=None
        for i,r in enumerate(rows):
            f=labels[i]['flags'];level_ok=any(x is True for x in f.values()) if name=='any' else f[name] is True
            if (quality(r) and level_ok and r['r1h'] is not None and r['r4h'] is not None and r['r1h']>=.05 and r['r4h']>=.15
                and (last is None or r['ts']-last>=HOLD)):
                indices.append(i);last=r['ts']
        add('price_level_only',None,name,indices)
    write_csv('summary.csv',summaries);write_csv('events.csv',all_events)
    write_json('diagnostics.json',diagnostics)
    gaps=[{'after':iso(a),'before':iso(b),'seconds':b-a} for a,b in zip(s.ts,s.ts[1:]) if b-a>180]
    raw=gzip.decompress((ROOT/'inputs.jsonl.gz').read_bytes())
    meta={'n':len(rows),'first':iso(s.ts[0]),'last':iso(s.ts[-1]),'midpoint':iso(midpoint),
          'sha256_uncompressed':hashlib.sha256(raw).hexdigest(),'versions':dict(Counter(r['version'] for r in rows)),
          'sources':dict(Counter(r['source'] for r in rows)),'gaps_over_180s':gaps,
          'context_available':{k:sum(l['flags'][k] is not None for l in labels) for k in LEVELS},
          'context_pass':{k:sum(l['flags'][k] is True for l in labels) for k in LEVELS},
          'note':'Recovered subset, not full SQLite export; minute-sampled levels and outcomes; exploratory.'}
    write_json('manifest.json',meta)
    print(json.dumps(meta,indent=2))
    print(json.dumps([r for r in summaries if r['period']=='all' and (r['level_type'] in ('any','none'))],indent=2))

if __name__=='__main__':main()
