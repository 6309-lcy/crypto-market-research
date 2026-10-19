"""Extend existing Binance/Bybit CSVs with actual OKX archives and hourly opens.

ZIP responses stay in memory. Running again replaces only OKX records and
preserves all other exchange records and their original provenance.
"""
import argparse
import copy
import csv
import hashlib
import io
import json
import threading
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

BASES = 'ADA ARB ATOM AVAX BNB BTC DOGE DOT ETH LINK LTC OP SOL UNI XRP'.split()
HOUR = 3600000
FUNDING_FIELDS = ['exchange', 'base', 'symbol', 'settlement_time', 'funding_rate',
                  'settlement_mark_price', 'source_endpoint']
PRICE_FIELDS = ['exchange', 'base', 'symbol', 'time', 'trade_open', 'mark_open']
HOST = 'https://www.okx.com'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def stamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(
        timespec='milliseconds' if value % 1000 else 'seconds').replace('+00:00', 'Z')


def windows(values):
    result = []
    for value in sorted(values):
        if result and value == result[-1][1] + HOUR:
            result[-1][1] = value
        else:
            result.append([value, value])
    return [{'start': iso(a), 'end': iso(b), 'hours': (b-a)//HOUR+1} for a, b in result]


def months(start, end):
    # Archive month boundaries are UTC+8, including April 1 for March 31 16:00 UTC.
    first = datetime.fromtimestamp((start+8*HOUR)/1000, timezone.utc)
    last = datetime.fromtimestamp((end+8*HOUR)/1000, timezone.utc)
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        yield '%04d%02d' % (year, month), '%04d-%02d' % (year, month)
        year, month = (year+1, 1) if month == 12 else (year, month+1)


class Client:
    def __init__(self, spacing):
        self.spacing = spacing
        self.lock = threading.Lock()
        self.last = 0.0
        self.records = []
        self.local = threading.local()

    def get(self, endpoint, params, archive=False):
        url = endpoint + '?' + urlencode(params)
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
            self.local.session.headers.update({'User-Agent': 'Mozilla/5.0', 'Accept': '*/*'})
        for attempt in range(1, 4):
            with self.lock:
                delay = self.spacing - (time.monotonic() - self.last)
                if delay > 0:
                    time.sleep(delay)
                self.last = time.monotonic()
            record = {'endpoint': endpoint, 'parameters': dict(params), 'url': url,
                      'attempt': attempt, 'requested_at': iso(int(time.time()*1000))}
            try:
                response = self.local.session.get(url, timeout=30)
                raw = response.content
                record.update(http_status=response.status_code, response_bytes=len(raw), response_sha256=digest(raw))
                response.raise_for_status()
                if archive:
                    result = []
                    with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
                        members = []
                        for name in zipped.namelist():
                            if not name.lower().endswith('.csv'):
                                continue
                            body = zipped.read(name)
                            parsed = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
                            if not parsed or not {'instrument_name', 'funding_rate', 'funding_time'} <= set(parsed[0]):
                                raise ValueError('Archive CSV schema invalid or empty')
                            result.extend(parsed)
                            members.append({'name': name, 'sha256': digest(body), 'bytes': len(body), 'rows': len(parsed)})
                        record['archive_members'] = members
                        if not members:
                            raise ValueError('No funding CSV in archive')
                else:
                    payload = response.json()
                    if payload.get('code') != '0':
                        raise ValueError('OKX %s: %s' % (payload.get('code'), payload.get('msg')))
                    result = payload['data']
                record['success'] = True
                with self.lock:
                    self.records.append(record)
                return result
            except Exception as exc:
                # Store bounded public errors without machine-specific exception text.
                record.update(success=False, error=str(exc)[:600] if isinstance(exc, (ValueError, requests.HTTPError)) else type(exc).__name__)
                with self.lock:
                    self.records.append(record)
                if attempt == 3:
                    raise RuntimeError(record['error']) from None
                time.sleep(0.5 * attempt)


def funding(client, symbol, start, end):
    result, errors = {}, []
    for compact, dashed in months(start, end):
        endpoint = ('https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/'
                    + compact + '/' + symbol + '-fundingrates-' + dashed + '.zip')
        try:
            rows = client.get(endpoint, {'v': '1001'}, archive=True)
            for row in rows:
                if row['instrument_name'] != symbol:
                    raise ValueError('Unexpected instrument in archive')
                ts = int(row['funding_time'])
                float(row['funding_rate'])
                if start <= ts <= end:
                    value = (row['funding_rate'], endpoint + '?v=1001')
                    if ts in result and result[ts][0] != value[0]:
                        raise ValueError('Conflicting archive funding rates')
                    result[ts] = value
        except (RuntimeError, ValueError) as exc:
            errors.append({'month': dashed, 'error': str(exc)})
    return result, errors


def candles(client, symbol, start, end, mark):
    endpoint = HOST + ('/api/v5/market/history-mark-price-candles' if mark else '/api/v5/market/history-candles')
    cursor = end + 1
    result, errors = {}, []
    while cursor > start:
        params = {'instId': symbol, 'bar': '1H', 'after': cursor, 'limit': 100 if mark else 300}
        try:
            rows = client.get(endpoint, params)
            if not rows:
                break
            times = []
            for row in rows:
                ts = int(row[0])
                times.append(ts)
                if start <= ts <= end and ts % HOUR == 0:
                    if float(row[1]) <= 0:
                        raise ValueError('Nonpositive candle open')
                    result[ts] = row[1]
            oldest = min(times)
            if oldest <= start:
                break
            next_cursor = oldest - 1
            if next_cursor >= cursor:
                raise ValueError('Pagination made no progress; requested bounds may be ignored')
            cursor = next_cursor
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))
            break
    return result, errors


