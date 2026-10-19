"""Independent synthetic execution tests; never open the downloaded raw cache.

Run with aqa2 Python: python -m unittest discover -s <this directory> -p test_replay.py -v
"""
import csv
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np

SPEC = importlib.util.spec_from_file_location('execution_replay_under_test', Path(__file__).with_name('replay.py'))
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


def book(bid=100, ask=101, quantity=1):
    return np.array([[[bid, quantity], [bid - 1, quantity]],
                     [[ask, quantity], [ask + 1, quantity]]], dtype=float)


def trades(events):
    """Events are (milliseconds, price, BTC quantity, aggressor direction)."""
    if not events:
        return (np.array([], dtype=np.int64), np.array([]), np.array([]), np.array([], dtype=np.int8))
    ts, price, quantity, side = zip(*events)
    return (np.rint(np.array(ts) * 1000).astype(np.int64), np.array(price, dtype=float),
            np.array(quantity, dtype=float), np.array(side, dtype=np.int8))


class Quotes:
    """Synthetic visible quotes; callbacks allow independent arrival/exit states."""
    def __init__(self, callback):
        self.callback = callback
        self.requests = []

    def at(self, ts):
        self.requests.append(ts)
        value = self.callback(ts)
        return None if value is None else (value, ts - 1)


class GridTests(unittest.TestCase):
    def setUp(self):
        self.small = patch.object(r, 'N', 50)
        self.small.start()
        self.addCleanup(self.small.stop)

    def test_equal_time_and_future_updates_are_not_visible(self):
        g = r.Grid()
        g.update(r.START + 20, [[100, 1]], [[101, 1]], True)
        g.update(r.START + 100, [[100, 2]], [], False)
        g.update(r.START + 150, [[100, 9]], [], False)
        g.emit_before(r.START + 300)
        self.assertEqual(g.at(r.START + 100)[0][0, 0, 1], 1)
        self.assertEqual(g.at(r.START + 149)[0][0, 0, 1], 1)
        self.assertEqual(g.at(r.START + 200)[0][0, 0, 1], 9)
        self.assertLess(g.at(r.START + 200)[1], r.START + 200)
        self.assertIsNone(g.at(r.START - 1))

    def test_staleness_uses_original_event_time(self):
        g = r.Grid()
        g.update(r.START + 20, [[100, 1]], [[101, 1]], True)
        g.emit_before(r.START + 3000)
        self.assertIsNotNone(g.at(r.START + 1020))
        self.assertIsNone(g.at(r.START + 1021))

    def test_snapshot_replaces_and_zero_deletes_levels(self):
        g = r.Grid()
        g.update(r.START + 10, [[100, 1], [99, 2]], [[101, 1]], True)
        g.update(r.START + 110, [[100, 0]], [], False)
        g.update(r.START + 210, [[98, 3]], [[102, 4]], True)
        g.emit_before(r.START + 400)
        self.assertEqual(g.at(r.START + 200)[0][0, 0, 0], 99)
        self.assertEqual(g.at(r.START + 300)[0][0, 0, 0], 98)
        self.assertEqual(g.at(r.START + 300)[0][0, 1, 1], 0)

    def test_regression_and_delta_without_snapshot_fail(self):
        g = r.Grid()
        with self.assertRaises(ValueError):
            g.update(r.START + 10, [[100, 1]], [[101, 1]], False)
        g.update(r.START + 20, [[100, 1]], [[101, 1]], True)
        with self.assertRaises(ValueError):
            g.update(r.START + 19, [], [], False)


class QueueTests(unittest.TestCase):
    def test_only_opposing_exact_price_strictly_inside_lifetime_counts(self):
        events = [(99, 100, 10, -1), (100, 100, 10, -1),
                  (101, 100, 10, 1), (102, 99, 10, -1),
                  (103, 100, .02, -1), (104, 100, .006, -1),
                  (105, 100, .004, -1), (106, 100, 10, -1)]
        filled, first, full = r.queue_fill(*trades(events), 100, 106, 100, 1, .02)
        self.assertAlmostEqual(filled, .01)
        self.assertEqual((first, full), (104, 105))
        self.assertAlmostEqual(r.queue_fill(*trades(events), 100, 105, 100, 1, .02)[0], .006)

    def test_sell_queue_uses_buy_aggressors_and_retains_partial(self):
        events = [(101, 101, 1, -1), (102, 101, .022, 1), (103, 101, .002, 1)]
        filled, first, full = r.queue_fill(*trades(events), 100, 104, 101, -1, .02)
        self.assertAlmostEqual(filled, .004)
        self.assertEqual(first, 102)
        self.assertIsNone(full)

    def test_excess_volume_cannot_overfill(self):
        filled, first, full = r.queue_fill(*trades([(101, 100, 20, -1)]), 100, 102, 100, 1, 0)
        self.assertAlmostEqual(filled, r.QTY)
        self.assertEqual(first, full)


