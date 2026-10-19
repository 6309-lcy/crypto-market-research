#!/usr/bin/env python3
"""Download public September 2026 perpetual funding and hourly trade candles.

Run with Python 3.10+ and requests installed. Outputs go beside this script.
All rates are raw per-settlement rates; volumes retain venue-native units.
"""
import csv
import hashlib
import json
import math
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

START = 1788220800000
END = 1790812800000
HOUR = 3600000
BASES = 'ADA ARB ATOM AVAX BNB BTC DOGE DOT ETH LINK LTC OP SOL UNI XRP'.split()
VENUES = ['binance', 'bybit', 'okx', 'hyperliquid']
ROOT = Path(__file__).resolve().parent
FUNDING_FIELDS = ['exchange', 'base', 'symbol', 'settlement_time', 'funding_rate',
                  'settlement_mark_price', 'source_endpoint']
CANDLE_FIELDS = ['exchange', 'base', 'symbol', 'open_time', 'open', 'high', 'low',
                 'close', 'volume', 'mark_open', 'mark_source']
API = {'binance': 'https://fapi.binance.com', 'bybit': 'https://api.bybit.com',
       'okx': 'https://www.okx.com', 'hyperliquid': 'https://api.hyperliquid.xyz'}
LOCK = threading.Lock()
NEXT_REQUEST = 0.0
REQUESTS = []


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def request(venue, endpoint, params, post=False):
    global NEXT_REQUEST
    for attempt in range(1, 5):
        with LOCK:
            delay = NEXT_REQUEST - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            NEXT_REQUEST = time.monotonic() + 0.25
        audit = {'exchange': venue, 'endpoint': API[venue] + endpoint,
                 'method': 'POST' if post else 'GET', 'parameters': params.copy(),
                 'requested_at': now(), 'attempt': attempt}
        try:
            response = requests.request('POST' if post else 'GET', API[venue] + endpoint,
                                        json=params if post else None,
                                        params=None if post else params, timeout=(15, 45))
            audit.update(status_code=response.status_code,
                         response_sha256=sha(response.content), received_at=now())
            response.raise_for_status()
            body = response.json()
            if venue == 'bybit' and body.get('retCode') != 0:
                raise ValueError('Bybit API: ' + str(body.get('retCode')) + ' ' + str(body.get('retMsg')))
            if venue == 'okx' and body.get('code') != '0':
                raise ValueError('OKX API: ' + str(body.get('code')) + ' ' + str(body.get('msg')))
            with LOCK:
                REQUESTS.append(audit)
            return body
        except Exception as exc:
            audit.update(error=str(exc), received_at=now())
            with LOCK:
                REQUESTS.append(audit)
            if attempt == 4:
                raise RuntimeError(str(exc)) from exc
            time.sleep(min(2 ** (attempt - 1), 8))