def fetch(client, base, start, price_start, end):
    symbol = base + '-USDT-SWAP'
    funded, funding_errors = funding(client, symbol, start, end)
    traded, trade_errors = candles(client, symbol, price_start, end, False)
    marked, mark_errors = candles(client, symbol, price_start, end, True)
    paired = set(traded) & set(marked)
    expected = set(range(price_start, end+1, HOUR))
    times = sorted(funded)
    previous = Counter()
    gaps, intervals = [], Counter()
    for a, b in zip(times, times[1:]):
        delta = b-a
        intervals[round(delta/HOUR, 3)] += 1
        baseline = previous.most_common(1)[0][0] if previous else None
        if baseline and delta > baseline+1000:
            gaps.append({'previous_settlement': iso(a), 'settlement': iso(b),
                         'gap_hours': delta/HOUR, 'prior_modal_interval_hours': baseline/HOUR})
        previous[round(delta/1000)*1000] += 1
    detail = {'exchange': 'okx', 'base': base, 'symbol': symbol,
              'funding_count': len(funded), 'funding_first': iso(times[0]) if times else None,
              'funding_last': iso(times[-1]) if times else None,
              'funding_interval_hours_histogram': dict(sorted(intervals.items())),
              'funding_interval_gaps_against_prior_mode': gaps,
              'funding_errors': funding_errors, 'trade_errors': trade_errors, 'mark_errors': mark_errors,
              'trade_count': len(traded), 'mark_count': len(marked), 'paired_price_count': len(paired),
              'price_first': iso(min(paired)) if paired else None, 'price_last': iso(max(paired)) if paired else None,
              'complete_hour_windows': windows(paired), 'missing_trade_hours': windows(expected-set(traded)),
              'missing_mark_hours': windows(expected-set(marked)), 'missing_paired_hours': windows(expected-paired),
              'status': 'complete' if paired == expected and funded and not (funding_errors or trade_errors or mark_errors) else 'partial' if paired or funded else 'missing'}
    fs = [dict(zip(FUNDING_FIELDS, ['okx', base, symbol, str(ts), funded[ts][0], '', funded[ts][1]])) for ts in times]
    ps = [dict(zip(PRICE_FIELDS, ['okx', base, symbol, iso(ts), traded[ts], marked[ts]])) for ts in sorted(paired)]
    print('okx %s funding=%d paired_hours=%d/%d status=%s' % (symbol, len(fs), len(ps), len(expected), detail['status']), flush=True)
    return fs, ps, detail


def read_csv(path, fields):
    with path.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != fields:
            raise ValueError('Unexpected schema in ' + path.name)
        return list(reader)


