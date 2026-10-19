"""Financial accounting, causal timing, missing-data and extraction regression checks.

Run with: python -m unittest discover -s funding -p test_august_backtest.py -v
"""
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

sys.path.insert(0, str(Path(__file__).resolve().parent))
import august_backtest as bt
import august_prepare_data as prep


T0 = pd.Timestamp('2026-08-13T00:00:00Z')
EVENT_COLUMNS = ['exchange', 'base', 'settlement_time', 'funding_rate', 'mark_price']


def events(rows=()):
    return pd.DataFrame(rows, columns=EVENT_COLUMNS)


def snapshot(hour, base='BTC', low=0.0, high=0.004, prices=(100.0, 100.0),
             exchanges=('binance', 'hyperliquid'), intervals=(8, 1), spread=0.0):
    decision = T0 + pd.Timedelta(hours=hour)
    rows = []
    for exchange, rate, interval, mid in zip(exchanges, (low, high), intervals, prices):
        common = dict(decision_time=decision, exchange=exchange, base=base,
                      next_settlement_ms=np.nan, collected_time=decision)
        rows.append(dict(common, role='signal', observed_time=decision-pd.Timedelta(seconds=30),
                         funding_rate=rate, interval_hours=interval, mark_price=mid,
                         mid=np.nan, best_bid=np.nan, best_ask=np.nan))
        rows.append(dict(common, role='execution', observed_time=decision+pd.Timedelta(seconds=90),
                         funding_rate=np.nan, interval_hours=np.nan, mark_price=np.nan,
                         mid=mid, best_bid=mid-spread/2, best_ask=mid+spread/2))
    return rows


def observations(*hours, **kwargs):
    return pd.DataFrame([row for hour in hours for row in snapshot(hour, **kwargs)])


def replay(obs, settled, config=bt.Config()):
    """Supply explicit zero-rate outcomes wherever a test does not specify a payment."""
    rows = []
    times = pd.date_range(obs.decision_time.min(), obs.decision_time.max(), freq='h')
    for exchange, base in obs[['exchange', 'base']].drop_duplicates().itertuples(index=False, name=None):
        rows.extend((exchange, base, time, 0.0, 100.0) for time in times)
    complete = events(rows)
    if not settled.empty:
        complete = pd.concat([complete, settled], ignore_index=True)
    complete = complete.drop_duplicates(['exchange', 'base', 'settlement_time'], keep='last')
    return bt.run(obs, complete, config)