def fetch_funding(venue, base, symbol):
    rows = []
    if venue == 'binance':
        endpoint = '/fapi/v1/fundingRate'
        cursor = START
        while cursor < END:
            page = request(venue, endpoint, dict(symbol=symbol, startTime=cursor, endTime=END - 1, limit=1000))
            if not page:
                break
            rows.extend((int(x['fundingTime']), x['fundingRate'], x.get('markPrice', '')) for x in page)
            nxt = max(int(x['fundingTime']) for x in page) + 1
            if nxt <= cursor:
                raise ValueError('Funding pagination failed to advance')
            cursor = nxt
            if len(page) < 1000:
                break
    elif venue == 'bybit':
        endpoint = '/v5/market/funding/history'
        cursor = END - 1
        while cursor >= START:
            page = request(venue, endpoint, dict(category='linear', symbol=symbol, startTime=START, endTime=cursor, limit=200))['result']['list']
            if not page:
                break
            rows.extend((int(x['fundingRateTimestamp']), x['fundingRate'], '') for x in page)
            nxt = min(int(x['fundingRateTimestamp']) for x in page) - 1
            if nxt >= cursor:
                raise ValueError('Funding pagination failed to advance')
            cursor = nxt
            if len(page) < 200:
                break
    elif venue == 'okx':
        endpoint = '/api/v5/public/funding-rate-history'
        cursor = END
        while cursor > START:
            page = request(venue, endpoint, dict(instId=symbol, after=cursor, before=START - 1, limit=400))['data']
            if not page:
                break
            for x in page:
                if not x.get('realizedRate'):
                    raise ValueError('Missing actual realizedRate')
                rows.append((int(x['fundingTime']), x['realizedRate'], ''))
            nxt = min(int(x['fundingTime']) for x in page)
            if nxt >= cursor:
                raise ValueError('Funding pagination failed to advance')
            cursor = nxt
            if len(page) < 400:
                break
    else:
        endpoint = '/info'
        cursor = START
        while cursor < END:
            page = request(venue, endpoint, dict(type='fundingHistory', coin=base, startTime=cursor, endTime=END - 1), post=True)
            if not page:
                break
            rows.extend((int(x['time']), x['fundingRate'], '') for x in page)
            nxt = max(int(x['time']) for x in page) + 1
            if nxt <= cursor:
                raise ValueError('Funding pagination failed to advance')
            cursor = nxt
            if len(page) < 500:
                break
    return rows, API[venue] + endpoint


def fetch_candles(venue, base, symbol, mark=False):
    rows = []
    if venue == 'binance':
        endpoint = '/fapi/v1/markPriceKlines' if mark else '/fapi/v1/klines'
        page = request(venue, endpoint, dict(symbol=symbol, interval='1h', startTime=START, endTime=END - 1, limit=1000))
        rows = [(int(x[0]), *x[1:6]) for x in page]
    elif venue == 'bybit':
        endpoint = '/v5/market/mark-price-kline' if mark else '/v5/market/kline'
        page = request(venue, endpoint, dict(category='linear', symbol=symbol, interval='60', start=START, end=END - 1, limit=1000))['result']['list']
        rows = [(int(x[0]), *x[1:5], '' if mark else x[5]) for x in page]
    elif venue == 'okx':
        endpoint = '/api/v5/market/history-mark-price-candles' if mark else '/api/v5/market/history-candles'
        cursor = END
        while cursor > START:
            page = request(venue, endpoint, dict(instId=symbol, bar='1H', after=cursor, before=START - 1, limit=100 if mark else 300))['data']
            if not page:
                break
            rows.extend((int(x[0]), *x[1:5], '' if mark else x[5]) for x in page)
            nxt = min(int(x[0]) for x in page)
            if nxt >= cursor:
                raise ValueError('Candle pagination failed to advance')
            cursor = nxt
    else:
        endpoint = '/info'
        page = request(venue, endpoint, dict(type='candleSnapshot', req=dict(coin=base, interval='1h', startTime=START, endTime=END - 1)), post=True)
        rows = [(int(x['t']), x['o'], x['h'], x['l'], x['c'], x['v']) for x in page]
    return rows, API[venue] + endpoint


def unique(rows, kind):
    result = {}
    identical = 0
    for row in rows:
        if not START <= row[0] < END:
            continue
        if row[0] in result:
            old = result[row[0]]
            def normalized(r):
                return tuple(Decimal(str(v)) if v != '' else '' for v in r)
            if normalized(old) != normalized(row):
                raise ValueError(kind + ' conflicting duplicate at ' + iso(row[0]))
            identical += 1
        else:
            result[row[0]] = row
    return [result[t] for t in sorted(result)], identical


