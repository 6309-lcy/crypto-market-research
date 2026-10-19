"""Download free public Bybit BTCUSDT archives and validate every record in streams."""
import argparse
from array import array
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import os
import shutil
import time
import urllib.request
import zipfile

START = 1788220800000
END = 1788307200000
SOURCES = {
    'book': 'https://quote-saver.bycsi.com/orderbook/linear/BTCUSDT/2026-09-01_BTCUSDT_ob200.data.zip',
    'trades': 'https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-09-01.csv.gz',
}
try:
    import orjson
    loads = orjson.loads
except ImportError:
    loads = json.loads


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def download(url, cache):
    path = cache / url.rsplit('/', 1)[1]
    partial = path.with_name(path.name + '.part')
    meta_path = path.with_name(path.name + '.meta.json')
    request_time = utc()
    headers = {'User-Agent': 'public-market-research/1.0', 'Accept-Encoding': 'identity'}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method='HEAD'), timeout=60) as r:
        expected = int(r.headers['Content-Length'])
        etag = r.headers.get('ETag')
        modified = r.headers.get('Last-Modified')
    previous = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    reused = path.exists() and path.stat().st_size == expected and previous.get('etag') == etag
    if not reused:
        if path.exists():
            raise RuntimeError('Existing final cache object does not match source metadata')
        if partial.exists() and previous.get('etag') != etag:
            partial.unlink()
        meta_path.write_text(json.dumps({'etag': etag, 'download_started_utc': request_time}), encoding='utf-8')
        for attempt in range(1, 9):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset == expected:
                break
            try:
                h = dict(headers)
                if offset:
                    h.update({'Range': f'bytes={offset}-', 'If-Range': etag or modified})
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
                    if offset and r.status == 206:
                        if not r.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                            raise RuntimeError('Invalid resume Content-Range')
                        mode = 'ab'
                    elif r.status == 200:
                        offset, mode = 0, 'wb'
                    else:
                        raise RuntimeError(f'Unexpected HTTP status {r.status}')
                    last_log = time.monotonic()
                    with partial.open(mode) as f:
                        while chunk := r.read(1024 * 1024):
                            f.write(chunk)
                            offset += len(chunk)
                            if time.monotonic() - last_log >= 30:
                                print(f'download {path.name}: {offset}/{expected} bytes', flush=True)
                                last_log = time.monotonic()
                if partial.stat().st_size != expected:
                    raise RuntimeError('Incomplete response length')
                break
            except Exception as exc:
                print(f'download retry {attempt}/8: {type(exc).__name__}: {exc}', flush=True)
                if attempt == 8:
                    raise
                time.sleep(min(2 ** attempt, 30))
        if partial.stat().st_size != expected:
            raise RuntimeError('Downloaded length mismatch')
        partial.replace(path)
        previous = {'download_started_utc': request_time, 'download_completed_utc': utc()}
    digest = sha(path)
    if reused and previous.get('sha256') and previous['sha256'] != digest:
        raise RuntimeError('Cached file SHA256 changed')
    meta = {'url': url, 'bytes': expected, 'sha256': digest, 'etag': etag,
            'last_modified': modified, 'checked_utc': request_time, 'cache_reused': reused,
            'download_started_utc': previous.get('download_started_utc'),
            'download_completed_utc': previous.get('download_completed_utc')}
    meta_path.write_text(json.dumps(meta, indent=2), encoding='utf-8')
    print(f'download verified {path.name}: {expected} bytes sha256={digest}', flush=True)
    return path, meta


