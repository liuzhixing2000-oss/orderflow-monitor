"""Read-only export of the stopped Sample B; no remote mutations."""
import concurrent.futures
import gzip
import hashlib
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
BASE = 'https://orderflow-monitor-research-v01-production.up.railway.app'

def fetch(after):
    url = f'{BASE}/research/sample-b/export?after_id={after}&limit=500'
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=45) as response:
                data = json.load(response)
            return data
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)

def compact(s, source):
    return {'timestamp': s['timestamp'], 'price': s.get('price'),
            'symbol': s['symbol'], 'feed_status': s.get('feed_status'),
            'age': s.get('last_update_age_seconds'),
            'stats': s.get('price_stats', {}),
            'models': {k: s.get('models', {}).get(k) for k in ('version', 'data_quality', 'regime', 'continuation')},
            'source': source,
            'snapshot_hash': hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest()}

if __name__ == '__main__':
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(fetch, range(0, 8262, 500)):
            for r in result['rows']:
                s = json.loads(r['payload'])['snapshot']
                rows.append(compact(s, 'sample_b_export'))
            print('exported', len(rows), flush=True)
    rows.sort(key=lambda s:s['timestamp'])
    raw = ''.join(json.dumps(s, separators=(',', ':'))+'\n' for s in rows).encode()
    (ROOT/'sample_b_snapshots.jsonl.gz').write_bytes(gzip.compress(raw, mtime=0))
    print(json.dumps({'n':len(rows), 'first':rows[0]['timestamp'], 'last':rows[-1]['timestamp'], 'sha256':hashlib.sha256(raw).hexdigest()}))
