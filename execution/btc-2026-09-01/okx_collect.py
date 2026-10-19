"""Download and validate the public OKX BTC swap archives for 2026-09-01 UTC."""
import argparse
import bisect
from collections import Counter
import csv
import hashlib
import io
import json
import shutil
import tarfile
import time
import urllib.request
import zipfile
import zlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

START = 1788220800000
END = 1788307200000
OUT = Path(__file__).resolve().parent
SOURCES = [
    ('books', 'https://static.okx.com/cdn/okx/match/orderbook/pro/L2/400lv/daily/20260901/BTC-USDT-SWAP-L2orderbook-400lv-2026-09-01.tar.gz'),
    ('trades', 'https://static.okx.com/cdn/okex/traderecords/trades/daily/20260901/BTC-USDT-SWAP-trades-2026-09-01.zip?v=1001'),
    ('trades', 'https://static.okx.com/cdn/okex/traderecords/trades/daily/20260902/BTC-USDT-SWAP-trades-2026-09-02.zip?v=1001'),
]


def utc(ms=None):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms is not None else datetime.now(timezone.utc).isoformat()


def download(cache, kind, url):
    path = cache / url.split('/')[-1].split('?')[0]
    meta = {'kind': kind, 'url': url, 'file': path.name, 'started_utc': utc(), 'attempts': []}
    began = time.monotonic()
    for attempt in range(3):
        try:
            if path.exists():
                meta['cache_hit'] = True
                break
            part = path.with_suffix(path.suffix + '.part')
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'OKX-public-archive-validator/1.0'}), timeout=60) as response:
                expected = int(response.headers.get('Content-Length', 0))
                if shutil.disk_usage(cache).free < max(expected, 500_000_000) + 256_000_000:
                    raise OSError('Insufficient free cache disk space')
                size = 0
                with part.open('wb') as target:
                    while chunk := response.read(1024 * 1024):
                        target.write(chunk)
                        size += len(chunk)
                if expected and expected != size:
                    raise OSError('Content-Length mismatch')
                meta['content_length'] = expected or None
                meta['last_modified'] = response.headers.get('Last-Modified')
            part.replace(path)
            meta['cache_hit'] = False
            break
        except Exception as exc:
            meta['attempts'].append({'attempt': attempt + 1, 'error': type(exc).__name__, 'detail': str(exc).replace(str(cache), '<cache>')})
            if attempt == 2:
                meta['failure'] = 'download_failed'
                return None, meta
            time.sleep(2 ** attempt)
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    meta.update(bytes=path.stat().st_size, sha256=digest.hexdigest(), finished_utc=utc(), elapsed_seconds=round(time.monotonic() - began, 3))
    provenance = path.with_name(path.name + '.download.json')
    if meta['cache_hit'] and provenance.exists():
        prior = json.loads(provenance.read_text(encoding='utf-8'))
        if prior.get('sha256') == meta['sha256']:
            meta['original_download'] = prior
            meta['cache_sha256_verified'] = True
        else:
            meta['failure'] = 'cache_sha256_mismatch'
            return None, meta
    elif not meta['cache_hit']:
        provenance.write_text(json.dumps(meta, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'downloaded': path.name, 'bytes': meta['bytes'], 'sha256': meta['sha256']}), flush=True)
    return path, meta


def timestamp(value):
    text = str(value).strip()
    if text.lstrip('-').isdigit():
        return int(text)
    parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('ISO timestamp requires explicit timezone')
    return int(parsed.timestamp() * 1000)