def csv_bytes(fields, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--funding-start', default='2026-01-22T00:00:00Z')
    parser.add_argument('--price-start', default='2026-01-24T00:00:00Z')
    parser.add_argument('--end', default='2026-03-31T16:00:00Z')
    parser.add_argument('--workers', type=int, default=3, choices=range(1, 4))
    parser.add_argument('--spacing', type=float, default=0.2)
    args = parser.parse_args()
    start, price_start, end = map(stamp, [args.funding_start, args.price_start, args.end])
    if not start <= price_start <= end or any(t % HOUR for t in (start, price_start, end)):
        parser.error('Dates must be ordered UTC hour boundaries')
    if args.spacing < 0.2:
        parser.error('Global spacing must be at least 0.2 seconds')
    folder = args.output_dir
    original_bytes = {name: (folder/name).read_bytes() for name in ('funding.csv', 'prices.csv', 'provenance.json')}
    manifest = json.loads(original_bytes['provenance.json'])
    old_extension = manifest.get('okx_archive_extension')
    # Preserve the pre-extension document without recursively nesting subsequent runs.
    if old_extension:
        original = old_extension['original_provenance']
    else:
        original = copy.deepcopy(manifest)
    old_funding = [r for r in read_csv(folder/'funding.csv', FUNDING_FIELDS) if r['exchange'] != 'okx']
    old_prices = [r for r in read_csv(folder/'prices.csv', PRICE_FIELDS) if r['exchange'] != 'okx']
    preserved_hashes = {'funding.csv': digest(csv_bytes(FUNDING_FIELDS, old_funding)),
                        'prices.csv': digest(csv_bytes(PRICE_FIELDS, old_prices))}
    client = Client(args.spacing)
    fs, ps, details = [], [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch, client, base, start, price_start, end) for base in BASES]
        for future in as_completed(futures):
            funding_rows, price_rows, detail = future.result()
            fs.extend(funding_rows)
            ps.extend(price_rows)
            details.append(detail)
    merged_funding = sorted(old_funding+fs, key=lambda r: (r['exchange'], r['base'], int(r['settlement_time'])))
    merged_prices = sorted(old_prices+ps, key=lambda r: (r['exchange'], r['base'], r['time']))
    data = {'funding.csv': csv_bytes(FUNDING_FIELDS, merged_funding), 'prices.csv': csv_bytes(PRICE_FIELDS, merged_prices)}
    summary = {'requested_contracts': len(BASES),
               'usable_contracts': sorted(d['base'] for d in details if d['paired_price_count'] and d['funding_count']),
               'complete_contracts': sorted(d['base'] for d in details if d['status'] == 'complete'),
               'missing_contracts': sorted(d['base'] for d in details if d['status'] == 'missing'),
               'unusable_contracts': sorted(d['base'] for d in details if not d['paired_price_count'] or not d['funding_count']),
               'funding_rows': len(fs), 'price_rows': len(ps)}
    extension = {'generated_at': iso(int(time.time()*1000)), 'universe': BASES,
                 'funding_start_inclusive': iso(start), 'price_start_inclusive': iso(price_start), 'end_inclusive': iso(end),
                 'expected_price_hours_per_contract': (end-price_start)//HOUR+1,
                 'archive_semantics': 'Monthly archive names use UTC+8 grouping. Original funding_time millisecond timestamps are filtered in UTC. ZIPs decoded in memory only. No funding settlement mark price is supplied by the archive.',
                 'price_semantics': 'Actual hourly candle OPENs; exact timestamp inner join of trade and mark observations. Execution proxies, not BBO quotes. No filling or symbol substitution.',
                 'funding_diagnostics_note': 'Retrospective interval histograms rounded to 0.001 hours. Gap flags compare with the mode of earlier intervals, rounded to seconds, with 1 second jitter tolerance. Schedule changes may cause flags; no future interval inference is used.',
                 'rate_limits': {'global_spacing_seconds': args.spacing, 'concurrency': args.workers, 'max_attempts_per_request': 3},
                 'original_provenance': original,
                 'input_file_sha256': {name: digest(raw) for name, raw in original_bytes.items()},
                 'preserved_non_okx_csv_sha256': preserved_hashes,
                 'exchange_summary': summary, 'contracts': sorted(details, key=lambda d: d['base']),
                 'requests': sorted(client.records, key=lambda r: (r['requested_at'], r['url'], r['attempt']))}
    manifest['okx_archive_extension'] = extension
    manifest['exchange_summary']['okx'] = summary
    manifest['contracts'] = sorted([d for d in manifest['contracts'] if d['exchange'] != 'okx']+details,
                                   key=lambda d: (d['exchange'], d['base']))
    for name, raw in data.items():
        manifest['files'][name] = {'sha256': digest(raw), 'bytes': len(raw)}
    script = Path(__file__).read_bytes()
    manifest['files']['add_okx.py'] = {'sha256': digest(script), 'bytes': len(script)}
    manifest['files'].pop('provenance.json', None)
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    manifest['files']['provenance.json'] = {'sha256': digest(canonical), 'scope': 'canonical document excluding files.provenance.json'}
    data['provenance.json'] = (json.dumps(manifest, indent=2, ensure_ascii=True)+'\n').encode('utf-8')
    # Refuse to overwrite CSVs or provenance changed by another writer during download.
    for name, raw in original_bytes.items():
        if (folder/name).read_bytes() != raw:
            raise RuntimeError('Input changed during download: '+name)
    assert [r for r in merged_funding if r['exchange'] != 'okx'] == old_funding
    assert [r for r in merged_prices if r['exchange'] != 'okx'] == old_prices
    for name, raw in data.items():
        (folder/name).write_bytes(raw)
    print(json.dumps(summary, sort_keys=True), flush=True)
    print('MERGED FILES STABLE: funding.csv, prices.csv, provenance.json', flush=True)


if __name__ == '__main__':
    main()
