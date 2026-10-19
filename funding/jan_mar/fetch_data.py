"""Download actual public USDT perpetual funding and hourly candle opens.

Default window includes funding warmup from January 22, 2026. Prices start
January 24. No symbols are substituted and no observations are filled.
"""
import argparse
import csv
import hashlib
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASES = 'ADA ARB ATOM AVAX BNB BTC DOGE DOT ETH LINK LTC MATIC OP SOL UNI XRP'.split()
HOUR = 3600000
HOSTS = {'binance': 'https://fapi.binance.com', 'bybit': 'https://api.bybit.com'}
FUNDING_FIELDS = ['exchange', 'base', 'symbol', 'settlement_time', 'funding_rate',
                  'settlement_mark_price', 'source_endpoint']
PRICE_FIELDS = ['exchange', 'base', 'symbol', 'time', 'trade_open', 'mark_open']


def utc_ms(value):
    value = str(value).replace('Z', '+00:00')
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec='milliseconds' if value % 1000 else 'seconds').replace('+00:00', 'Z')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ranges(values):
    result = []
    for value in sorted(values):
        if result and value == result[-1][1] + HOUR:
            result[-1][1] = value
        else:
            result.append([value, value])
    return [{'start': iso(a), 'end': iso(b), 'hours': (b-a)//HOUR+1} for a, b in result]


class Client:
    def __init__(self, spacing):
        self.spacing = spacing
        self.lock = threading.Lock()
        self.last = 0.0
        self.requests = []

    def get(self, exchange, endpoint, params):
        url = HOSTS[exchange] + endpoint + '?' + urlencode(params)
        for attempt in range(1, 4):
            with self.lock:
                delay = self.spacing - (time.monotonic() - self.last)
                if delay > 0:
                    time.sleep(delay)
                self.last = time.monotonic()
            record = {'exchange': exchange, 'endpoint': endpoint, 'parameters': dict(params),
                      'url': url, 'attempt': attempt, 'requested_at': iso(int(time.time()*1000))}
            try:
                request = Request(url, headers={'User-Agent': 'public-market-research/1.0'})
                with urlopen(request, timeout=30) as response:
                    raw = response.read()
                    record['http_status'] = response.status
                record['response_sha256'] = sha(raw)
                record['response_bytes'] = len(raw)
                payload = json.loads(raw)
                if exchange == 'bybit' and payload.get('retCode') != 0:
                    raise ValueError('Bybit %s: %s' % (payload.get('retCode'), payload.get('retMsg')))
                if exchange == 'binance' and not isinstance(payload, list):
                    raise ValueError('Unexpected Binance response: ' + str(payload))
                record['success'] = True
                with self.lock:
                    self.requests.append(record)
                return payload
            except Exception as exc:
                if isinstance(exc, HTTPError):
                    body = exc.read()
                    record.update(http_status=exc.code, response_sha256=sha(body), response_bytes=len(body))
                    error = 'HTTP %s: %s' % (exc.code, body.decode('utf-8', errors='replace')[:500])
                else:
                    error = str(exc)
                record.update(success=False, error=error)
                with self.lock:
                    self.requests.append(record)
                if attempt == 3:
                    raise RuntimeError(error) from None
                time.sleep(attempt * 0.5)


def funding(client, exchange, symbol, start, end):
    endpoint = '/fapi/v1/fundingRate' if exchange == 'binance' else '/v5/market/funding/history'
    cursor = start if exchange == 'binance' else end
    rows = {}
    errors = []
    while start <= cursor <= end:
        params = {'symbol': symbol, 'startTime': start if exchange == 'bybit' else cursor,
                  'endTime': cursor if exchange == 'bybit' else end,
                  'limit': 1000 if exchange == 'binance' else 200}
        if exchange == 'bybit':
            params['category'] = 'linear'
        try:
            payload = client.get(exchange, endpoint, params)
        except RuntimeError as exc:
            errors.append(str(exc))
            break
        batch = payload if exchange == 'binance' else payload['result']['list']
        if not batch:
            break
        stamps = []
        for item in batch:
            stamp = int(item['fundingTime' if exchange == 'binance' else 'fundingRateTimestamp'])
            stamps.append(stamp)
            if start <= stamp <= end:
                rows[stamp] = (item['fundingRate'], item.get('markPrice', ''))
        next_cursor = max(stamps)+1 if exchange == 'binance' else min(stamps)-1
        if (exchange == 'binance' and next_cursor <= cursor) or (exchange == 'bybit' and next_cursor >= cursor):
            errors.append('Pagination made no progress; endpoint may have ignored requested bounds')
            break
        cursor = next_cursor
    return rows, endpoint, errors


def candles(client, exchange, symbol, start, end, mark):
    if exchange == 'binance':
        endpoint = '/fapi/v1/markPriceKlines' if mark else '/fapi/v1/klines'
    else:
        endpoint = '/v5/market/mark-price-kline' if mark else '/v5/market/kline'
    cursor = start if exchange == 'binance' else end
    rows, errors = {}, []
    while start <= cursor <= end:
        if exchange == 'binance':
            params = {'symbol': symbol, 'interval': '1h', 'startTime': cursor, 'endTime': end, 'limit': 1500}
        else:
            params = {'category': 'linear', 'symbol': symbol, 'interval': '60', 'start': start, 'end': cursor, 'limit': 1000}
        try:
            payload = client.get(exchange, endpoint, params)
        except RuntimeError as exc:
            errors.append(str(exc))
            break
        batch = payload if exchange == 'binance' else payload['result']['list']
        if not batch:
            break
        stamps = []
        for item in batch:
            stamp = int(item[0])
            stamps.append(stamp)
            if start <= stamp <= end and stamp % HOUR == 0:
                rows[stamp] = item[1]
        next_cursor = max(stamps)+HOUR if exchange == 'binance' else min(stamps)-1
        if (exchange == 'binance' and next_cursor <= cursor) or (exchange == 'bybit' and next_cursor >= cursor):
            errors.append('Pagination made no progress; endpoint may have ignored requested bounds')
            break
        cursor = next_cursor
    return rows, errors


def fetch_contract(client, exchange, base, start, price_start, end):
    symbol = base + 'USDT'
    settlements, endpoint, funding_errors = funding(client, exchange, symbol, start, end)
    traded, trade_errors = candles(client, exchange, symbol, price_start, end, False)
    marked, mark_errors = candles(client, exchange, symbol, price_start, end, True)
    expected = set(range(price_start, end+1, HOUR))
    paired = set(traded) & set(marked)
    times = sorted(settlements)
    deltas = Counter(round((b-a)/HOUR, 3) for a, b in zip(times, times[1:]))
    # Only intervals already observed before the current settlement form its baseline.
    observed = Counter()
    gaps = []
    for a, b in zip(times, times[1:]):
        delta = b-a
        baseline = observed.most_common(1)[0][0] if observed else None
        if baseline and delta > baseline + 1000:
            gaps.append({'previous_settlement': iso(a), 'settlement': iso(b),
                         'gap_hours': delta/HOUR, 'prior_modal_interval_hours': baseline/HOUR})
        observed[round(delta / 1000) * 1000] += 1
    detail = {'exchange': exchange, 'base': base, 'symbol': symbol,
              'funding_count': len(times), 'funding_first': iso(times[0]) if times else None,
              'funding_last': iso(times[-1]) if times else None,
              'funding_interval_hours_histogram': dict(sorted(deltas.items())),
              'funding_interval_gaps_against_prior_mode': gaps,
              'funding_errors': funding_errors, 'trade_errors': trade_errors, 'mark_errors': mark_errors,
              'trade_count': len(traded), 'mark_count': len(marked), 'paired_price_count': len(paired),
              'price_first': iso(min(paired)) if paired else None, 'price_last': iso(max(paired)) if paired else None,
              'complete_hour_windows': ranges(paired), 'missing_trade_hours': ranges(expected-set(traded)),
              'missing_mark_hours': ranges(expected-set(marked)), 'missing_paired_hours': ranges(expected-paired),
              'status': 'complete' if paired == expected and times and not (funding_errors or trade_errors or mark_errors) else 'partial' if paired or times else 'missing'}
    frows = [[exchange, base, symbol, stamp, settlements[stamp][0], settlements[stamp][1], endpoint] for stamp in times]
    prows = [[exchange, base, symbol, iso(stamp), traded[stamp], marked[stamp]] for stamp in sorted(paired)]
    print('%s %s: funding=%d paired_hours=%d/%d status=%s' %
          (exchange, symbol, len(times), len(paired), len(expected), detail['status']), flush=True)
    return frows, prows, detail


def write_csv(path, fields, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(fields)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--funding-start', default='2026-01-22T00:00:00Z')
    parser.add_argument('--price-start', default='2026-01-24T00:00:00Z')
    parser.add_argument('--end', default='2026-03-31T16:00:00Z')
    parser.add_argument('--workers', type=int, default=3, choices=range(1, 4))
    parser.add_argument('--spacing', type=float, default=0.15)
    args = parser.parse_args()
    start, price_start, end = map(utc_ms, [args.funding_start, args.price_start, args.end])
    if not start <= price_start <= end or any(t % HOUR for t in (start, price_start, end)):
        parser.error('Dates must be ordered UTC hourly boundaries')
    if args.spacing < 0.15:
        parser.error('Global request spacing must be at least 0.15 seconds')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = Client(args.spacing)
    frows, prows, details = [], [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_contract, client, exchange, base, start, price_start, end)
                   for exchange in HOSTS for base in BASES]
        for future in as_completed(futures):
            fs, ps, detail = future.result()
            frows.extend(fs)
            prows.extend(ps)
            details.append(detail)
    frows.sort(key=lambda r: (r[0], r[1], r[3]))
    prows.sort(key=lambda r: (r[0], r[1], r[3]))
    write_csv(args.output_dir / 'funding.csv', FUNDING_FIELDS, frows)
    write_csv(args.output_dir / 'prices.csv', PRICE_FIELDS, prows)
    expected = set(range(price_start, end+1, HOUR))
    exchange_summary = {}
    for exchange in HOSTS:
        subset = [d for d in details if d['exchange'] == exchange]
        usable = [d['base'] for d in subset if d['paired_price_count'] and d['funding_count']]
        complete = [d['base'] for d in subset if d['status'] == 'complete']
        exchange_summary[exchange] = {'requested_contracts': len(BASES), 'usable_contracts': sorted(usable),
                                      'complete_contracts': sorted(complete),
                                      'missing_contracts': [d['base'] for d in subset if d['status'] == 'missing'],
                                      'unusable_contracts': [d['base'] for d in subset if not d['paired_price_count'] or not d['funding_count']],
                                      'funding_rows': sum(d['funding_count'] for d in subset),
                                      'price_rows': sum(d['paired_price_count'] for d in subset)}
    files = {name: {'sha256': sha((args.output_dir/name).read_bytes()), 'bytes': (args.output_dir/name).stat().st_size}
             for name in ('funding.csv', 'prices.csv')}
    files['fetch_data.py'] = {'sha256': sha(Path(__file__).read_bytes()), 'bytes': Path(__file__).stat().st_size}
    manifest = {'generated_at': iso(int(time.time()*1000)), 'universe': BASES,
                'funding_start_inclusive': iso(start), 'price_start_inclusive': iso(price_start),
                'end_inclusive': iso(end), 'expected_price_hours_per_contract': len(expected),
                'funding_timestamp_unit': 'original exchange UTC milliseconds',
                'price_timestamp_format': 'UTC ISO 8601 hourly candle open time',
                'price_semantics': 'Observed trade and mark candle OPEN prices; trade opens are execution proxies, not BBO quotes. Inner join on exact open timestamp only.',
                'funding_semantics': 'Decimal fraction. Settlement mark price populated only if actually returned by funding endpoint. No future interval inference or synthetic filling.',
                'funding_diagnostics_note': 'Interval histograms are retrospective diagnostics rounded to 0.001 hours. Gap flags compare each realized interval to the mode of earlier observed intervals rounded to seconds, with 1 second jitter tolerance; changes in exchange schedule can also cause flags. Raw settlement timestamps remain unchanged. Missing settlements cannot be conclusively identified without the historical schedule.',
                'rate_limits': {'global_spacing_seconds': args.spacing, 'concurrency': args.workers, 'max_attempts_per_request': 3},
                'exchange_summary': exchange_summary,
                'contracts': sorted(details, key=lambda d: (d['exchange'], d['base'])),
                'requests': sorted(client.requests, key=lambda d: (d['requested_at'], d['url'], d['attempt'])),
                'files': files,
                'self_hash_convention': 'provenance.json SHA256 covers canonical UTF-8 JSON (sort_keys=True, separators=(comma,colon), ensure_ascii=True) with its own files entry omitted. A literal whole-file self-hash is impossible.'}
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    manifest['files']['provenance.json'] = {'sha256': sha(canonical), 'scope': 'canonical document excluding files.provenance.json'}
    with (args.output_dir/'provenance.json').open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=True)
        stream.write('\n')
    print(json.dumps(exchange_summary, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