class Coverage:
    def __init__(self):
        self.count = 0
        self.first = None
        self.last = None
        self.previous = None
        self.out_of_order = 0
        self.longest = 0
        self.minutes = [0] * 1440

    def add(self, ts):
        if self.previous is not None and ts < self.previous:
            self.out_of_order += 1
        self.previous = ts
        if not START <= ts < END:
            return False
        self.count += 1
        self.first = ts if self.first is None else min(self.first, ts)
        self.last = ts if self.last is None else max(self.last, ts)
        self.minutes[(ts - START) // 60000] += 1
        return True

    def report(self):
        missing = [i for i, count in enumerate(self.minutes) if not count]
        run = longest = 0
        for count in self.minutes:
            run = 0 if count else run + 1
            longest = max(longest, run)
        return {'records_in_window': self.count, 'first_ms': self.first, 'last_ms': self.last,
                'first_utc': utc(self.first) if self.first is not None else None,
                'last_utc': utc(self.last) if self.last is not None else None,
                'minutes_present': 1440 - len(missing), 'missing_minute_indices': missing,
                'longest_missing_minute_run': longest, 'out_of_order_records': self.out_of_order,
                'longest_event_or_boundary_gap_ms': max(self.longest, (self.first - START) if self.first else END - START,
                    (END - self.last) if self.last else END - START)}


def validate(downloaded):
    report = {'instrument': 'BTC-USDT-SWAP', 'start_ms_inclusive': START, 'end_ms_exclusive': END,
              'started_utc': utc(), 'sources': [meta for _, meta in downloaded],
              'quantity': {'native_unit': 'contracts', 'conversion_applied': False,
                           'current_ctVal_btc_reference_only': '0.01',
                           'historical_ctVal_verified': False},
              'failures': [], 'limitations': [], 'schemas': {}}
    rows = [{'minute_utc': utc(START + i * 60000), 'book_records': 0, 'trade_records': 0,
             'trade_qty_contracts': Decimal(0), 'trade_price_min': None, 'trade_price_max': None,
             'spread_min': None, 'spread_max': None, 'bid_last': None, 'ask_last': None,
             'trades_with_book': 0, 'trades_without_book': 0,
             'trade_book_spread_min': None, 'trade_book_spread_max': None,
             'trade_book_age_max_ms': None} for i in range(1440)]
    counts = Counter()
    trades = []
    trade_coverage = Coverage()
    seen_ids = set()
    trade_previous = None
    for path, meta in downloaded:
        if meta['kind'] != 'trades' or path is None:
            continue
        file_counts = Counter()
        try:
            with zipfile.ZipFile(path) as archive:
                meta['entries'] = []
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    meta['entries'].append({'name': member.filename, 'uncompressed_bytes': member.file_size})
                    with archive.open(member) as stream:
                        reader = csv.DictReader(io.TextIOWrapper(stream, encoding='utf-8-sig', newline=''))
                        report['schemas'][member.filename] = {'fields': reader.fieldnames}
                        for record in reader:
                            file_counts['records'] += 1
                            try:
                                ts = timestamp(record.get('created_time', record.get('ts')))
                                meta['first_timestamp_ms'] = min(meta.get('first_timestamp_ms', ts), ts)
                                meta['last_timestamp_ms'] = max(meta.get('last_timestamp_ms', ts), ts)
                                price = Decimal(record.get('price', record.get('px')))
                                qty = Decimal(record.get('size', record.get('sz')))
                                if not price.is_finite() or not qty.is_finite() or price <= 0 or qty <= 0:
                                    raise ValueError('Invalid price or quantity')
                                if record.get('instrument_name', 'BTC-USDT-SWAP') != 'BTC-USDT-SWAP' or record.get('side') not in ('buy', 'sell'):
                                    raise ValueError('Invalid trade instrument or side')
                                if trade_previous is not None and ts >= trade_previous and START <= ts < END and START <= trade_previous < END:
                                    trade_coverage.longest = max(trade_coverage.longest, ts - trade_previous)
                                trade_previous = ts
                                inside = trade_coverage.add(ts)
                                if not inside:
                                    file_counts['outside_window'] += 1
                                    continue
                                ident = record.get('trade_id', record.get('tradeId'))
                                if ident is None:
                                    counts['trade_missing_id'] += 1
                                elif ident in seen_ids:
                                    counts['trade_duplicates'] += 1
                                    continue
                                else:
                                    seen_ids.add(ident)
                                row = rows[(ts - START) // 60000]
                                row['trade_records'] += 1
                                row['trade_qty_contracts'] += qty
                                extrema(row, 'trade_price', price)
                                trades.append(ts)
                            except Exception:
                                counts['trade_invalid_records'] += 1
                meta['validation'] = dict(file_counts)
                meta['complete_archive_read'] = True
        except Exception as exc:
            report['failures'].append({'source': path.name, 'error': type(exc).__name__, 'detail': 'Trade archive could not be completely read'})
    trades.sort()
    seen_ids.clear()
    book_coverage = Coverage()
    book_previous = None
    bids, asks = {}, {}
    bid_keys, ask_keys = [], []
    previous_seq = None
    previous_record = None
    book_state = None
    trade_index = 0
    schema_shapes = Counter()
    action_counts = Counter()

    def match_until(limit):
        nonlocal trade_index
        while trade_index < len(trades) and trades[trade_index] < limit:
            ts = trades[trade_index]
            row = rows[(ts - START) // 60000]
            if book_state is None or book_state[0] > ts:
                row['trades_without_book'] += 1
            else:
                bt, bid, ask = book_state
                row['trades_with_book'] += 1
                extrema(row, 'trade_book_spread', ask - bid)
                row['trade_book_age_max_ms'] = max(row['trade_book_age_max_ms'] or 0, ts - bt)
            trade_index += 1

    for path, meta in downloaded:
        if meta['kind'] != 'books' or path is None:
            continue
        try:
            with tarfile.open(path, 'r|gz') as archive:
                meta['entries'] = []
                for member in archive:
                    if not member.isfile():
                        continue
                    meta['entries'].append({'name': member.name, 'uncompressed_bytes': member.size})
                    for line in archive.extractfile(member):
                        if not line.strip():
                            continue
                        counts['book_lines'] += 1
                        try:
                            envelope = json.loads(line)
                            records = envelope.get('data', [envelope])
                            if isinstance(records, dict):
                                records = [records]
                            for record in records:
                                counts['book_records_total'] += 1
                                schema_shapes[','.join(sorted(record))] += 1
                                action = record.get('action', envelope.get('action'))
                                action_counts[action] += 1
                                ts = timestamp(record['ts'])
                                inside = book_coverage.add(ts)
                                if book_previous is not None and ts >= book_previous and inside and START <= book_previous < END:
                                    book_coverage.longest = max(book_coverage.longest, ts - book_previous)
                                book_previous = ts
                                match_until(ts)
                                if record.get('instId', envelope.get('arg', {}).get('instId', 'BTC-USDT-SWAP')) != 'BTC-USDT-SWAP':
                                    raise ValueError('Wrong book instrument')
                                if record == previous_record:
                                    counts['book_duplicates'] += 1
                                previous_record = record
                                seq = int(record['seqId']) if 'seqId' in record else None
                                prev = int(record['prevSeqId']) if 'prevSeqId' in record else None
                                if seq is not None:
                                    counts['book_seq_present'] += 1
                                heartbeat = action == 'update' and not record.get('bids') and not record.get('asks') and seq is not None and seq == prev == previous_seq
                                if heartbeat:
                                    counts['book_ignorable_heartbeats'] += 1
                                    continue
                                if action == 'snapshot':
                                    if bids or asks:
                                        counts['book_snapshot_resets'] += 1
                                    bids.clear(); asks.clear(); bid_keys.clear(); ask_keys.clear()
                                    previous_seq = None
                                elif action != 'update':
                                    raise ValueError('Unknown book action')
                                elif not bids or not asks:
                                    counts['book_updates_without_snapshot'] += 1
                                if action == 'update' and prev is not None and previous_seq is not None:
                                    counts['book_prev_seq_tests'] += 1
                                    if prev != previous_seq:
                                        counts['book_sequence_gaps'] += 1
                                    if seq is not None and seq < previous_seq:
                                        counts['book_sequence_resets'] += 1
                                    elif seq == previous_seq:
                                        counts['book_duplicate_sequences'] += 1
                                previous_seq = seq
                                for side, keys, levels in ((bids, bid_keys, record.get('bids', [])), (asks, ask_keys, record.get('asks', []))):
                                    for level in levels:
                                        price, qty = Decimal(level[0]), Decimal(level[1])
                                        if not price.is_finite() or not qty.is_finite() or price <= 0 or qty < 0:
                                            raise ValueError('Invalid book level')
                                        if qty == 0:
                                            if price in side:
                                                del side[price]
                                                keys.pop(bisect.bisect_left(keys, price))
                                        else:
                                            if price not in side:
                                                bisect.insort(keys, price)
                                            side[price] = (str(level[0]), str(level[1]))
                                if 'checksum' in record:
                                    counts['book_checksum_tests'] += 1
                                    parts = []
                                    top_b, top_a = bid_keys[-25:][::-1], ask_keys[:25]
                                    for i in range(max(len(top_b), len(top_a))):
                                        if i < len(top_b):
                                            parts.extend(bids[top_b[i]])
                                        if i < len(top_a):
                                            parts.extend(asks[top_a[i]])
                                    crc = zlib.crc32(':'.join(parts).encode())
                                    signed = crc if crc < 2 ** 31 else crc - 2 ** 32
                                    if signed != int(record['checksum']):
                                        counts['book_checksum_failures'] += 1
                                if not bids or not asks:
                                    counts['book_empty_side'] += 1
                                    book_state = None
                                else:
                                    bid, ask = bid_keys[-1], ask_keys[0]
                                    if bid >= ask:
                                        counts['book_crossed_or_locked'] += 1
                                    book_state = (ts, bid, ask)
                                    if inside:
                                        row = rows[(ts - START) // 60000]
                                        row['book_records'] += 1
                                        row['bid_last'], row['ask_last'] = bid, ask
                                        extrema(row, 'spread', ask - bid)
                                if counts['book_records_total'] % 500000 == 0:
                                    print(json.dumps({'books_processed': counts['book_records_total'], 'timestamp_utc': utc(ts)}), flush=True)
                        except Exception as exc:
                            counts['book_invalid_records'] += 1
                            if 'first_book_error' not in report:
                                report['first_book_error'] = type(exc).__name__ + ': ' + str(exc)[:180]
            meta['complete_archive_read'] = True
        except Exception as exc:
            report['failures'].append({'source': path.name, 'error': type(exc).__name__, 'detail': 'Book archive could not be completely read'})
    match_until(END)
    report['schemas']['books'] = {'record_key_shapes': dict(schema_shapes), 'actions': dict(action_counts),
        'level_format': ['raw_price', 'raw_quantity_contracts', 'order_count'],
        'snapshot_replaces_book': True, 'zero_quantity_deletes_level': True}
    report['books'] = book_coverage.report()
    report['trades'] = trade_coverage.report()
    for key in ('trade_duplicates', 'trade_invalid_records', 'trade_missing_id', 'book_duplicates',
                'book_invalid_records', 'book_updates_without_snapshot', 'book_seq_present',
                'book_prev_seq_tests', 'book_sequence_gaps', 'book_sequence_resets',
                'book_duplicate_sequences', 'book_ignorable_heartbeats', 'book_snapshot_resets',
                'book_checksum_tests', 'book_checksum_failures', 'book_crossed_or_locked', 'book_empty_side'):
        counts.setdefault(key, 0)
    report['counts'] = dict(counts)
    report['trade_book_matching'] = {'rule': 'Latest book timestamp <= trade timestamp; all book updates at equal timestamp applied before matching',
        'matched': sum(r['trades_with_book'] for r in rows), 'unmatched': sum(r['trades_without_book'] for r in rows)}
    if not counts['book_seq_present']:
        report['limitations'].append('Archive supplies no seqId/prevSeqId; exact sequence continuity and sequence gap/reset checks unavailable. Timestamp gaps are measured without inferring packet loss.')
    if not counts['book_checksum_tests']:
        report['limitations'].append('Archive supplies no checksum; signed CRC32 validation unavailable.')
    for _, meta in downloaded:
        if 'failure' in meta:
            report['failures'].append({'source': meta['file'], 'error': meta['failure']})
    bad = ('trade_invalid_records', 'trade_duplicates', 'book_invalid_records', 'book_duplicates',
           'book_updates_without_snapshot', 'book_sequence_gaps', 'book_checksum_failures', 'book_crossed_or_locked', 'book_empty_side')
    for key in bad:
        if counts[key]:
            report['failures'].append({'check': key, 'count': counts[key]})
    for name in ('books', 'trades'):
        if report[name]['minutes_present'] != 1440 or report[name]['out_of_order_records']:
            report['failures'].append({'check': name + '_coverage_or_order'})
    report['status'] = 'failed' if report['failures'] else 'passed_with_limitations' if report['limitations'] else 'passed'
    report['finished_utc'] = utc()
    with (OUT / 'okx_minutes.csv').open('w', encoding='utf-8', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (OUT / 'okx_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'books': report['books'], 'trades': report['trades'], 'counts': report['counts'], 'failures': report['failures']}), flush=True)
    return report


def extrema(row, prefix, value):
    lo, hi = prefix + '_min', prefix + '_max'
    row[lo] = value if row[lo] is None else min(row[lo], value)
    row[hi] = value if row[hi] is None else max(row[hi], value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=OUT.parents[2] / 'market_data_cache' / 'btc-2026-09-01' / 'okx')
    parser.add_argument('--inspect', action='store_true')
    args = parser.parse_args()
    if args.cache.resolve().is_relative_to(OUT.parents[1]):
        parser.error('--cache must be outside the repository')
    args.cache.mkdir(parents=True, exist_ok=True)
    downloaded = [download(args.cache, kind, url) for kind, url in SOURCES]
    if args.inspect:
        for path, meta in downloaded:
            print(json.dumps(meta), flush=True)
            if path is None:
                continue
            if path.name.endswith('.gz'):
                with tarfile.open(path, 'r|gz') as archive:
                    for member in archive:
                        if member.isfile():
                            stream = archive.extractfile(member)
                            print(member.name, member.size, flush=True)
                            for _ in range(3):
                                print(stream.readline()[:12000].decode('utf-8-sig'), flush=True)
                            break
            else:
                with zipfile.ZipFile(path) as archive:
                    print(archive.namelist(), flush=True)
                    with archive.open(archive.namelist()[0]) as stream:
                        for _ in range(4):
                            print(stream.readline().decode('utf-8-sig'), flush=True)
    else:
        result = validate(downloaded)
        raise SystemExit(1 if result['failures'] else 0)


if __name__ == '__main__':
    main()
