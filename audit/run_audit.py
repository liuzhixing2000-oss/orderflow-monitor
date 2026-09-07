"""Reproduce and audit frozen log-recovered Sample A. No network or live writes."""
from __future__ import annotations

import ast
import bisect
import csv
import gzip
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'vendor'))
import legacy_replay_backtest as legacy


def dump(path, obj):
    path.write_text(json.dumps(obj, indent=2, allow_nan=False) + '\n')


def write_csv(path, rows):
    if not rows:
        path.write_text('')
        return
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def summary(values):
    a = [float(v) for v in values if v is not None]
    if not a:
        return dict(n=0, mean=None, median=None, win_rate=None, pf=None)
    pos, neg = sum(v for v in a if v > 0), -sum(v for v in a if v < 0)
    return dict(n=len(a), mean=float(np.mean(a)), median=float(np.median(a)),
                win_rate=sum(v > 0 for v in a) / len(a), pf=pos / neg if neg else None)


def live_functions():
    """Extract unchanged pure functions, avoiding app startup/config dependencies."""
    tree = ast.parse((HERE.parent / 'research_models.py').read_text())
    names = {'_clamp', '_regime', '_continuation_side'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    code = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + nodes, type_ignores=[])
    ns = {}
    exec(compile(ast.fix_missing_locations(code), 'frozen_pure_model_functions', 'exec'), ns)
    return ns


def price_gate(stats, side):
    sign = 1 if side == 'long' else -1
    return all(stats[k]['return_pct'] is not None and sign * stats[k]['return_pct'] >= floor
               for k, floor in [('1h', .05), ('4h', .15)])


def features(stats, rv):
    return [stats[k]['return_pct'] for k in ['5m', '15m', '1h', '4h']] + [stats[k]['efficiency'] for k in ['1h', '4h']] + [rv]