class FundingMathTests(unittest.TestCase):
    def test_normalization_once_and_selection_direction(self):
        obs = observations(0, low=0.0008, high=0.0002)
        ranked = bt.candidates(obs[obs.role == 'signal'], T0, bt.Config())
        self.assertEqual(ranked[0]['long_exchange'], 'binance')
        self.assertEqual(ranked[0]['short_exchange'], 'hyperliquid')
        self.assertAlmostEqual(ranked[0]['edge_8h_bps'], 8.0)
        self.assertAlmostEqual(bt.normalize_8h(0.0002, 1), 0.0016)
        for invalid in (0, -1, np.nan, np.inf):
            with self.subTest(interval=invalid), self.assertRaises(ValueError):
                bt.normalize_8h(0.001, invalid)

    def test_raw_hourly_and_eight_hour_settlements_are_economically_equal(self):
        rows = [('hyperliquid', 'BTC', T0+pd.Timedelta(hours=h), 0.0001, 200.0)
                for h in range(1, 9)]
        rows.append(('binance', 'BTC', T0+pd.Timedelta(hours=8), 0.0008, 200.0))
        ev = events(rows)
        for side in (1, -1):
            hourly, count = bt.settlement_cash(ev, 'BTC', 'hyperliquid', side, 3,
                                              T0, T0+pd.Timedelta(hours=8), pd.DataFrame())
            eight, _ = bt.settlement_cash(ev, 'BTC', 'binance', side, 3,
                                          T0, T0+pd.Timedelta(hours=8), pd.DataFrame())
            self.assertEqual(count, 8)
            self.assertAlmostEqual(hourly, -side*0.48)
            self.assertAlmostEqual(hourly, eight)

    def test_funding_signs_and_settlement_mark_not_entry_notional(self):
        for rate in (0.01, -0.01):
            for side in (1, -1):
                with self.subTest(rate=rate, side=side):
                    ev = events([('binance', 'BTC', T0+pd.Timedelta(hours=1), rate, 250)])
                    cash, count = bt.settlement_cash(ev, 'BTC', 'binance', side, 2,
                                                     T0, T0+pd.Timedelta(hours=1), pd.DataFrame())
                    self.assertAlmostEqual(cash, -side*5*np.sign(rate))
                    self.assertEqual(count, 1)

    def test_settlement_entry_exclusive_exit_inclusive(self):
        ev = events([('binance', 'BTC', T0+pd.Timedelta(hours=h), .01, 100)
                     for h in (0, 1, 2)])
        cash, count = bt.settlement_cash(ev, 'BTC', 'binance', 1, 1, T0,
                                         T0+pd.Timedelta(hours=1), pd.DataFrame())
        self.assertEqual((cash, count), (-1.0, 1))

    def test_mark_fallback_is_fresh_causal_and_exchange_specific(self):
        event_time = T0+pd.Timedelta(hours=1)
        ev = events([('binance', 'BTC', event_time, .01, np.nan)])
        values = pd.DataFrame([
            dict(base='BTC', exchange='binance', observed_time=event_time-pd.Timedelta(minutes=4), mark_price=120., mid=110.),
            dict(base='BTC', exchange='binance', observed_time=event_time+pd.Timedelta(seconds=1), mark_price=999., mid=999.),
            dict(base='BTC', exchange='bybit', observed_time=event_time, mark_price=888., mid=888.)])
        cash, _ = bt.settlement_cash(ev, 'BTC', 'binance', 1, 2, T0, event_time, values)
        self.assertAlmostEqual(cash, -2.4)
        values.loc[0, 'mark_price'] = np.nan
        cash, _ = bt.settlement_cash(ev, 'BTC', 'binance', 1, 2, T0, event_time, values)
        self.assertAlmostEqual(cash, -2.2)
        values.loc[0, 'observed_time'] = event_time-pd.Timedelta(minutes=6)
        with self.assertRaisesRegex(ValueError, 'Missing settlement valuation'):
            bt.settlement_cash(ev, 'BTC', 'binance', 1, 2, T0, event_time, values)

    def test_hyperliquid_uses_archived_oracle_for_funding_notional(self):
        ev=events([('hyperliquid','BTC',T0+pd.Timedelta(hours=1),.001,np.nan)])
        valuation=pd.DataFrame([dict(exchange='hyperliquid',base='BTC',
            observed_time=T0+pd.Timedelta(minutes=59),mark_price=110.0,mid=111.0,oracle_price=100.0)])
        cash,count=bt.settlement_cash(ev,'BTC','hyperliquid',1,2,T0,T0+pd.Timedelta(hours=1),valuation)
        self.assertAlmostEqual(cash,-.2)
        self.assertEqual(count,1)

    def test_equal_base_hedge_cancels_common_absolute_price_move(self):
        self.assertEqual(bt.price_components(7, 100, 120, 130, 150), 0)
        self.assertEqual(bt.price_components(7, 100, 120, 130, 140), 70)