class ExecutionTests(unittest.TestCase):
    decision = r.START + 10000

    def attempt(self, direction=1, bybit=None, okx=None, events=None, ttl=1000, latency=100):
        bybit = bybit or Quotes(lambda ts: book(quantity=.02))
        okx = okx or Quotes(lambda ts: book(103, 104))
        if events is None:
            events = [(self.decision + 200, 100 if direction == 1 else 101, .03, -direction)]
        row, busy = r.maker_attempt('test', 'scheduled_probe', self.decision, direction,
                                    ttl, latency, 2, bybit, okx, trades(events))
        return row, busy, bybit, okx

    def test_post_only_rejects_cross_on_arrival_both_directions(self):
        for direction, arrival in [(1, book(98, 99)), (-1, book(102, 103))]:
            with self.subTest(direction=direction):
                b = Quotes(lambda ts: book() if ts == self.decision else arrival)
                row, busy, _, o = self.attempt(direction, bybit=b)
                self.assertEqual(row['status'], 'post_only_reject')
                self.assertEqual(row['filled_btc'], 0)
                self.assertEqual(o.requests, [])
                self.assertEqual(busy, self.decision + r.QUOTE_LATENCY)

    def test_cancellations_do_not_reduce_queue_ahead(self):
        b = Quotes(lambda ts: book(quantity=.02 if ts == self.decision else .001))
        row, _, _, _ = self.attempt(bybit=b, events=[(self.decision + 200, 100, .015, -1)])
        self.assertEqual(row['status'], 'expired_unfilled')
        self.assertEqual(row['queue_ahead_btc'], .02)
        self.assertEqual(row['filled_btc'], 0)

    def test_larger_activation_queue_is_respected(self):
        b = Quotes(lambda ts: book(quantity=.001 if ts == self.decision else .02))
        row, _, _, _ = self.attempt(bybit=b, events=[(self.decision + 200, 100, .015, -1)])
        self.assertEqual(row['status'], 'expired_unfilled')
        self.assertEqual(row['queue_ahead_btc'], .02)

    def test_absent_activation_price_is_not_assumed_executable(self):
        b = Quotes(lambda ts: book() if ts == self.decision else book(98, 101))
        row, _, _, _ = self.attempt(bybit=b)
        self.assertEqual(row['status'], 'activation_price_outside_top10')

    def test_hedge_waits_for_latency_and_uses_arrival_depth(self):
        hedge = self.decision + 700
        exit_ts = hedge + r.HOLD
        o = Quotes(lambda ts: book(103, 104) if ts < hedge else book(90, 91))
        b = Quotes(lambda ts: book(100, 101, .02) if ts < exit_ts else book(99, 100))
        row, busy, _, _ = self.attempt(bybit=b, okx=o, latency=500)
        self.assertEqual(row['status'], 'closed_pair')
        self.assertEqual(row['hedge_ms'], hedge)
        self.assertEqual(o.requests, [hedge, exit_ts])
        self.assertEqual(row['okx_entry'], 90)
        self.assertEqual(row['bybit_exit'], 99)
        self.assertEqual(row['okx_exit'], 91)
        self.assertEqual(busy, exit_ts)
        self.assertAlmostEqual(row['gross_usdt'], -.02)
        fees = .01 * (100 * 2 + 90 * 5 + 99 * 5.5 + 91 * 5) / 10000
        self.assertAlmostEqual(row['fees_usdt'], fees)
        self.assertAlmostEqual(row['pnl_usdt'], -.02 - fees)
        for name, execution_time in [('bybit_entry', self.decision + r.QUOTE_LATENCY),
                                     ('okx_entry', hedge), ('bybit_exit', exit_ts),
                                     ('okx_exit', exit_ts)]:
            self.assertLess(row[name + '_observed_ms'], execution_time)

    def test_short_pair_liquidates_both_legs_and_pays_four_fees(self):
        row, _, _, _ = self.attempt(direction=-1)
        self.assertEqual(row['status'], 'closed_pair')
        self.assertEqual((row['bybit_entry'], row['okx_entry'], row['bybit_exit'], row['okx_exit']),
                         (101, 104, 101, 103))
        self.assertAlmostEqual(row['gross_usdt'], -.01)
        self.assertAlmostEqual(row['fees_usdt'], .01 * (101 * 2 + 104 * 5 + 101 * 5.5 + 103 * 5) / 10000)

    def test_partial_fill_unwinds_actual_quantity_after_expiry_plus_latency(self):
        row, busy, _, o = self.attempt(events=[(self.decision + 200, 100, .024, -1)])
        self.assertEqual(row['status'], 'partial_bybit_unwind')
        self.assertAlmostEqual(row['filled_btc'], .004)
        self.assertEqual(row['exit_ms'], self.decision + 1200)
        self.assertEqual(busy, self.decision + 1200)
        self.assertEqual(o.requests, [])
        self.assertAlmostEqual(row['fees_usdt'], .004 * (100 * 2 + 100 * 5.5) / 10000)
        self.assertLess(row['pnl_usdt'], 0)

    def test_failed_hedge_is_costly_bybit_unwind(self):
        for unavailable in (None, book(103, 104, .001)):
            with self.subTest(unavailable=unavailable is None):
                row, _, _, _ = self.attempt(okx=Quotes(lambda ts: unavailable))
                self.assertEqual(row['status'], 'failed_hedge_bybit_unwind')
                self.assertAlmostEqual(row['filled_btc'], .01)
                self.assertAlmostEqual(row['fees_usdt'], .01 * (100 * 2 + 100 * 5.5) / 10000)
                self.assertLess(row['pnl_usdt'], 0)

    def test_failed_unwind_preserves_unresolved_exposure(self):
        b = Quotes(lambda ts: book(quantity=.02) if ts <= self.decision + 100 else None)
        row, _, _, _ = self.attempt(bybit=b, okx=Quotes(lambda ts: None))
        self.assertEqual(row['status'], 'unresolved_failed_hedge_unwind')
        self.assertAlmostEqual(row['filled_btc'], .01)
        self.assertEqual(row['pnl_usdt'], '')
        totals = r.summary([row])
        self.assertEqual(totals['unresolved'], 1)
        self.assertEqual(totals['settled_experiments'], 0)

    def test_unresolved_inventory_blocks_further_scenario_orders(self):
        # The scheduler retains only the returned busy time, with no inventory state.
        b = Quotes(lambda ts: book(quantity=.02) if ts <= self.decision + 100 else None)
        row, busy, _, _ = self.attempt(bybit=b, okx=Quotes(lambda ts: None))
        self.assertEqual(row['status'], 'unresolved_failed_hedge_unwind')
        self.assertGreaterEqual(busy, r.END,
                                'Unresolved inventory must block new orders until resolved or the scenario ends')

    def test_partial_unwind_missing_depth_is_unresolved(self):
        b = Quotes(lambda ts: book(quantity=.02) if ts <= self.decision + 100 else book(quantity=.001))
        row, _, _, _ = self.attempt(bybit=b, events=[(self.decision + 200, 100, .024, -1)])
        self.assertEqual(row['status'], 'unresolved_partial_unwind')
        self.assertEqual(row['pnl_usdt'], '')
        self.assertAlmostEqual(row['filled_btc'], .004)

    def test_exit_missing_or_insufficient_depth_is_unresolved(self):
        for value, status in [(None, 'unresolved_exit_missing_or_stale'),
                              (book(quantity=.001), 'unresolved_exit_depth')]:
            with self.subTest(status=status):
                b = Quotes(lambda ts: book(quantity=.02) if ts < self.decision + 60000 else value)
                row, _, _, _ = self.attempt(bybit=b)
                self.assertEqual(row['status'], status)
                self.assertEqual(row['pnl_usdt'], '')
                self.assertEqual(r.summary([row])['unresolved'], 1)

    def test_funding_and_end_of_day_guards_include_exact_boundary(self):
        self.assertTrue(r.safe_interval(r.START + 10000, 66000))
        self.assertFalse(r.safe_interval(r.START + 3600000 - 66000, 66000))
        self.assertFalse(r.safe_interval(r.END - 66000, 66000))

    def test_depth_vwap_consumes_multiple_levels_and_refuses_shortfall(self):
        levels = np.array([[[100, .004], [99, .006]], [[101, .004], [102, .006]]])
        self.assertAlmostEqual(r.vwap(levels, 0), 99.4)
        self.assertAlmostEqual(r.vwap(levels, 1), 101.6)
        self.assertIsNone(r.vwap(levels, 1, .011))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def bybit_csv(self, rows):
        path = self.root / 'synthetic.csv.gz'
        with gzip.open(path, 'wt', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['timestamp', 'symbol', 'side', 'price', 'size', 'RPI'])
            writer.writeheader()
            for overrides in rows:
                row = dict(timestamp=str(r.START / 1000 + .123456), symbol='BTCUSDT',
                           side='Sell', price='100', size='.01', RPI='0')
                row.update(overrides)
                writer.writerow(row)
        return path

    def test_trade_precision_rpi_and_utc_window(self):
        path = self.bybit_csv([dict(timestamp=str(r.START // 1000 - 1)), {}, dict(RPI='1'),
                               dict(timestamp=str(r.END // 1000))])
        values, counts = r.read_bybit_trades(path)
        self.assertEqual(list(values[0]), [r.START * 1000 + 123456])
        self.assertEqual(list(values[3]), [-1])
        self.assertEqual(counts['all_rows'], 4)
        self.assertEqual(counts['excluded_rpi'], 1)
        self.assertEqual(counts['eligible_rows'], 1)

    def test_trade_reader_rejects_regressing_timestamps(self):
        path = self.bybit_csv([{}, dict(timestamp=str(r.START // 1000))])
        with self.assertRaises(ValueError):
            r.read_bybit_trades(path)

    def test_nonfinite_bybit_trade_values_fail_closed(self):
        for field in ('price', 'size'):
            for value in ('nan', 'inf'):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        r.read_bybit_trades(self.bybit_csv([{field: value}]))

    def test_okx_native_contract_conversion_and_snapshot_replacement(self):
        path = self.root / 'synthetic.tar.gz'
        records = [dict(action='snapshot', instId='BTC-USDT-SWAP', ts=r.START + 10,
                        bids=[['100', '2', '0', '1']], asks=[['101', '3', '0', '1']]),
                   dict(action='snapshot', instId='BTC-USDT-SWAP', ts=r.START + 110,
                        bids=[['99', '4', '0', '1']], asks=[['102', '5', '0', '1']])]
        payload = ''.join(json.dumps(item) + '\n' for item in records).encode()
        with tarfile.open(path, 'w:gz') as archive:
            member = tarfile.TarInfo('BTC-USDT-SWAP-L2orderbook-400lv-2026-09-01.data')
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        with patch.object(r, 'N', 10):
            grid = r.read_okx_book(path)
        self.assertAlmostEqual(grid.at(r.START + 100)[0][0, 0, 1], .02)
        self.assertAlmostEqual(grid.at(r.START + 200)[0][0, 0, 1], .04)
        self.assertEqual(grid.at(r.START + 200)[0][0, 0, 0], 99)

    def test_okx_trade_audit_combines_archives_and_filters_utc_day(self):
        paths = []
        for index, times in enumerate(([r.START - 1, r.START], [r.END - 1, r.END])):
            path = self.root / ('synthetic%d.zip' % index)
            text = 'instrument_name,trade_id,side,price,size,created_time,source\n'
            text += ''.join('BTC-USDT-SWAP,%d,buy,100,1,%d,0\n' % (ts, ts) for ts in times)
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('trades.csv', text)
            paths.append(path)
        counts = r.audit_okx_trades(paths)
        self.assertEqual(counts['in_day_rows'], 2)
        self.assertEqual(counts['first_ms'], r.START)
        self.assertEqual(counts['last_ms'], r.END - 1)

    def test_sha_identifies_exact_cached_bytes(self):
        path = self.root / 'synthetic.bin'
        path.write_bytes(b'raw archive fixture\x00\xff')
        self.assertEqual(r.sha(path), hashlib.sha256(path.read_bytes()).hexdigest())
        previous = r.sha(path)
        path.write_bytes(b'changed')
        self.assertNotEqual(r.sha(path), previous)


if __name__ == '__main__':
    unittest.main(verbosity=2)