def coverage(rows, funding=False):
    times = [r[0] for r in rows]
    result = {'count': len(times), 'first': iso(times[0]) if times else None,
              'last': iso(times[-1]) if times else None}
    if not funding:
        expected = set(range(START, END, HOUR))
        actual = set(times)
        result.update(expected_count=720, missing_open_times=[iso(t) for t in sorted(expected - actual)],
                      off_grid_open_times=[iso(t) for t in sorted(actual - expected)],
                      complete=(actual == expected))
        return result
    spacings = [b - a for a, b in zip(times, times[1:])]
    observed = Counter()
    irregular = []
    for a, b in zip(times, times[1:]):
        candidates = [h for h in (1, 4, 8) if abs(b - a - h * HOUR) <= 2000]
        if candidates:
            observed[candidates[0]] += 1
        else:
            irregular.append({'previous': iso(a), 'next': iso(b), 'spacing_ms': b - a})
    nominal = observed.most_common(1)[0][0] if observed else None
    missing = []
    collisions = []
    if nominal:
        expected = list(range(START, END, nominal * HOUR))
        occupied = Counter()
        for t in times:
            slot = START + round((t - START) / (nominal * HOUR)) * nominal * HOUR
            if START <= slot < END and abs(t - slot) <= 2000:
                occupied[slot] += 1
        missing = [iso(t) for t in expected if not occupied[t]]
        collisions = [iso(t) for t, n in occupied.items() if n > 1]
        result['expected_count_nominal'] = len(expected)
    result.update(observed_spacing_hours={str(k): v for k, v in sorted(observed.items())},
                  nominal_interval_hours=nominal, tolerance_ms=2000,
                  irregular_spacings=irregular, missing_nominal_boundaries=missing,
                  duplicate_nominal_boundaries=collisions,
                  spacing_min_ms=min(spacings) if spacings else None,
                  spacing_max_ms=max(spacings) if spacings else None,
                  complete=bool(nominal and not missing and not collisions and not irregular))
    return result


def market(venue, base):
    symbol = base + '-USDT-SWAP' if venue == 'okx' else base if venue == 'hyperliquid' else base + 'USDT'
    info = {'exchange': venue, 'base': base, 'symbol': symbol, 'errors': [], 'exclusion_reasons': []}
    funding, candles = [], []
    try:
        raw, endpoint = fetch_funding(venue, base, symbol)
        funding, duplicates = unique(raw, 'funding')
        info['funding'] = coverage(funding, funding=True)
        info['funding']['identical_duplicates_removed'] = duplicates
        for row in funding:
            if not Decimal(str(row[1])).is_finite():
                raise ValueError('Nonfinite funding rate')
    except Exception as exc:
        info['errors'].append({'dataset': 'funding', 'message': str(exc)})
        funding = []
        endpoint = ''
        info['funding'] = coverage([], funding=True)
    funding_out = [dict(zip(FUNDING_FIELDS, [venue, base, symbol, iso(t), rate, price, endpoint])) for t, rate, price in funding]
    try:
        raw, trade_endpoint = fetch_candles(venue, base, symbol)
        candles, duplicates = unique(raw, 'candles')
        for row in candles:
            values = [Decimal(str(v)) for v in row[1:]]
            o, h, l, c, v = values
            if not all(x.is_finite() for x in values) or min(o, h, l, c) <= 0 or v < 0 or h < max(o, l, c) or l > min(o, h, c):
                raise ValueError('Invalid trade OHLCV at ' + iso(row[0]))
        info['candles'] = coverage(candles)
        info['candles'].update(identical_duplicates_removed=duplicates, source_endpoint=trade_endpoint)
    except Exception as exc:
        info['errors'].append({'dataset': 'candles', 'message': str(exc)})
        candles = []
        info['candles'] = coverage([])
    marks = {}
    mark_endpoint = ''
    if venue != 'hyperliquid' and candles:
        try:
            raw, mark_endpoint = fetch_candles(venue, base, symbol, mark=True)
            mark_rows, duplicates = unique(raw, 'mark candles')
            marks = {r[0]: r[1] for r in mark_rows}
            info['marks'] = coverage(mark_rows)
            info['marks']['identical_duplicates_removed'] = duplicates
        except Exception as exc:
            info['errors'].append({'dataset': 'optional_marks', 'message': str(exc)})
    else:
        info['marks'] = {'count': 0, 'reason': 'Historical mark/oracle candles unavailable' if venue == 'hyperliquid' else 'No trade candles'}
    candle_out = [dict(zip(CANDLE_FIELDS, [venue, base, symbol, iso(r[0]), *r[1:], marks.get(r[0], ''), mark_endpoint if r[0] in marks else ''])) for r in candles]
    if not info['funding']['complete']:
        info['exclusion_reasons'].append('Incomplete or irregular actual funding settlements')
    if not info['candles']['complete']:
        info['exclusion_reasons'].append('Missing or off-grid hourly trade candles')
    info['eligible_complete_month'] = not info['exclusion_reasons']
    print(venue + ' ' + base + ': funding=' + str(len(funding)) + ' candles=' + str(len(candles)) + ' marks=' + str(len(marks)) + ' errors=' + str(len(info['errors'])), flush=True)
    return funding_out, candle_out, info