class Coverage:
    def __init__(self):
        self.times = array('q')
        self.counts = [0] * 1440
        self.outside = Counter()
        self.previous = None
        self.out_of_order = 0
        self.same_timestamp = 0

    def add(self, us):
        if self.previous is not None:
            self.out_of_order += us < self.previous
            self.same_timestamp += us == self.previous
        self.previous = us
        if us < START * 1000:
            self.outside['before_day'] += 1
            return False
        if us >= END * 1000:
            self.outside['at_or_after_end'] += 1
            return False
        self.times.append(us)
        self.counts[(us // 1000 - START) // 60000] += 1
        return True

    def report(self):
        values = sorted(self.times)
        bounds = [START * 1000, END * 1000]
        largest = (0, bounds[0], bounds[1])
        previous = bounds[0]
        for value in values:
            if value - previous > largest[0]:
                largest = (value - previous, previous, value)
            previous = value
        if bounds[1] - previous > largest[0]:
            largest = (bounds[1] - previous, previous, bounds[1])
        return {'in_day_records': len(values), 'outside_day': dict(self.outside),
                'first_ts_ms': values[0] / 1000 if values else None,
                'last_ts_ms': values[-1] / 1000 if values else None,
                'minutes_covered': sum(n > 0 for n in self.counts),
                'missing_minute_offsets': [i for i, n in enumerate(self.counts) if not n],
                'longest_gap_including_day_edges_ms': largest[0] / 1000,
                'longest_gap_start_ms': largest[1] / 1000,
                'longest_gap_end_ms': largest[2] / 1000,
                'timestamp_regressions': self.out_of_order, 'equal_adjacent_timestamps': self.same_timestamp}


def validate_books(path, minutes):
    cov, counts = Coverage(), Counter({k: 0 for k in (
        'invalid_records', 'duplicate_prices_within_side', 'delta_deletes_absent',
        'deltas_before_snapshot', 'records_with_cts', 'cts_after_ts')})
    duplicate_examples = []
    bids, asks = {}, {}
    prev_u = prev_seq = None
    seen = set()
    examples, errors = {}, []
    initialized = False
    last_log = time.monotonic()
    members = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            members.append({'name': Path(member.filename).name, 'uncompressed_bytes': member.file_size,
                            'compressed_bytes': member.compress_size, 'crc32': f'{member.CRC:08x}'})
            with archive.open(member) as stream:
                for line in stream:
                    counts['lines'] += 1
                    try:
                        record = loads(line)
                        d, kind, ts = record['data'], record['type'], record['ts']
                        if kind not in ('snapshot', 'delta') or record['topic'] != 'orderbook.200.BTCUSDT' or d['s'] != 'BTCUSDT':
                            raise ValueError('Invalid type/topic/symbol')
                        u, seq = d['u'], d['seq']
                        cts = record.get('cts')
                        if cts is not None:
                            if type(cts) is not int or cts < 0:
                                raise ValueError('Invalid matching engine timestamp')
                            counts['records_with_cts'] += 1
                            counts['cts_after_ts'] += cts > ts
                        if any(type(v) is not int or v < 0 for v in (ts, u, seq)):
                            raise ValueError('Invalid timestamp/u/seq')
                        parsed = []
                        for side in ('b', 'a'):
                            levels, prices = [], set()
                            if not isinstance(d[side], list):
                                raise ValueError('Side is not a list')
                            for level in d[side]:
                                if not isinstance(level, list) or len(level) != 2:
                                    raise ValueError('Invalid price/size pair')
                                p, q = map(float, level)
                                if not math.isfinite(p) or not math.isfinite(q) or p <= 0 or q < 0:
                                    raise ValueError('Invalid price/size')
                                if p in prices:
                                    counts['duplicate_prices_within_side'] += 1
                                prices.add(p)
                                levels.append((p, q))
                            parsed.append(levels)
                        counts[kind + 's'] += 1
                        inside = cov.add(ts * 1000)
                        key = (u, seq)
                        counts['duplicate_u_seq'] += key in seen
                        if key in seen and len(duplicate_examples) < 5:
                            duplicate_examples.append({'ts': ts, 'type': kind, 'u': u, 'seq': seq,
                                                       'inside_day': inside})
                        seen.add(key)
                        if prev_u is not None:
                            counts['u_equal'] += u == prev_u
                            counts['u_nonunit_increases'] += u > prev_u + 1
                            counts['u_regressions'] += u < prev_u
                            counts['seq_equal'] += seq == prev_seq
                            counts['seq_regressions'] += seq < prev_seq
                            if kind == 'delta' and u != 1:
                                counts['delta_u_regressions'] += u < prev_u
                                counts['delta_seq_regressions'] += seq < prev_seq
                        if kind == 'snapshot' or u == 1:
                            counts['reconstruction_resets'] += 1
                            counts['u_one_service_resets'] += u == 1
                            bids.clear()
                            asks.clear()
                            initialized = True
                        elif not initialized:
                            counts['deltas_before_snapshot'] += 1
                        for book, levels in zip((bids, asks), parsed):
                            for p, q in levels:
                                if q == 0:
                                    counts['zero_size_levels'] += 1
                                    if kind == 'delta':
                                        counts['delta_deletes_existing' if p in book else 'delta_deletes_absent'] += 1
                                    book.pop(p, None)
                                else:
                                    if kind == 'delta':
                                        counts['delta_updates' if p in book else 'delta_inserts'] += 1
                                    book[p] = q
                        counts['max_bid_levels'] = max(counts['max_bid_levels'], len(bids))
                        counts['max_ask_levels'] = max(counts['max_ask_levels'], len(asks))
                        empty = not bids or not asks
                        counts['empty_books'] += empty
                        best_b, best_a = (max(bids), min(asks)) if not empty else (None, None)
                        crossed = not empty and best_b > best_a
                        locked = not empty and best_b == best_a
                        counts['crossed_books'] += crossed
                        counts['locked_books'] += locked
                        if inside:
                            m = minutes[(ts - START) // 60000]
                            m['book_records'] += 1
                            m[kind + 's'] += 1
                            m['crossed_books'] += crossed
                            m['empty_books'] += empty
                            if not empty:
                                m['last_bid'], m['last_ask'] = best_b, best_a
                        example = {'type': kind, 'ts': ts, 'cts': cts, 'u': u, 'seq': seq,
                                   'bid_updates': len(d['b']), 'ask_updates': len(d['a']),
                                   'first_bid_pair': d['b'][:1], 'first_ask_pair': d['a'][:1]}
                        examples.setdefault('first', example)
                        examples['last'] = example
                        prev_u, prev_seq = u, seq
                    except (ValueError, KeyError, TypeError, OverflowError) as exc:
                        counts['invalid_records'] += 1
                        if len(errors) < 5:
                            errors.append({'line': counts['lines'], 'error': str(exc)[:160]})
                    if time.monotonic() - last_log >= 30:
                        print(f'book parsed {counts["lines"]} records', flush=True)
                        last_log = time.monotonic()
    return {'counters': dict(counts), 'coverage': cov.report(), 'examples': examples,
            'errors': errors, 'duplicate_identifier_examples': duplicate_examples, 'zip_members': members,
            'counter_scope': 'All archive records; minute summary and coverage use only the requested UTC interval.',
            'continuity_semantics': 'u increases need not be one. No missing-message count is inferred from nonunit u or seq increases. Snapshots replace both sides; u=1 signals a service reset. Equal/regressing identifiers are counted; monotonic identifiers cannot prove archive completeness.',
            'reconstruction': 'Price levels use consistent float conversion of archive decimal strings; zero sizes delete, positive sizes insert/replace. Every archive record is replayed, including any outside the requested interval. No depth truncation is imposed.'}


def validate_trades(path, minutes):
    cov, counts, flags, units = Coverage(), Counter({'invalid_records': 0}), Counter({'0': 0, '1': 0}), Counter()
    seen, examples, errors = set(), {}, []
    last_log = time.monotonic()
    with gzip.open(path, 'rt', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        required = {'timestamp', 'symbol', 'side', 'size', 'price', 'trdMatchID', 'RPI'}
        if not required.issubset(fields or []):
            raise ValueError('Missing required trade columns')
        for row in reader:
            counts['rows'] += 1
            try:
                raw_ts = Decimal(row['timestamp'])
                if not raw_ts.is_finite() or raw_ts < 0:
                    raise ValueError('Invalid trade timestamp')
                unit = 'seconds' if raw_ts < Decimal('100000000000') else 'milliseconds'
                us = int(raw_ts * (1000000 if unit == 'seconds' else 1000))
                units[unit] += 1
                p, q = float(row['price']), float(row['size'])
                if row['symbol'] != 'BTCUSDT' or row['side'] not in ('Buy', 'Sell'):
                    raise ValueError('Invalid symbol/side')
                if not all(math.isfinite(v) and v > 0 for v in (p, q)):
                    raise ValueError('Invalid trade price/size')
                if row['RPI'] not in ('0', '1') or not row['trdMatchID']:
                    raise ValueError('Invalid RPI/trade identifier')
                inside = cov.add(us)
                ident = row['trdMatchID']
                counts['duplicate_trade_ids'] += ident in seen
                seen.add(ident)
                flags[row['RPI']] += 1
                counts[row['side']] += 1
                if inside:
                    m = minutes[(us // 1000 - START) // 60000]
                    m['trades'] += 1
                    m['trade_size_btc'] += q
                    m['trade_notional_usdt'] += p * q
                    m['rpi_trades'] += int(row['RPI'])
                example = {k: row[k] for k in ('timestamp', 'symbol', 'side', 'size', 'price', 'tickDirection', 'RPI') if k in row}
                examples.setdefault('first', example)
                examples['last'] = example
            except (ValueError, KeyError, TypeError, OverflowError, ArithmeticError) as exc:
                counts['invalid_records'] += 1
                if len(errors) < 5:
                    errors.append({'row': counts['rows'], 'error': str(exc)[:160]})
            if time.monotonic() - last_log >= 30:
                print(f'trades parsed {counts["rows"]} records', flush=True)
                last_log = time.monotonic()
    return {'columns': fields, 'counters': dict(counts), 'coverage': cov.report(),
            'timestamp_units': dict(units), 'rpi_flag_counts': dict(flags),
            'examples': examples, 'errors': errors,
            'rpi_policy': 'All rows, including RPI=1, are retained and counted separately; raw archive preserves original flags. Aggregates do not deduplicate.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_cache = Path(__file__).resolve().parents[3] / 'market_data_cache' / 'btc-2026-09-01' / 'bybit'
    parser.add_argument('--cache', type=Path, default=Path(os.environ.get('MARKET_DATA_CACHE', str(default_cache))))
    args = parser.parse_args()
    cache = args.cache.expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if cache == repo or repo in cache.parents:
        raise ValueError('Raw cache must be outside the repository')
    cache.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cache).free
    print(f'disk free before download: {free} bytes; cache outside repository', flush=True)
    if free < 1024 ** 3:
        raise RuntimeError('At least 1 GiB free disk required')
    started = utc()
    report = {'venue': 'Bybit', 'symbol': 'BTCUSDT', 'market': 'linear perpetual',
              'interval': {'start_ms_inclusive': START, 'end_ms_exclusive': END,
                           'start_utc': '2026-09-01T00:00:00Z', 'end_utc': '2026-09-02T00:00:00Z'},
              'run_started_utc': started, 'script_sha256': sha(Path(__file__)),
              'semantics_reference': 'https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook',
              'disk_free_bytes_before_download': free, 'sources': {},
              'limitations': ['Free public market archives only; no private account data or paid services.',
                              'Exchange event/publication timestamps are not local receipt timestamps.',
                              'Price-level L2 and public trades cannot establish order queue position or exact maker fills.',
                              'Bybit public books exclude RPI orders; RPI trades are preserved separately and need not match displayed liquidity.',
                              'Historical account fees, latency and order eligibility are unverified; no net execution PnL is claimed.',
                              'SHA256 identifies downloaded bytes, not an independently published source checksum.',
                              'Minute coverage and monotonic identifiers do not prove that every exchange event was archived.']}
    minutes = [dict(minute_utc=datetime.fromtimestamp((START + i * 60000) / 1000, timezone.utc).isoformat(),
                    book_records=0, snapshots=0, deltas=0, crossed_books=0, empty_books=0,
                    last_bid='', last_ask='', trades=0, trade_size_btc=0.0,
                    trade_notional_usdt=0.0, rpi_trades=0) for i in range(1440)]
    for name, url in SOURCES.items():
        path, meta = download(url, cache)
        report['sources'][name] = meta
        report[name] = validate_books(path, minutes) if name == 'book' else validate_trades(path, minutes)
        print(f'{name} validation finished: {json.dumps(report[name]["coverage"])}', flush=True)
    output = Path(__file__).resolve().parent
    minute_path = output / 'bybit_minutes.csv'
    with minute_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(minutes[0]))
        writer.writeheader()
        writer.writerows(minutes)
    report['minute_summary'] = {'file': minute_path.name, 'rows': 1440, 'sha256': sha(minute_path),
                                'numeric_aggregation': 'Float totals; event timestamps classified using integer microseconds.'}
    issues = []
    for name in SOURCES:
        section = report[name]
        if section['counters'].get('invalid_records', 0):
            issues.append(name + ': invalid records')
        if section['coverage']['minutes_covered'] != 1440:
            issues.append(name + ': missing minutes')
        if section['coverage']['timestamp_regressions']:
            issues.append(name + ': timestamp regressions')
    for key in ('crossed_books', 'empty_books', 'locked_books', 'delta_u_regressions', 'delta_seq_regressions', 'duplicate_u_seq', 'deltas_before_snapshot'):
        if report['book']['counters'].get(key, 0):
            issues.append('book: ' + key)
    if report['trades']['counters'].get('duplicate_trade_ids', 0):
        issues.append('trades: duplicate trade IDs')
    report['validation'] = {'status': 'completed_with_findings' if issues else 'completed_no_detected_anomalies',
                            'findings': issues, 'all_records_streamed': True,
                            'zip_crc_and_gzip_crc': 'Checked by Python archive readers on full streams'}
    report['run_completed_utc'] = utc()
    (output / 'bybit_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    print(f'report written; {report["validation"]["status"]}', flush=True)


if __name__ == '__main__':
    main()