class ReplayTests(unittest.TestCase):
    def test_equal_quantity_different_prices_and_complete_accounting(self):
        obs = observations(0, 8, prices=(100., 120.), spread=2.)
        ev = events([('hyperliquid', 'BTC', T0+pd.Timedelta(hours=1), .001, 150)])
        config = bt.Config(slippage_bps=3)
        trades, curve, _, summary = replay(obs, ev, config)
        self.assertEqual(len(trades), 1)
        trade = trades.iloc[0]
        quantity = 10000/120
        self.assertAlmostEqual(trade.quantity, quantity)
        long_entry, short_entry = 101*1.0003, 119*.9997
        long_exit, short_exit = 99*.9997, 121*1.0003
        price = quantity*((long_exit-long_entry)+(short_entry-short_exit))
        fees = quantity*((long_entry+long_exit)*5+(short_entry+short_exit)*4.5)/10000
        self.assertAlmostEqual(trade.funding_pnl, quantity*150*.001)
        self.assertAlmostEqual(trade.price_pnl, price)
        self.assertAlmostEqual(trade.fees, fees)
        self.assertAlmostEqual(trade.execution_cost, -price)
        self.assertAlmostEqual(trade.net_pnl, trade.funding_pnl+price-fees)
        self.assertAlmostEqual(summary['net_pnl'], trade.net_pnl)
        self.assertAlmostEqual(curve.iloc[-1].equity, config.capital+trade.net_pnl)
        self.assertEqual(curve.iloc[-1].open_pairs, 0)

    def test_adverse_slippage_monotonically_reduces_net(self):
        obs = observations(0, 8)
        results = [replay(obs, events(), bt.Config(slippage_bps=s))[3]['net_pnl']
                   for s in (0, 1, 3)]
        self.assertGreater(results[0], results[1])
        self.assertGreater(results[1], results[2])

    def test_future_mutations_do_not_change_prior_entries_or_equity(self):
        obs = observations(0, 8, 16)
        original = replay(obs, events())
        changed = obs.copy(deep=True)
        future = changed.decision_time > T0
        changed.loc[future & (changed.role == 'signal'), 'funding_rate'] *= -10
        changed.loc[future & (changed.role == 'execution'), ['mid', 'best_bid', 'best_ask']] *= 2
        ev = events([('hyperliquid', 'BTC', T0+pd.Timedelta(hours=12), -.9, 9999)])
        mutated = replay(changed, ev)
        assert_frame_equal(original[1].iloc[:1], mutated[1].iloc[:1])
        assert_frame_equal(original[2].iloc[:1], mutated[2].iloc[:1])
        for field in ('quantity', 'long_entry_price', 'short_entry_price', 'entry_fees',
                      'long_exchange', 'short_exchange', 'entry_decision'):
            self.assertEqual(original[0].iloc[0][field], mutated[0].iloc[0][field])

    def test_original_threshold_is_two_bps_plus_round_trip_fees(self):
        config = bt.Config(slippage_bps=0)
        for edge, count in ((20.9, 0), (21.0, 0), (21.1, 1)):
            with self.subTest(edge=edge):
                obs = observations(0, 8, high=edge/10000/8)
                trades, _, decisions, _ = replay(obs, events(), config)
                self.assertEqual(len(trades), count)
                self.assertEqual(decisions.iloc[0].threshold_8h_bps, 21.)

    def test_frozen_threshold_one_bps_and_minimum_eight_hour_hold(self):
        rows = snapshot(0, high=.0001/8)+snapshot(1, high=0)+snapshot(8, high=0)+snapshot(9, high=0)
        trades, _, decisions, _ = replay(pd.DataFrame(rows), events(), bt.Config(policy='august_frozen'))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades.iloc[0].exit_decision, (T0+pd.Timedelta(hours=8)).isoformat())
        self.assertEqual(trades.iloc[0].exit_reason, 'spread_narrowed')
        self.assertEqual(decisions.iloc[0].threshold_8h_bps, 1.)

    def test_frozen_exit_threshold_one_daily_bps_is_one_third_per_eight_hours(self):
        for edge, expected_hour in ((.32, 8), (.34, 16)):
            with self.subTest(edge=edge):
                rows = snapshot(0, high=.0001/8)
                rows += snapshot(8, high=edge/10000/8)
                rows += snapshot(16, high=edge/10000/8)
                trades, _, _, _ = replay(pd.DataFrame(rows), events(), bt.Config(policy='august_frozen'))
                self.assertEqual(trades.iloc[0].exit_decision,
                                 (T0+pd.Timedelta(hours=expected_hour)).isoformat())

    def test_both_policies_maximum_48_hour_hold(self):
        obs = observations(0, 8, 16, 24, 32, 40, 48, 56)
        for policy in ('original', 'august_frozen'):
            with self.subTest(policy=policy):
                trades, _, _, _ = replay(obs, events(), bt.Config(policy=policy))
                self.assertEqual(trades.iloc[0].exit_reason, 'max_holding')
                self.assertEqual(trades.iloc[0].exit_decision, (T0+pd.Timedelta(hours=48)).isoformat())

    def test_capacity_reserve_and_position_limit(self):
        obs = pd.DataFrame([r for hour in (0, 8) for base in ('AAA', 'BBB', 'CCC')
                            for r in snapshot(hour, base=base)])
        config = bt.Config(capital=15000, reserve=1000, max_positions=2)
        trades, curve, _, summary = replay(obs, events(), config)
        self.assertEqual(len(trades), 1)
        self.assertLessEqual(curve.gross_exposure.max(), 14000)
        self.assertGreater(summary['capacity_rejections'], 0)
        _, curve, _, summary = replay(obs, events(), replace(config, capital=100000))
        self.assertEqual(summary['max_open_pairs'], 2)
        self.assertLessEqual(curve.gross_exposure.max(), 40000)

    def test_stale_or_future_signals_cannot_open(self):
        obs = observations(0)
        signal = obs[obs.role == 'signal'].copy()
        for seconds in (-301, 1):
            signal['observed_time'] = T0+pd.Timedelta(seconds=seconds)
            self.assertEqual(bt.candidates(signal, T0, bt.Config()), [])

    def test_execution_delay_and_crossed_book_rejected(self):
        books = observations(0)
        books = books[(books.role == 'execution') & (books.exchange == 'binance')].copy()
        for seconds, valid in ((59, False), (60, True), (180, True), (181, False)):
            books['observed_time'] = T0+pd.Timedelta(seconds=seconds)
            self.assertEqual(bt.quote(books, 'binance', 'BTC', T0) is not None, valid)
        books['observed_time'] = T0+pd.Timedelta(seconds=90)
        books['best_bid'] = books.best_ask+1
        self.assertIsNone(bt.quote(books, 'binance', 'BTC', T0))

    def test_missing_entry_quotes_and_zero_trades(self):
        obs = observations(0, 8)
        obs = obs[~((obs.decision_time == T0) & (obs.role == 'execution'))]
        trades, curve, _, summary = replay(obs, events())
        self.assertTrue(trades.empty)
        self.assertEqual(summary['missing_quotes'], 1)
        self.assertEqual(summary['net_pnl'], 0)
        self.assertTrue((curve.equity == bt.Config().capital).all())
        trades, curve, _, summary = replay(observations(0, 8, high=0), events())
        self.assertTrue(trades.empty)
        self.assertEqual(summary['max_drawdown_pct'], 0)
        self.assertTrue((curve.open_pairs == 0).all())

    def test_missing_final_quotes_fails_instead_of_dropping_open_position(self):
        obs = observations(0, 8)
        obs = obs[~((obs.decision_time > T0) & (obs.role == 'execution'))]
        with self.assertRaisesRegex(ValueError, 'Cannot liquidate'):
            replay(obs, events())

    def test_validation_rejects_known_missing_settlement(self):
        obs = observations(0, 8)
        unrelated = events([('bybit', 'ETH', T0+pd.Timedelta(hours=1), .001, 100)])
        with self.assertRaisesRegex(ValueError, 'Actual settlements missing'):
            bt.validate_inputs(obs, unrelated)

    def test_regression_validation_detects_hourly_holes_between_observations(self):
        obs = observations(0, 8)
        partial = events([('hyperliquid', 'BTC', T0+pd.Timedelta(hours=1), .001, 100)])
        with self.assertRaisesRegex(ValueError, 'settlement|Settlement'):
            bt.validate_inputs(obs, partial)

    def test_stale_exit_signal_is_missing_not_current_spread(self):
        obs = pd.DataFrame(snapshot(0)+snapshot(8, high=0)+snapshot(16, high=0))
        obs.loc[(obs.decision_time == T0+pd.Timedelta(hours=8)) & (obs.role == 'signal'),
                'observed_time'] -= pd.Timedelta(hours=1)
        trades, _, _, _ = replay(obs, events())
        self.assertEqual(trades.iloc[0].exit_reason, 'signal_missing')

    def test_missing_settlement_outcome_must_not_be_zero_income(self):
        obs = observations(0, 8)
        with self.assertRaisesRegex(ValueError, 'settlement|Settlement'):
            bt.run(obs, events())

    def test_missing_existing_quote_has_explicit_valuation_error(self):
        obs = pd.DataFrame(snapshot(0, base='AAA')+snapshot(1, base='BBB')+
                           snapshot(8, base='AAA')+snapshot(8, base='BBB'))
        with self.assertRaisesRegex(ValueError, 'Missing hourly valuation'):
            replay(obs, events(), bt.Config(policy='august_frozen'))

    def test_nonfinite_settlement_mark_rejected(self):
        ev = events([('binance', 'BTC', T0+pd.Timedelta(hours=1), .001, np.inf)])
        with self.assertRaises(ValueError):
            bt.settlement_cash(ev, 'BTC', 'binance', 1, 2, T0,
                               T0+pd.Timedelta(hours=1), pd.DataFrame())