def compute_rows(records):
    ts = [r['timestamp'] for r in records]
    px = [r['price'] for r in records]
    out = []
    for i, r in enumerate(records):
        stats = {k: legacy.calc_price_stats(ts, px, i, s) for k, s in [('1m', 60), ('5m', 300), ('15m', 900), ('1h', 3600), ('4h', 14400)]}
        nd = {k: legacy.normalized_delta(r.get('trade_flow', {}).get(k)) for k in ['1m', '5m', '15m']}
        quality = (r.get('feed_status') == 'live' and stats['1h']['span_seconds'] >= 3420
                   and stats['4h']['span_seconds'] >= 13680 and stats['1h']['samples'] >= 45
                   and stats['4h']['samples'] >= 180)
        j = bisect.bisect_left(ts, ts[i] - 3600)
        rv = math.sqrt(sum((math.log(px[k] / px[k-1]) * 100) ** 2 for k in range(j+1, i+1)))
        out.append(dict(i=i, ts=ts[i], price=px[i], stats=stats, nd=nd, oi=r.get('open_interest') or {},
                        quality=quality, age=r.get('last_update_age_seconds'), x=features(stats, rv),
                        hash=hashlib.sha256(json.dumps(r, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
    return out


def score_row(row, side, variant):
    nd = row['nd'] if variant not in ('no_delta', 'no_delta_oi') else dict.fromkeys(row['nd'])
    oi = row['oi'] if variant not in ('no_oi', 'no_delta_oi') else {}
    regime = legacy.classify_regime(row['stats'], nd)
    score = legacy.continuation_score(row['stats'], nd, oi, regime, side)
    eligible = row['quality'] and regime['label'] == ('TREND_UP' if side == 'long' else 'TREND_DOWN')
    return score, eligible, regime


def events_for(rows, side, threshold, variant='full'):
    last = None
    previous = None
    result = []
    for r in rows:
        score, eligible, regime = score_row(r, side, variant)
        if previous and eligible and previous[1] and previous[0] < threshold <= score and (last is None or r['ts'] - last >= 14400):
            result.append(dict(row=r, score=score, regime=regime['label']))
            last = r['ts']
        previous = (score, eligible)
    return result


def evaluate(rows, event, horizon, side):
    ts = [r['ts'] for r in rows]
    target = event['ts'] + horizon * 60
    j = bisect.bisect_left(ts, target, event['i'] + 1)
    if j >= len(rows) or rows[j]['ts'] > target + 180:
        return dict(net=None, exit_ts=None, holding_minutes=None, max_gap=None, mfe=None, mae=None, quality=False)
    sign = 1 if side == 'long' else -1
    path = rows[event['i']:j+1]
    returns = [sign * (r['price'] / event['price'] - 1) * 100 for r in path]
    gap = max((b['ts'] - a['ts'] for a, b in zip(path, path[1:])), default=0)
    return dict(net=returns[-1] - .12, exit_ts=rows[j]['ts'], holding_minutes=(rows[j]['ts'] - event['ts'])/60,
                max_gap=gap, mfe=max(returns), mae=min(returns), quality=gap <= 90)


def sensitivity(rows):
    vals = [x for x in rows if x['net'] is not None]
    result = {'overall': summary([x['net'] for x in vals])}
    result['leave_one_event_out'] = [dict(omitted=x['event_id'], **summary([y['net'] for y in vals if y['event_id'] != x['event_id']])) for x in vals]
    for key in ('day', 'symbol'):
        result['leave_one_' + key + '_out'] = [dict(omitted=k, **summary([x['net'] for x in vals if x[key] != k])) for k in sorted({x[key] for x in vals})]
    return result


def match_controls(rows, event, side, horizon=240):
    eligible = [r for r in rows if r['quality'] and price_gate(r['stats'], side) and r['ts'] < event['ts'] and all(v is not None for v in r['x'])]
    if len(eligible) < 30:
        return [], 'INSUFFICIENT_PAST_SCALING_ROWS'
    past = np.asarray([r['x'] for r in eligible])
    med = np.median(past, axis=0)
    scale = 1.4826 * np.median(np.abs(past - med), axis=0)
    scale = np.where(scale > 1e-10, scale, np.std(past, axis=0))
    scale = np.where(scale > 1e-10, scale, 1e-10)
    target = np.asarray(event['x'])
    candidates = []
    for r in eligible:
        if not event['ts'] - 48*3600 <= r['ts'] <= event['ts'] - horizon*60:
            continue
        z = np.abs((np.asarray(r['x']) - target) / scale)
        distance = float(np.sqrt(np.mean(z*z)))
        if np.max(z) > 2 or distance > 1:
            continue
        outcome = evaluate(rows, r, horizon, side)
        if outcome['net'] is None or not outcome['quality'] or outcome['exit_ts'] > event['ts']:
            continue
        candidates.append(dict(row=r, distance=distance, net=outcome['net'], exit_ts=outcome['exit_ts']))
    chosen = []
    for c in sorted(candidates, key=lambda x:(x['distance'], x['row']['ts'])):
        if all(abs(c['row']['ts'] - p['row']['ts']) >= 14400 for p in chosen):
            chosen.append(c)
            if len(chosen) == 5:
                break
    return chosen, 'OK' if chosen else 'NO_CONTROL_WITHIN_FIXED_CALIPER'


def btc_attribution(derived, ledger):
    def past_price(symbol, target):
        rows = derived[symbol]
        j = bisect.bisect_right([r['ts'] for r in rows], target) - 1
        if j < 0 or target - rows[j]['ts'] > 90:
            return None
        return rows[j]['price']
    result=[]
    for event in ledger:
        if event['symbol']=='BTCUSDT' or event['net'] is None:
            continue
        end = (math.ceil(event['entry_ts']/900)-1)*900
        pairs=[]
        for t in range(int(end-95*900), int(end)+1, 900):
            p0,p1=past_price(event['symbol'],t-900),past_price(event['symbol'],t)
            b0,b1=past_price('BTCUSDT',t-900),past_price('BTCUSDT',t)
            if all(x is not None for x in (p0,p1,b0,b1)):
                pairs.append(((b1/b0-1)*100,(p1/p0-1)*100))
        beta=None
        if len(pairs)>=32:
            a=np.asarray(pairs)
            variance=float(np.var(a[:,0]))
            if variance>1e-12:
                beta=float(np.mean((a[:,0]-np.mean(a[:,0]))*(a[:,1]-np.mean(a[:,1])))/variance)
        b0,b1=past_price('BTCUSDT',event['entry_ts']),past_price('BTCUSDT',event['exit_ts'])
        bg=(1 if event['side']=='long' else -1)*(b1/b0-1)*100 if b0 and b1 else None
        result.append(dict(event_id=event['event_id'],symbol=event['symbol'],side=event['side'],threshold=event['threshold'],
                           horizon=event['horizon'],day=event['day'],net=event['net'],quality=event['quality'],
                           beta_samples=len(pairs),beta=beta,btc_directional_gross=bg,
                           residual_after_signal_cost=event['net']-beta*bg if beta is not None and bg is not None else None))
    return result


def main():
    output = HERE / 'results'
    output.mkdir(exist_ok=True)
    manifest = json.loads((HERE/'evidence/manifest.json').read_text())
    raw = gzip.decompress((HERE/'evidence/recovered_sample_a.jsonl.gz').read_bytes())
    assert hashlib.sha256(raw).hexdigest() == manifest['canonical_jsonl_sha256'], 'Dataset hash mismatch'
    groups = defaultdict(list)
    skipped = Counter()
    for line in raw.decode().splitlines():
        r = json.loads(line)['payload']
        # Match Storage.insert: startup snapshots with no price were logged,
        # but never inserted into the source SQLite table.
        if not r.get('price'):
            skipped[r['symbol']] += 1
            continue
        groups[r['symbol']].append(r)
    derived = {s: compute_rows(rs) for s, rs in groups.items()}
    live = live_functions()
    diag = {}
    ledger, aggregates, ablated, baselines, matches, controls = [], [], [], [], [], []
    for symbol, rows in derived.items():
        mismatch = 0
        rounding_mismatch = 0
        for r in rows:
            f = dict(normalized_delta=r['nd'], price_stats=r['stats'], open_interest=r['oi'], order_book={})
            regime = live['_regime'](f)
            for side in ('long', 'short'):
                ls = live['_continuation_side'](f, regime, side)['score']
                rs, _, rr = score_row(r, side, 'full')
                mismatch += int(ls != rs or regime['label'] != rr['label'])
                rounded_stats = {k:{**v, 'return_pct':round(v['return_pct'],5) if v['return_pct'] is not None else None,
                                     'efficiency':round(v['efficiency'],4) if v['efficiency'] is not None else None} for k,v in r['stats'].items()}
                rounded_features = {**f, 'price_stats':rounded_stats,
                                    'normalized_delta':{k:round(v,4) if v is not None else None for k,v in r['nd'].items()}}
                rounded_regime=live['_regime'](rounded_features)
                rounded_score=live['_continuation_side'](rounded_features,rounded_regime,side)['score']
                rounding_mismatch += int(rounded_score != rs or rounded_regime['label'] != rr['label'])
        quality_rows = [r for r in rows if r['quality']]
        diag[symbol] = dict(rows=len(rows), eligible_rows=len(quality_rows),
                            logged_but_not_stored_missing_price=skipped[symbol],
                            missing_1m_price_return=sum(r['stats']['1m']['return_pct'] is None for r in quality_rows),
                            eligible_stale_age_gt5=sum(r['age'] is None or r['age'] > 5 for r in quality_rows),
                            same_input_no_book_formula_mismatches=mismatch,
                            live_rounding_on_legacy_inputs_mismatches=rounding_mismatch,
                            first_eligible_utc=iso(quality_rows[0]['ts']) if quality_rows else None,
                            genuine_10s_sampling_parity='UNAVAILABLE_IN_LEGACY_DATA')
        for side in ('long', 'short'):
            # Fully price-only first-entry baseline, unchanged 4h cooldown.
            previous = False
            last = None
            pe = []
            for r in rows:
                eligible = r['quality'] and price_gate(r['stats'], side)
                if eligible and not previous and (last is None or r['ts'] - last >= 14400):
                    pe.append(r)
                    last = r['ts']
                previous = eligible
            for horizon in legacy.HORIZONS_MIN:
                outcomes = [evaluate(rows, e, horizon, side) for e in pe]
                baselines.append(dict(symbol=symbol, side=side, horizon=horizon, recorded=len(pe), **summary([x['net'] for x in outcomes])))
            for threshold in legacy.THRESHOLDS:
                for variant in ('full', 'no_delta', 'no_oi', 'no_delta_oi'):
                    events = events_for(rows, side, threshold, variant)
                    for horizon in legacy.HORIZONS_MIN:
                        vals = []
                        qualified = []
                        for event in events:
                            r = event['row']
                            o = evaluate(rows, r, horizon, side)
                            vals.append(o['net'])
                            if o['quality']:
                                qualified.append(o['net'])
                            if variant == 'full':
                                eid = f'{symbol}:{side}:{threshold}:{r["ts"]:.6f}'
                                item = dict(event_id=eid, symbol=symbol, side=side, threshold=threshold, score=event['score'],
                                            entry_ts=r['ts'], entry_utc=iso(r['ts']), day=iso(r['ts'])[:10], entry_price=r['price'],
                                            source_payload_sha256=r['hash'], horizon=horizon, **o)
                                item.update({f'return_{k}_pct':r['stats'][k]['return_pct'] for k in ('1m','5m','15m','1h','4h')})
                                item.update({f'delta_norm_{k}':r['nd'][k] for k in ('1m','5m','15m')})
                                item.update(oi_change_5m_pct=r['oi'].get('change_5m_pct'),
                                            efficiency_1h=r['stats']['1h']['efficiency'],efficiency_4h=r['stats']['4h']['efficiency'])
                                ledger.append(item)
                                if horizon == 240:
                                    for av in ('no_delta', 'no_oi', 'no_delta_oi'):
                                        ss, ee, rr = score_row(r, side, av)
                                        ablated.append(dict(event_id=eid, variant=av, original_score=event['score'], ablated_score=ss,
                                                            eligible=ee, regime=rr['label'], passes_threshold=ee and ss>=threshold))
                                    # Matching is reported across all original thresholds; none selected by result.
                                    chosen, reason = match_controls(rows, r, side)
                                    control_mean = float(np.mean([x['net'] for x in chosen])) if chosen else None
                                    matches.append(dict(event_id=eid, symbol=symbol, side=side, threshold=threshold,
                                                        day=item['day'], signal_net=o['net'], signal_quality=o['quality'],
                                                        n_controls=len(chosen), control_mean=control_mean,
                                                        difference=o['net']-control_mean if control_mean is not None and o['net'] is not None and o['quality'] else None,
                                                        status=reason))
                                    for c in chosen:
                                        controls.append(dict(event_id=eid, symbol=symbol, control_ts=c['row']['ts'],
                                                             control_utc=iso(c['row']['ts']), control_exit_ts=c['exit_ts'],
                                                             distance=c['distance'], net=c['net']))
                        aggregates.append(dict(symbol=symbol, side=side, threshold=threshold, variant=variant, horizon=horizon,
                                               recorded=len(events), missing_exits=sum(x is None for x in vals),
                                               gap_qualified_n=len(qualified), gap_qualified_mean=float(np.mean(qualified)) if qualified else None,
                                               **summary(vals)))
    write_csv(output/'events.csv', ledger)
    write_csv(output/'btc_beta_attribution.csv', btc_attribution(derived, ledger))
    write_csv(output/'aggregate_results.csv', aggregates)
    write_csv(output/'component_ablation_at_original_events.csv', ablated)
    write_csv(output/'price_only_baselines.csv', baselines)
    write_csv(output/'matched_controls.csv', controls)
    write_csv(output/'matched_differences.csv', matches)
    dump(output/'feature_diagnostics.json', diag)
    focus = [x for x in ledger if x['side']=='long' and x['threshold']==70 and x['horizon']==240]
    dump(output/'sensitivity.json', {'pooled_long70_240':sensitivity(focus), 'ETH_long70_240':sensitivity([x for x in focus if x['symbol']=='ETHUSDT'])})
    matched_summary = []
    for symbol in [*derived, 'POOLED']:
        for side in ('long', 'short'):
            for threshold in legacy.THRESHOLDS:
                sub = [x for x in matches if (symbol=='POOLED' or x['symbol']==symbol) and x['side']==side and x['threshold']==threshold]
                matched_summary.append(dict(symbol=symbol, side=side, threshold=threshold, signals=len(sub),
                                            matched=sum(x['difference'] is not None for x in sub),
                                            mean_difference=summary([x['difference'] for x in sub])['mean'],
                                            dates=len({x['day'] for x in sub if x['difference'] is not None}),
                                            confidence_interval=None, inference='DESCRIPTIVE_ONLY_FEW_DAYS_POST_SELECTION'))
    dump(output/'matched_summary.json', matched_summary)
    # Compare exact original report cells; A-v2 is deliberately not the reference.
    comparisons = []
    for line in (HERE/'evidence/original_replay.jsonl').read_text().splitlines():
        m=json.loads(line)['message']
        if not m.startswith('LEGACY_REPLAY_RESULT '):
            continue
        original=json.loads(m.split(' ',1)[1])
        for band in original['thresholds']:
            actual=next(x for x in aggregates if x['symbol']==original['symbol'] and x['side']==original['side'] and x['horizon']==original['horizon_min'] and x['threshold']==band['threshold'] and x['variant']=='full')
            match=band['n']==actual['n'] and (band['avg_net'] is None and actual['mean'] is None or band['avg_net'] is not None and actual['mean'] is not None and abs(band['avg_net']-actual['mean'])<=.000011)
            comparisons.append(dict(symbol=original['symbol'], side=original['side'], threshold=band['threshold'], horizon=original['horizon_min'],
                                    reported_n=band['n'], recovered_n=actual['n'], reported_mean=band['avg_net'], recovered_mean=actual['mean'], agrees=bool(match)))
    write_csv(output/'original_report_reconciliation.csv', comparisons)
    dump(output/'run_manifest.json', dict(dataset_sha256=manifest['canonical_jsonl_sha256'],
                                         source_sha256={str(p.relative_to(HERE.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__), HERE/'PROTOCOL.md', HERE/'vendor/legacy_replay_backtest.py', HERE.parent/'research_models.py']},
                                         reconciliation_cells=len(comparisons), reconciliation_agree=sum(x['agrees'] for x in comparisons),
                                         primary_sample='RECOVERED_SUBSET_OF_EXPLORATORY_A', confirmation=False))
    print(json.dumps({'diagnostics':diag, 'primary':sensitivity([x for x in focus if x['symbol']=='ETHUSDT']),
                       'matching':[x for x in matched_summary if x['side']=='long' and x['threshold']==70],
                       'reconciliation_agree':sum(x['agrees'] for x in comparisons), 'cells':len(comparisons)}, indent=2))


if __name__ == '__main__':
    main()