def write_csv(name, fields, rows, time_field):
    rows.sort(key=lambda r: (r['exchange'], r['base'], r[time_field]))
    with (ROOT / name).open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    return {'sha256': sha((ROOT / name).read_bytes()), 'rows': len(rows)}


def main():
    started = now()
    funding, candles, markets = [], [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(market, venue, base) for venue in VENUES for base in BASES]
        for future in as_completed(futures):
            f, c, info = future.result()
            funding.extend(f)
            candles.extend(c)
            markets.append(info)
    artifacts = {'funding.csv': write_csv('funding.csv', FUNDING_FIELDS, funding, 'settlement_time'),
                 'candles.csv': write_csv('candles.csv', CANDLE_FIELDS, candles, 'open_time'),
                 'fetch_data.py': {'sha256': sha(Path(__file__).read_bytes())}}
    markets.sort(key=lambda m: (m['exchange'], m['base']))
    common = [b for b in BASES if all(next(m for m in markets if m['exchange'] == v and m['base'] == b)['eligible_complete_month'] for v in VENUES)]
    manifest = {'schema_version': 1, 'period': {'start_inclusive': iso(START), 'end_exclusive': iso(END), 'expected_hourly_candles': 720},
                'candidate_bases': BASES, 'venues': VENUES, 'candidate_market_count': 60,
                'fetch_started_at': started, 'fetch_as_of': now(),
                'policy': {'max_concurrency': 3, 'global_min_request_spacing_seconds': 0.25, 'retries_after_initial_attempt': 3,
                           'strict_timestamp_filter': '[start,end)', 'duplicate_policy': 'Deduplicate identical values; conflicting duplicates fail that market dataset',
                           'funding_rates': 'Actual raw per-event rates; OKX realizedRate; no annualization or filling',
                           'funding_frequency': 'Mode of observed 1/4/8-hour spacings within 2000ms; nominal UTC boundaries reported without filling',
                           'volume': 'Venue-native units: Binance/Bybit base asset; OKX contracts; Hyperliquid base asset. Not unified.',
                           'marks': 'CEX historical mark candle open when available; Hyperliquid blank. Trade prices never used as marks.',
                           'universe': 'All 60 candidate markets retained in coverage; no substitutions; complete common bases reported for downstream freezing'},
                'artifacts': artifacts, 'complete_common_bases': common, 'markets': markets,
                'exclusions': [{'exchange': m['exchange'], 'base': m['base'], 'reasons': m['exclusion_reasons']} for m in markets if m['exclusion_reasons']],
                'errors': [{'exchange': m['exchange'], 'base': m['base'], **e} for m in markets for e in m['errors']],
                'requests': sorted(REQUESTS, key=lambda r: r['requested_at'])}
    (ROOT / 'provenance.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=True, allow_nan=False) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({'funding_rows': len(funding), 'candle_rows': len(candles), 'complete_common_bases': common,
                      'excluded_markets': len(manifest['exclusions']), 'errors': len(manifest['errors']), 'requests': len(REQUESTS)}), flush=True)


if __name__ == '__main__':
    main()