class ExtractionTests(unittest.TestCase):
    def extract_rows(self, funding, books=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kind, rows in (('funding', funding), ('l2', books)):
                folder = root/'archive'/kind
                folder.mkdir(parents=True)
                (folder/'tiny.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                prep.extract(root/'archive', root/'out')
            return pd.read_csv(root/'out'/'august_observations.csv')

    def funding_row(self, when, rate=.0002, interval=1, exchange='hyperliquid', nxt=None):
        return dict(collected_at_ms=int(when.timestamp()*1000), funding_ts_ms=None,
                    base='BTC', exchange=exchange, source='live', funding_rate=rate,
                    funding_interval_hours=interval, next_funding_ts_ms=nxt, mark_price=100)

    def test_extraction_preserves_raw_hl_rate_and_latest_causal_signal(self):
        rows = [self.funding_row(T0-pd.Timedelta(minutes=m), rate=r)
                for m, r in ((6, .9), (4, .0001), (1, .0002))]
        rows.append(self.funding_row(T0+pd.Timedelta(seconds=1), rate=.8))
        out = self.extract_rows(rows)
        selected = out[out.decision_ms == int(T0.timestamp()*1000)]
        self.assertEqual(len(selected), 1)
        self.assertAlmostEqual(selected.iloc[0].funding_rate, .0002)
        self.assertEqual(selected.iloc[0].interval_hours, 1)
        mutated = self.extract_rows(rows[:-1]+[dict(rows[-1], funding_rate=-999)])
        assert_frame_equal(selected.reset_index(drop=True),
                           mutated[mutated.decision_ms == int(T0.timestamp()*1000)].reset_index(drop=True))

    def test_interval_inference_never_backfills_initial_unknown(self):
        first_next = int((T0+pd.Timedelta(hours=1)).timestamp()*1000)
        rows = [self.funding_row(T0-pd.Timedelta(minutes=1), interval=None,
                                 exchange='binance', nxt=first_next),
                self.funding_row(T0+pd.Timedelta(hours=1)-pd.Timedelta(minutes=1),
                                 interval=None, exchange='binance', nxt=first_next+8*prep.HOUR)]
        out = self.extract_rows(rows)
        self.assertTrue(pd.isna(out.iloc[0].interval_hours))
        self.assertEqual(out.iloc[1].interval_hours, 8)
        rows[-1]['next_funding_ts_ms'] = first_next+24*prep.HOUR
        self.assertTrue(self.extract_rows(rows).interval_hours.isna().all())

    def test_extraction_chooses_latest_valuation_first_execution(self):
        books = []
        for seconds, mid in ((-121, 50), (-100, 90), (-10, 100), (59, 110), (60, 120), (90, 130), (181, 140)):
            books.append(dict(collected_at_ms=int((T0+pd.Timedelta(seconds=seconds)).timestamp()*1000),
                              base='BTC', exchange='binance', source='l2', best_bid=mid-1,
                              best_ask=mid+1, bids=[[mid-1, 2]], asks=[[mid+1, 2]]))
        out = self.extract_rows([self.funding_row(T0-pd.Timedelta(minutes=1))], books)
        self.assertEqual(out[out.role == 'valuation'].iloc[0].mid, 100)
        self.assertEqual(out[out.role == 'execution'].iloc[0].mid, 120)


if __name__ == '__main__':
    unittest.main()
