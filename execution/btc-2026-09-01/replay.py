"""Single-day public-data execution experiment; no claim of realized account profit.

Run: python replay.py [--cache PATH] [--self-test-only]
Only the four named outputs are written. Raw archives are streamed, never extracted.
"""
import argparse
from array import array
from bisect import bisect_left, insort
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
import zipfile

import numpy as np

START = 1788220800000
END = START + 86400000
STEP = 100
N = (END - START) // STEP
QTY = 0.01
CONTRACT_BTC = 0.01  # Historical contract specification is unverified.
BYBIT_TAKER = 5.5
OKX_TAKER = 5.0
MAX_AGE = 1000
HOLD = 60000
QUOTE_LATENCY = 100
OUTPUT = Path(__file__).resolve().parent


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def utc(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()


class Grid:
    def __init__(self):
        self.observed = np.full(N, -1, dtype=np.int64)
        self.levels = np.zeros((N, 2, 10, 2), dtype=np.float64)
        self.next = 0
        self.bids, self.asks = {}, {}
        self.bkeys, self.akeys = [], []
        self.last = -1
        self.valid = False
        self.counts = Counter()

    def emit_before(self, ts):
        # Emit before applying equal-time events: every stored observation is < grid time.
        stop = min(N, max(0, (ts - START) // STEP + 1))
        if stop <= self.next:
            return
        if self.valid and self.last >= 0:
            levels = np.zeros((2, 10, 2))
            for side, keys, book in ((0, self.bkeys[-10:][::-1], self.bids),
                                     (1, self.akeys[:10], self.asks)):
                for j, p in enumerate(keys):
                    levels[side, j] = p, book[p]
            self.observed[self.next:stop] = self.last
            self.levels[self.next:stop] = levels
        self.next = stop

    def update(self, ts, bids, asks, snapshot, factor=1.0):
        if ts < self.last:
            raise ValueError('Book timestamps regress')
        self.emit_before(ts)
        if snapshot:
            self.bids.clear(); self.asks.clear()
            self.bkeys.clear(); self.akeys.clear()
            self.valid = True
            self.counts['snapshots'] += 1
        elif not self.valid:
            self.counts['updates_without_snapshot'] += 1
            raise ValueError('No initial snapshot')
        for book, keys, levels in ((self.bids, self.bkeys, bids), (self.asks, self.akeys, asks)):
            for level in levels:
                p, q = float(level[0]), float(level[1]) * factor
                if not np.isfinite(p) or not np.isfinite(q) or p <= 0 or q < 0:
                    raise ValueError('Invalid level')
                if q == 0:
                    if p in book:
                        del book[p]
                        keys.pop(bisect_left(keys, p))
                else:
                    if p not in book:
                        insort(keys, p)
                    book[p] = q
        self.last = ts
        self.valid = bool(self.bkeys and self.akeys and self.bkeys[-1] < self.akeys[0])
        if not self.valid:
            raise ValueError('Empty, locked or crossed book')
        self.counts['records'] += 1
        if self.counts['records'] % 250000 == 0:
            print(f"book records={self.counts['records']} time={utc(ts)}", flush=True)

    def at(self, ts):
        i = int((ts - START) // STEP)
        if i < 0 or i >= N:
            return None
        observed = int(self.observed[i])
        if observed < 0 or observed >= ts or ts - observed > MAX_AGE:
            return None
        return self.levels[i], observed


def read_bybit_book(path):
    grid = Grid()
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            with archive.open(member) as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    d = r['data']
                    if d['s'] != 'BTCUSDT' or r['topic'] != 'orderbook.200.BTCUSDT':
                        raise ValueError('Unexpected Bybit instrument')
                    grid.update(int(r['ts']), d['b'], d['a'], r['type'] == 'snapshot' or d['u'] == 1)
    grid.emit_before(END)
    return grid


def read_okx_book(path):
    grid = Grid()
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            if not member.isfile():
                continue
            for line in archive.extractfile(member):
                if not line.strip():
                    continue
                envelope = json.loads(line)
                records = envelope.get('data', [envelope])
                if isinstance(records, dict):
                    records = [records]
                for r in records:
                    action = r.get('action', envelope.get('action'))
                    if action not in ('snapshot', 'update'):
                        raise ValueError('Unexpected OKX book action')
                    grid.update(int(r['ts']), r.get('bids', []), r.get('asks', []),
                                action == 'snapshot', CONTRACT_BTC)
    grid.emit_before(END)
    return grid


def read_bybit_trades(path):
    ts, prices, qty, side = array('q'), array('d'), array('d'), array('b')
    counts = Counter()
    previous = -1
    with gzip.open(path, 'rt', newline='', encoding='utf-8') as stream:
        for r in csv.DictReader(stream):
            counts['all_rows'] += 1
            us = int(Decimal(r['timestamp']) * 1000000)
            if us < previous:
                raise ValueError('Trade timestamps regress')
            previous = us
            if r['symbol'] != 'BTCUSDT' or r['side'] not in ('Buy', 'Sell'):
                raise ValueError('Unexpected Bybit trade')
            if r['RPI'] == '1':
                counts['excluded_rpi'] += 1
                continue
            if START * 1000 <= us < END * 1000:
                p, q = float(r['price']), float(r['size'])
                if not np.isfinite(p) or not np.isfinite(q) or p <= 0 or q <= 0:
                    raise ValueError('Invalid trade')
                ts.append(us); prices.append(p); qty.append(q)
                side.append(1 if r['side'] == 'Buy' else -1)
                counts['eligible_rows'] += 1
            if counts['all_rows'] % 1000000 == 0:
                print(f"Bybit trades={counts['all_rows']}", flush=True)
    return tuple(np.frombuffer(v, dtype=d) for v, d in
                 ((ts, np.int64), (prices, np.float64), (qty, np.float64), (side, np.int8))), dict(counts)


def audit_okx_trades(paths):
    counts = Counter()
    first, last = None, None
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                with archive.open(member) as stream:
                    reader = csv.DictReader(io.TextIOWrapper(stream, encoding='utf-8-sig', newline=''))
                    for r in reader:
                        counts[path.name] += 1
                        ts = int(r.get('created_time', r.get('ts')))
                        p, q = float(r.get('price', r.get('px'))), float(r.get('size', r.get('sz')))
                        if not np.isfinite(p) or not np.isfinite(q) or p <= 0 or q <= 0 or r['side'] not in ('buy', 'sell'):
                            raise ValueError('Invalid OKX trade')
                        if START <= ts < END:
                            counts['in_day_rows'] += 1
                            first = ts if first is None else min(first, ts)
                            last = ts if last is None else max(last, ts)
    return dict(counts, first_ms=first, last_ms=last,
                use='Full-file audit only; OKX hedge execution uses displayed depth, not trades.')


def vwap(book, side, quantity=QTY):
    left, value = quantity, 0.0
    for p, q in book[side]:
        take = min(left, q)
        value += take * p
        left -= take
        if left <= 1e-10:
            return value / quantity
    return None


def queue_fill(times, prices, quantities, sides, activation, expiry, price, direction, ahead):
    lo = np.searchsorted(times, int(round(activation * 1000)), side='right')
    hi = np.searchsorted(times, int(round(expiry * 1000)), side='left')
    filled, first, full = 0.0, None, None
    for k in range(lo, hi):
        if sides[k] != -direction or abs(prices[k] - price) > 1e-7:
            continue
        volume = quantities[k]
        consumed = min(ahead, volume)
        ahead -= consumed
        volume -= consumed
        added = min(QTY - filled, volume)
        if added > 1e-12:
            first = float(times[k]) / 1000 if first is None else first
            filled += added
            if filled >= QTY - 1e-10:
                full = float(times[k]) / 1000
                break
    return filled, first, full


def safe_interval(decision, duration):
    # Exclude every hourly boundary without assuming the historical funding schedule.
    return decision + duration < END and decision // 3600000 == (decision + duration) // 3600000


def execution_row(scenario, kind, decision, direction):
    return dict(scenario=scenario, experiment=kind, decision_ms=decision,
                direction='buy_bybit_sell_okx' if direction == 1 else 'sell_bybit_buy_okx',
                quantity_btc=QTY, status='', quote_ms='', activation_ms='', fill_ms='',
                hedge_ms='', exit_ms='', queue_ahead_btc='', filled_btc=0.0,
                unhedged_ms=0.0, bybit_entry='', okx_entry='', bybit_exit='', okx_exit='',
                gross_usdt='', fees_usdt='', pnl_usdt='', bybit_entry_observed_ms='',
                okx_entry_observed_ms='', bybit_exit_observed_ms='', okx_exit_observed_ms='')


def close_pair(row, bybit, okx, direction, quantity, bp, op, hedge, maker_fee):
    exit_ts = hedge + HOLD
    row.update(hedge_ms=hedge, exit_ms=exit_ts, bybit_entry=bp, okx_entry=op,
               filled_btc=quantity)
    b, o = bybit.at(exit_ts), okx.at(exit_ts)
    if b is None or o is None:
        row['status'] = 'unresolved_exit_missing_or_stale'
        return
    bx, ox = vwap(b[0], 0 if direction == 1 else 1, quantity), vwap(o[0], 1 if direction == 1 else 0, quantity)
    if bx is None or ox is None:
        row['status'] = 'unresolved_exit_depth'
        return
    gross = quantity * direction * (bx - bp + op - ox)
    fees = quantity * (bp * maker_fee + op * OKX_TAKER + bx * BYBIT_TAKER + ox * OKX_TAKER) / 10000
    row.update(status='closed_pair', bybit_exit=bx, okx_exit=ox,
               bybit_exit_observed_ms=b[1], okx_exit_observed_ms=o[1],
               gross_usdt=gross, fees_usdt=fees, pnl_usdt=gross - fees)


def maker_attempt(name, kind, decision, direction, ttl, latency, fee, bybit, okx, trades):
    row = execution_row(name, kind, decision, direction)
    activation = decision + QUOTE_LATENCY
    expiry = activation + ttl
    row.update(quote_ms=decision, activation_ms=activation)
    initial, active = bybit.at(decision), bybit.at(activation)
    if initial is None or active is None:
        row['status'] = 'quote_missing_or_stale'
        return row, expiry
    side = 0 if direction == 1 else 1
    price = initial[0][side, 0, 0]
    row['bybit_entry_observed_ms'] = active[1]
    if (direction == 1 and price >= active[0][1, 0, 0]) or (direction == -1 and price <= active[0][0, 0, 0]):
        row['status'] = 'post_only_reject'
        return row, activation
    levels = active[0][side]
    matching = levels[np.abs(levels[:, 0] - price) < 1e-7]
    if not len(matching):
        row['status'] = 'activation_price_outside_top10'
        return row, expiry
    ahead = max(initial[0][side, 0, 1], matching[0, 1])
    row['queue_ahead_btc'] = ahead
    filled, first, full = queue_fill(*trades, activation, expiry, price, direction, ahead)
    row.update(filled_btc=filled, bybit_entry=price)
    if first is None:
        row['status'] = 'expired_unfilled'
        return row, expiry
    row['fill_ms'] = full if full is not None else first
    hedge = (full if full is not None else expiry) + latency
    row['unhedged_ms'] = hedge - first
    if full is None:
        # Sub-contract maker fills cannot be hedged as one OKX contract.
        b = bybit.at(hedge)
        bx = vwap(b[0], 0 if direction == 1 else 1, filled) if b else None
        if bx is None:
            row['status'] = 'unresolved_partial_unwind'
            return row, END
        gross = filled * direction * (bx - price)
        fees = filled * (price * fee + bx * BYBIT_TAKER) / 10000
        row.update(status='partial_bybit_unwind', exit_ms=hedge, bybit_exit=bx,
                   bybit_exit_observed_ms=b[1], gross_usdt=gross, fees_usdt=fees, pnl_usdt=gross - fees)
        return row, hedge
    o = okx.at(hedge)
    op = vwap(o[0], 0 if direction == 1 else 1) if o else None
    if op is None:
        # Fail the hedge explicitly and flatten the maker exposure on Bybit.
        b = bybit.at(hedge)
        bx = vwap(b[0], 0 if direction == 1 else 1) if b else None
        if bx is None:
            row['status'] = 'unresolved_failed_hedge_unwind'
        else:
            gross = QTY * direction * (bx - price)
            fees = QTY * (price * fee + bx * BYBIT_TAKER) / 10000
            row.update(status='failed_hedge_bybit_unwind', exit_ms=hedge, bybit_exit=bx,
                       bybit_exit_observed_ms=b[1], gross_usdt=gross, fees_usdt=fees, pnl_usdt=gross - fees)
        return row, END if row['status'].startswith('unresolved') else hedge
    row['okx_entry_observed_ms'] = o[1]
    close_pair(row, bybit, okx, direction, QTY, price, op, hedge, fee)
    return row, END if row['status'].startswith('unresolved') else hedge + HOLD


def summary(rows):
    settled = [r for r in rows if r['pnl_usdt'] != '']
    pair = [r for r in rows if r['status'] == 'closed_pair']
    pnl = [float(r['pnl_usdt']) for r in settled]
    return dict(attempts=len(rows), statuses=dict(Counter(r['status'] for r in rows)),
                closed_pairs=len(pair), settled_experiments=len(settled),
                gross_usdt=sum(float(r['gross_usdt']) for r in settled),
                fees_usdt=sum(float(r['fees_usdt']) for r in settled), pnl_usdt=sum(pnl),
                mean_pnl_usdt=float(np.mean(pnl)) if pnl else None,
                win_fraction=float(np.mean(np.array(pnl) > 0)) if pnl else None,
                max_unhedged_ms=max((r['unhedged_ms'] for r in rows), default=0),
                unresolved=sum(r['status'].startswith('unresolved') for r in rows))


def self_test():
    tests = []
    g = Grid()
    g.update(START + 20, [[100, 1]], [[101, 1]], True)
    g.update(START + 100, [[100, 2]], [], False)
    assert g.at(START + 100)[0][0, 0, 1] == 1
    assert g.at(START + 100)[1] == START + 20
    g.emit_before(START + 2000)
    assert g.at(START + 1200) is None
    tests.append('strict-past equal-time snapshot and stale rejection')
    assert abs(vwap(np.array([[[100, .004], [99, .006]], [[101, .01], [102, 1]]]), 0) - 99.4) < 1e-10
    assert vwap(np.array([[[100, .001]], [[101, .001]]]), 0) is None
    tests.append('multi-level depth VWAP and insufficient depth')
    t = np.array([100000, 101000, 102000, 103000, 104000, 105000], dtype=np.int64)
    p = np.array([100., 100., 100., 99., 100., 100.])
    q = np.array([9., .5, .5, 9., .006, .004])
    s = np.array([-1, -1, -1, -1, -1, -1])
    filled, first, full = queue_fill(t, p, q, s, 100, 106, 100, 1, 1.)
    assert abs(filled - .01) < 1e-10 and first == 104 and full == 105
    assert queue_fill(t, p, q, s, 100, 105, 100, 1, 1.)[2] is None
    tests.append('pre-activation and wrong-price exclusion, displayed queue depletion, partial fill and TTL')
    assert safe_interval(START + 1000, 66000)
    assert not safe_interval(START + 3590000, 66000)
    assert not safe_interval(END - 1000, 66000)
    tests.append('funding-boundary exclusion and end-of-day closure guard')
    class SyntheticBook:
        def __init__(self, bid, ask):
            self.book = np.array([[[bid, 1.0]], [[ask, 1.0]]])
        def at(self, ts):
            return self.book, int(ts) - 1
    b, o = SyntheticBook(100, 101), SyntheticBook(102, 103)
    decision = START + 10000
    activation = decision + QUOTE_LATENCY
    synthetic = (np.array([int((activation + 1) * 1000), int((activation + 2) * 1000)]),
                 np.array([100., 100.]), np.array([1., .01]), np.array([-1, -1]))
    r, busy = maker_attempt('test', 'test', decision, 1, 1000, 500, 2, b, o, synthetic)
    assert r['status'] == 'closed_pair' and r['hedge_ms'] == activation + 502
    assert r['exit_ms'] == r['hedge_ms'] + HOLD and busy == r['exit_ms']
    expected_gross = .01 * (100 - 100 + 102 - 103)
    expected_fees = .01 * (100 * 2 + 102 * 5 + 100 * 5.5 + 103 * 5) / 10000
    assert abs(r['pnl_usdt'] - (expected_gross - expected_fees)) < 1e-12
    assert r['unhedged_ms'] == 500
    tests.append('synthetic maker fill, delayed hedge, 60s liquidation and all four fees')
    synthetic = (synthetic[0], synthetic[1], np.array([1., .004]), synthetic[3])
    r, _ = maker_attempt('test', 'test', decision, 1, 1000, 100, 2, b, o, synthetic)
    assert r['status'] == 'partial_bybit_unwind' and abs(r['filled_btc'] - .004) < 1e-12
    assert r['okx_entry'] == '' and r['fees_usdt'] > 0
    tests.append('synthetic partial maker fill flattened without fractional OKX hedge')
    class CrossingBook(SyntheticBook):
        def at(self, ts):
            if ts >= activation:
                return np.array([[[98., 1.]], [[99., 1.]]]), int(ts) - 1
            return super().at(ts)
    r, _ = maker_attempt('test', 'test', decision, 1, 1000, 100, 2, CrossingBook(100, 101), o, synthetic)
    assert r['status'] == 'post_only_reject'
    tests.append('synthetic quote crossing at activation rejected post-only')
    del g
    return tests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=Path(__file__).resolve().parents[3] / 'market_data_cache' / 'btc-2026-09-01')
    parser.add_argument('--self-test-only', action='store_true')
    args = parser.parse_args()
    started = time.time()
    tests = self_test()
    print('Self tests passed: ' + '; '.join(tests), flush=True)
    if args.self_test_only:
        return
    cache = args.cache.resolve()
    repo = OUTPUT.parents[1]
    if cache == repo or repo in cache.parents:
        raise ValueError('Raw cache must be outside crypto-market-research repository')
    paths = dict(bybit_book=cache / 'bybit/2026-09-01_BTCUSDT_ob200.data.zip',
                 bybit_trades=cache / 'bybit/BTCUSDT2026-09-01.csv.gz',
                 okx_book=cache / 'okx/BTC-USDT-SWAP-L2orderbook-400lv-2026-09-01.tar.gz')
    for day in ('01', '02'):
        paths['okx_trades_' + day] = cache / f'okx/BTC-USDT-SWAP-trades-2026-09-{day}.zip'
    sources = {key: dict(file=path.name, bytes=path.stat().st_size, sha256=sha(path)) for key, path in paths.items()}
    print('Streaming complete Bybit book archive', flush=True)
    bybit = read_bybit_book(paths['bybit_book'])
    print('Streaming complete OKX book archive', flush=True)
    okx = read_okx_book(paths['okx_book'])
    print('Streaming complete Bybit trade archive', flush=True)
    trades, trade_counts = read_bybit_trades(paths['bybit_trades'])
    print('Auditing both complete OKX trade archives', flush=True)
    okx_audit = audit_okx_trades([paths['okx_trades_01'], paths['okx_trades_02']])
    rows, opportunities = [], []
    scan_counts = Counter()
    maximum = None
    busy_taker = START
    for decision in range(START + 1000, END, 1000):
        b, o = bybit.at(decision), okx.at(decision)
        if b is None or o is None:
            scan_counts['missing_or_stale_seconds'] += 1
            continue
        for direction in (1, -1):
            bp = vwap(b[0], 1 if direction == 1 else 0)
            op = vwap(o[0], 0 if direction == 1 else 1)
            if bp is None or op is None:
                scan_counts['insufficient_depth_directions'] += 1
                continue
            mid = (bp + op) / 2
            spread = direction * (op - bp) / mid * 10000
            fee_bps = (bp * BYBIT_TAKER + op * OKX_TAKER) / mid
            net = spread - fee_bps
            item = dict(decision_ms=decision, direction=direction, bybit_observed_ms=b[1],
                        okx_observed_ms=o[1], bybit_entry_vwap=bp, okx_entry_vwap=op,
                        gross_cross_bps=spread, entry_fee_bps=fee_bps, net_entry_bps=net,
                        quantity_btc=QTY, roundtrip_zero_exit_basis_net_bps=net - BYBIT_TAKER - OKX_TAKER)
            opportunities.append(item)
            scan_counts['valid_directions'] += 1
            scan_counts['positive_gross_directions'] += int(spread > 0)
            scan_counts['positive_after_entry_fees_directions'] += int(net > 0)
            if maximum is None or net > maximum['net_entry_bps']:
                maximum = item
            if net <= BYBIT_TAKER + OKX_TAKER or decision < busy_taker or not safe_interval(decision, HOLD + 1000):
                continue
            entry = decision + 100
            be, oe = bybit.at(entry), okx.at(entry)
            r = execution_row('taker_threshold_100ms', 'threshold', decision, direction)
            if be is None or oe is None:
                r['status'] = 'entry_missing_or_stale'
            else:
                ep = vwap(be[0], 1 if direction == 1 else 0)
                hp = vwap(oe[0], 0 if direction == 1 else 1)
                if ep is None or hp is None:
                    r['status'] = 'entry_insufficient_depth'
                else:
                    r.update(fill_ms=entry, bybit_entry_observed_ms=be[1], okx_entry_observed_ms=oe[1])
                    close_pair(r, bybit, okx, direction, QTY, ep, hp, entry, BYBIT_TAKER)
                    busy_taker = END if r['status'].startswith('unresolved') else entry + HOLD
            rows.append(r)
    scenario_results = {'taker_threshold_100ms': summary(rows)}
    print(f'Taker scan complete: {dict(scan_counts)}', flush=True)
    for fee in (2.0, 0.0):
        for ttl in (1000, 5000):
            for latency in (100, 500):
                for kind in ('threshold', 'scheduled_probe'):
                    name = f'maker_{kind}_fee{fee:g}_ttl{ttl}_hedge{latency}'
                    selected = []
                    busy = START
                    spacing = 1000 if kind == 'threshold' else 60000
                    for decision in range(START + 10000, END, spacing):
                        if decision < busy or not safe_interval(decision, ttl + latency + QUOTE_LATENCY + HOLD + 1000):
                            continue
                        b, o = bybit.at(decision), okx.at(decision)
                        if b is None or o is None:
                            continue
                        if kind == 'scheduled_probe':
                            direction = 1 if ((decision - START - 10000) // spacing) % 2 == 0 else -1
                        else:
                            choices = []
                            for direction in (1, -1):
                                bp = b[0][0 if direction == 1 else 1, 0, 0]
                                op = vwap(o[0], 0 if direction == 1 else 1)
                                if op is not None:
                                    edge = direction * (op - bp) / ((bp + op) / 2) * 10000
                                    choices.append((edge - fee - OKX_TAKER - BYBIT_TAKER - OKX_TAKER, direction))
                            if not choices or max(choices)[0] <= 0:
                                continue
                            direction = max(choices)[1]
                        r, busy = maker_attempt(name, kind, decision, direction, ttl, latency, fee, bybit, okx, trades)
                        selected.append(r)
                    rows.extend(selected)
                    scenario_results[name] = summary(selected)
                    print(name + ': ' + json.dumps(scenario_results[name]), flush=True)
    fields = list(execution_row('', '', START, 1))
    for filename, data, names in (('executions.csv', rows, fields),
                                  ('opportunities.csv', opportunities, list(maximum))):
        with (OUTPUT / filename).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=names)
            writer.writeheader(); writer.writerows(data)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)
    for direction, label in ((1, 'Buy Bybit / sell OKX'), (-1, 'Sell Bybit / buy OKX')):
        sample = [r for r in opportunities if r['direction'] == direction][::60]
        axes[0].plot([(r['decision_ms'] - START) / 3600000 for r in sample],
                     [r['net_entry_bps'] for r in sample], label=label, linewidth=.7)
    axes[0].axhline(0, color='black', linewidth=.8)
    axes[0].set(xlabel='UTC hour (one-minute display sample; scan uses every second)',
                ylabel='Cross spread less entry fees (bps)', title='0.01 BTC taker opportunity diagnostics')
    axes[0].legend()
    for name in scenario_results:
        if 'scheduled_probe' not in name:
            continue
        settled = sorted([r for r in rows if r['scenario'] == name and r['pnl_usdt'] != ''], key=lambda r: r['exit_ms'])
        axes[1].plot([(r['exit_ms'] - START) / 3600000 for r in settled],
                     np.cumsum([r['pnl_usdt'] for r in settled]), label=name.replace('maker_scheduled_probe_', ''))
    axes[1].set(xlabel='UTC hour', ylabel='Cumulative modeled USDT after fees',
                title='Scheduled execution probes: 60 s paired liquidation; not a profit strategy')
    axes[1].legend(fontsize=7, ncol=2)
    fig.suptitle('2026-09-01 public archive pilot — hypothetical fills and unverified historical contract size')
    fig.savefig(OUTPUT / 'performance.png', dpi=160)
    plt.close(fig)
    report = dict(interval=dict(start_utc=utc(START), end_utc_exclusive=utc(END)),
                  sources=sources, script_sha256=sha(Path(__file__)), self_tests=tests,
                  full_archives_streamed=True, bybit_book=dict(bybit.counts), okx_book=dict(okx.counts),
                  bybit_trades=trade_counts, okx_trades_audit=okx_audit,
                  grid=dict(step_ms=STEP, rows_per_venue=N, depth_per_side=10,
                            bybit_valid_rows=int(np.count_nonzero(bybit.observed >= 0)),
                            okx_valid_rows=int(np.count_nonzero(okx.observed >= 0)),
                            rule='Snapshot at grid t contains only raw book events with timestamp strictly < t; lookup floors to grid; preserve original observation timestamp; reject age >1000ms.'),
                  assumptions=dict(quantity_btc=QTY, okx_contract_btc=CONTRACT_BTC,
                      historical_contract_size_verified=False, okx_contracts_per_full_hedge=QTY / CONTRACT_BTC,
                      bybit_taker_bps=BYBIT_TAKER, okx_taker_bps=OKX_TAKER,
                      bybit_maker_bps=[2, 0], historical_account_fees_verified=False,
                      quote_latency_ms=QUOTE_LATENCY, hedge_latency_ms=[100, 500], ttl_ms=[1000, 5000],
                      paired_hold_after_hedge_ms=HOLD, max_order_btc=QTY,
                      max_matched_inventory_btc_per_venue_per_scenario=QTY,
                      funding='No funding cash flows: exclude entries whose worst-case lifetime crosses ANY UTC hour; this over-excludes common native settlement times. Historical funding schedule unverified.',
                      queue='At activation join behind max(decision displayed quantity, activation displayed quantity) at exact quoted price. Require activation price visible in top10; reject post-only crosses. Only opposite-side non-RPI trades at exact price strictly after activation and before expiry deplete queue. No cancellation credit or later queue additions. Partial fills retained; sub-contract residual exposure flattened on Bybit at TTL plus latency.',
                      exit='60 seconds after full-quantity OKX hedge, simultaneous hypothetical taker liquidation on both venues at depth VWAP. Partial or failed hedges flatten on Bybit. Missing/stale/depth failures explicitly unresolved; never fabricated.',
                      threshold='Decision spread must cover all four fees under assumed zero exit basis; this assumption is a trigger, not a forecast or profit guarantee. Taker entry is delayed 100ms on both venues.',
                      scheduled_probe='Every 60 seconds from 00:00:10 alternating directions; skip while busy. Independent of alpha or future outcomes. Each sensitivity is a separate counterfactual account, never aggregate their PnL.'),
                  limitations=['Single day, selected BTC venues, no out-of-sample or profitable-strategy claim.',
                      'Public timestamps are exchange publication/event clocks, not synchronized local receive clocks; no measured network latency.',
                      '100ms grid can omit intervening states; hedge and exit lookups may be up to 100ms coarser than event time. Staleness limit is 1000ms.',
                      'L2 queue and public prints cannot establish actual priority or private fills. Queue depletion, fill completion, and leg execution remain scenarios.',
                      'No market impact, hidden liquidity, exchange rejection, margin, liquidation, transfer, or account constraints modeled; no extra slippage beyond displayed-depth VWAP.',
                      'Archive byte hashes identify inputs, not independent proof of completeness. Existing collectors contain additional validation; this run fails on parse errors and regressions.',
                      'OKX trade files are fully audited but not used to infer hedge fills. Historical .01 BTC contract size must be verified before relying on sizing.',
                      'Any unresolved exposure is excluded from settled PnL and explicitly counted; settled totals alone would be incomplete if unresolved >0.'],
                  taker_scan=dict(scan_counts), max_net_taker_opportunity=maximum,
                  scenarios=scenario_results, executions_rows=len(rows), opportunities_rows=len(opportunities),
                  run_seconds=round(time.time() - started, 3),
                  artifacts={name: dict(sha256=sha(OUTPUT / name), bytes=(OUTPUT / name).stat().st_size)
                             for name in ('executions.csv', 'opportunities.csv', 'performance.png')})
    (OUTPUT / 'results.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(f'Completed in {report["run_seconds"]} seconds; results.json sha256={sha(OUTPUT / "results.json")}', flush=True)


if __name__ == '__main__':
    main()
