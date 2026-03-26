"""Temporary test script — investigate CLOB prices-history endpoint."""
import httpx
import json
import time

r = httpx.get('https://gamma-api.polymarket.com/markets', params={
    'limit': 10, 'closed': 'false', 'volume_num_min': 50000
})
markets = r.json()
print('Active markets:', len(markets))
for m in markets[:3]:
    ids = json.loads(m.get('clobTokenIds', '[]'))
    tok = ids[0][:20] if ids else 'N/A'
    print(f"  {m['question'][:50]} | vol: {m.get('volumeNum',0):.0f} | token: {tok}")

if markets:
    m = markets[0]
    ids = json.loads(m.get('clobTokenIds', '[]'))
    token = ids[0]
    end_ts = int(time.time())
    start_ts = end_ts - 3 * 24 * 3600  # 3 days

    r2 = httpx.get('https://clob.polymarket.com/prices-history', params={
        'market': token, 'fidelity': 60, 'startTs': start_ts, 'endTs': end_ts
    })
    print('prices-history 3d fidelity=60 status:', r2.status_code)
    history = r2.json().get('history', [])
    print('Price points:', len(history))
    if history:
        print('First:', history[0])
        print('Last:', history[-1])
    else:
        # Try interval approach
        r3 = httpx.get('https://clob.polymarket.com/prices-history', params={
            'market': token, 'interval': '1d'
        })
        print('interval=1d status:', r3.status_code, r3.json().get('history', [])[:2])
