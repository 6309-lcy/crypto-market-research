"""Offline synthetic regression tests for the fixed September funding policy.

Run with aqa2: python -B -m unittest discover -s funding/low_turnover -v
"""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

_SPEC = importlib.util.spec_from_file_location(
    "lowturnover_test_engine", Path(__file__).with_name("backtest.py"))
bt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bt
_SPEC.loader.exec_module(bt)
START = pd.Timestamp("2026-09-15T00:00:00Z")


def fixture(hours=100, bases=("BTC",), venues=("binance", "bybit"),
            rates=(0.0, 0.002), offsets_ms=None):
    funding, prices = [], []
    offsets_ms = offsets_ms or {}
    for base in bases:
        for exchange, rate in zip(venues, rates):
            interval = 1 if exchange == "hyperliquid" else 8
            for hour in range(-96, hours + 9, interval):
                funding.append(dict(base=base, exchange=exchange,
                    settlement_time=START + pd.Timedelta(hours=hour)
                    + pd.Timedelta(milliseconds=offsets_ms.get(exchange, 0)),
                    funding_rate=rate, settlement_mark_price=np.nan,
                    normalized_8h=999.0))
            for time in pd.date_range(START, periods=hours + 1, freq="h"):
                prices.append(dict(base=base, exchange=exchange, time=time,
                                   trade_open=100.0, mark_open=100.0))
    return pd.DataFrame(funding), pd.DataFrame(prices)


def run(funding, prices, config=None):
    return bt.replay(funding, prices, config or bt.Config(),
                     start=START, end=prices.time.max())


def forward(rows, base="BTC", long="binance", short="bybit"):
    return next(r for r in rows if (r["base"], r["long_exchange"], r["short_exchange"])
                == (base, long, short))


class ForecastTests(unittest.TestCase):
    def test_frozen_protocol_defaults(self):
        cfg = bt.Config()
        self.assertEqual(bt.START, START)
        self.assertEqual(bt.END, pd.Timestamp("2026-09-30T23:00:00Z"))
        self.assertEqual((cfg.history_hours, cfg.horizon_hours), (72, 72))
        self.assertEqual((cfg.min_hold_hours, cfg.max_hold_hours, cfg.cooldown_hours),
                         (24, 168, 24))
        self.assertEqual((cfg.entry_buffer_bps, cfg.exit_buffer_bps), (5, 2))
        self.assertEqual((cfg.capital, cfg.leg_notional, cfg.max_pairs, cfg.reserve),
                         (100000, 10000, 5, 1000))
        self.assertIs(bt.DATA, bt.engine.DATA)

    def test_complete_nine_buckets_and_five_minute_publication(self):
        funding, _ = fixture()
        before = forward(bt.forecast_pairs(funding, START + pd.Timedelta(minutes=4)))
        after = forward(bt.forecast_pairs(funding, START + pd.Timedelta(minutes=5)))
        self.assertEqual(before['history_end'], START - pd.Timedelta(hours=8))
        self.assertEqual(after['history_end'], START)
        self.assertEqual(after['history_start'], START - pd.Timedelta(hours=72))
        self.assertEqual(after['bucket_available_time'], START + pd.Timedelta(minutes=5))
        self.assertEqual((after['bucket_count'], after['long_event_count'],
                          after['short_event_count']), (9, 9, 9))

    def test_millisecond_offsets_keep_native_events_in_nominal_bucket(self):
        exact, _ = fixture(venues=('hyperliquid', 'bybit'), rates=(0.0001, 0.0016))
        jittered = exact.copy()
        jittered['settlement_time'] += pd.Timedelta(milliseconds=1)
        decision = START + pd.Timedelta(minutes=5)
        a = forward(bt.forecast_pairs(exact, decision), long='hyperliquid')
        b = forward(bt.forecast_pairs(jittered, decision), long='hyperliquid')
        for key in ('q25_8h_bps', 'expected_72h_bps', 'long_event_count',
                    'short_event_count', 'positive_buckets'):
            self.assertEqual(a[key], b[key])
        self.assertEqual(b['long_signal_time'], START - pd.Timedelta(hours=1)
                         + pd.Timedelta(milliseconds=1))
        self.assertEqual(b['short_signal_time'], START - pd.Timedelta(hours=8)
                         + pd.Timedelta(milliseconds=1))

    def test_hourly_native_rates_sum_once_against_eight_hour_cex(self):
        funding, _ = fixture(venues=('hyperliquid', 'bybit'), rates=(0.0001, 0.0016))
        row = forward(bt.forecast_pairs(funding, START), long='hyperliquid')
        self.assertEqual((row['long_event_count'], row['short_event_count']), (72, 9))
        self.assertAlmostEqual(row['long_mean_8h_bps'], 8)
        self.assertAlmostEqual(row['short_mean_8h_bps'], 16)
        self.assertAlmostEqual(row['q25_8h_bps'], 8)
        self.assertAlmostEqual(row['expected_72h_bps'], 72)
        self.assertAlmostEqual(row['expected_24h_bps'], 24)

    def test_missing_or_duplicate_native_event_rejects_incomplete_history(self):
        funding, _ = fixture(venues=('hyperliquid', 'bybit'), rates=(0.0001, 0.0016))
        event = (funding.exchange == 'hyperliquid') & (
            funding.settlement_time == START - pd.Timedelta(hours=20))
        self.assertEqual(bt.forecast_pairs(funding.loc[~event], START), [])
        duplicate = pd.concat([funding, funding.loc[event]], ignore_index=True)
        self.assertEqual(bt.forecast_pairs(duplicate, START), [])

    def test_quartile_is_of_paired_spreads_and_both_directions_are_retained(self):
        funding, _ = fixture()
        starts = pd.date_range(START - pd.Timedelta(hours=80), periods=9, freq='8h')
        long = np.array([0, 10, 20, 30, 40, 50, 60, 70, 80], dtype=float)
        spread = np.array([9, 1, 8, 2, 7, 3, 6, 4, 5], dtype=float)
        for time, a, d in zip(starts, long, spread):
            funding.loc[(funding.exchange == 'binance') &
                        (funding.settlement_time == time), 'funding_rate'] = a / 1e4
            funding.loc[(funding.exchange == 'bybit') &
                        (funding.settlement_time == time), 'funding_rate'] = (a + d) / 1e4
        rows = bt.forecast_pairs(funding, START)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(forward(rows)['q25_8h_bps'], 3)
        self.assertAlmostEqual(forward(rows, long='bybit', short='binance')['q25_8h_bps'], -7)

    def test_requires_eight_of_nine_strictly_positive_buckets(self):
        funding, _ = fixture()
        for nonpositive, expected in ((1, True), (2, False)):
            changed = funding.copy()
            times = pd.date_range(START - pd.Timedelta(hours=80),
                                  periods=nonpositive, freq='8h')
            changed.loc[(changed.exchange == 'bybit') &
                        changed.settlement_time.isin(times), 'funding_rate'] = 0
            row = forward(bt.forecast_pairs(changed, START))
            self.assertEqual(row['positive_buckets'], 9 - nonpositive)
            self.assertEqual(row['threshold_passed'], expected)

    def test_entry_forecast_covers_all_four_fills_plus_buffer(self):
        funding, _ = fixture(rates=(0, 0.0004))
        for cost, expected in ((1, True), (3, False), (5, False)):
            cfg = bt.Config(fill_cost_bps=cost)
            row = forward(bt.forecast_pairs(funding, START, cfg))
            self.assertAlmostEqual(row['expected_72h_bps'], 36)
            self.assertAlmostEqual(row['entry_threshold_bps'], 21 + 4 * cost + 5)
            self.assertEqual(row['threshold_passed'], expected)

    def test_shared_forecast_cache_reprices_each_cost_scenario(self):
        funding, _ = fixture(rates=(0, 0.0004))
        cache = {}
        a = forward(bt.cached_forecasts(funding, START, bt.Config(fill_cost_bps=1), cache))
        b = forward(bt.cached_forecasts(funding, START, bt.Config(fill_cost_bps=5), cache))
        self.assertTrue(a['threshold_passed'])
        self.assertFalse(b['threshold_passed'])
        self.assertEqual(a['expected_72h_bps'], b['expected_72h_bps'])
        self.assertEqual(a['entry_threshold_bps'], 30)

    def test_future_mutations_leave_past_forecasts_equity_and_fills_unchanged(self):
        funding, prices = fixture(hours=80)
        changed = funding.copy()
        cutoff = START + pd.Timedelta(hours=12)
        future = changed.settlement_time > cutoff
        changed.loc[future, 'funding_rate'] = -0.4
        changed.loc[future, 'settlement_time'] += pd.Timedelta(milliseconds=100)
        changed_prices = prices.copy()
        changed_prices.loc[changed_prices.time > cutoff, ['trade_open', 'mark_open']] *= 2
        a, b = run(funding, prices), run(changed, changed_prices)
        for index, column in ((1, 'time'), (2, 'decision_time')):
            pd.testing.assert_frame_equal(a[index].loc[a[index][column] <= cutoff].reset_index(drop=True),
                                          b[index].loc[b[index][column] <= cutoff].reset_index(drop=True))
        for key in ('entry_time', 'quantity', 'long_entry_price', 'short_entry_price'):
            self.assertEqual(a[0].iloc[0][key], b[0].iloc[0][key])


class AccountingTests(unittest.TestCase):
    def test_next_hour_fill_flat_start_and_equal_base_quantity(self):
        funding, prices = fixture(hours=73)
        prices.loc[prices.exchange == 'bybit', 'trade_open'] = 125
        entry = START + pd.Timedelta(hours=1)
        prices.loc[(prices.exchange == 'binance') & (prices.time == entry), 'trade_open'] = 110
        ledger, curve, _, payments, _ = run(funding, prices)
        trade = ledger.iloc[0]
        self.assertEqual(curve.iloc[0].open_pairs, 0)
        self.assertEqual(curve.iloc[0].equity, 100000)
        self.assertEqual(trade.entry_time, trade.decision_time + pd.Timedelta(hours=1))
        self.assertEqual((trade.long_entry_reference, trade.short_entry_reference), (110, 125))
        self.assertAlmostEqual(trade.quantity, 80)
        self.assertTrue((payments.settlement_time > entry).all())

    def test_four_adverse_penalties_four_fees_and_net_reconciliation(self):
        funding, prices = fixture(hours=73, rates=(0.001, 0.003))
        ledger, curve, _, payments, summary = run(funding, prices)
        trade = ledger.iloc[0]
        self.assertEqual(len(ledger), 1)
        for key, value in (('long_entry_price', 100.03), ('short_entry_price', 99.97),
                           ('long_exit_price', 99.97), ('short_exit_price', 100.03)):
            self.assertAlmostEqual(trade[key], value)
        fees = 100 * (100.03 * 5 + 99.97 * 5.5 + 99.97 * 5 + 100.03 * 5.5) / 1e4
        self.assertAlmostEqual(trade.entry_fees, 100 * (100.03 * 5 + 99.97 * 5.5) / 1e4)
        self.assertAlmostEqual(trade.fees, fees)
        self.assertAlmostEqual(trade.execution_cost, 12)
        self.assertAlmostEqual(trade.price_pnl, -12)
        self.assertTrue((payments.loc[payments.side == 1, 'cash'] < 0).all())
        self.assertTrue((payments.loc[payments.side == -1, 'cash'] > 0).all())
        self.assertAlmostEqual(trade.funding_pnl, payments.cash.sum())
        self.assertAlmostEqual(trade.net_pnl, payments.cash.sum() - 12 - fees)
        self.assertAlmostEqual(curve.iloc[-1].equity, 100000 + trade.net_pnl)
        self.assertEqual(curve.iloc[-1].open_pairs, 0)
        for key in ('funding_pnl', 'reference_price_pnl', 'execution_cost', 'price_pnl', 'fees', 'net_pnl'):
            self.assertAlmostEqual(summary[key], ledger[key].sum())

    def test_negative_rates_reverse_funding_cash_signs(self):
        funding, prices = fixture(hours=73, rates=(-0.003, -0.001))
        payments = run(funding, prices)[3]
        self.assertTrue((payments.loc[payments.side == 1, 'cash'] > 0).all())
        self.assertTrue((payments.loc[payments.side == -1, 'cash'] < 0).all())

    def test_event_ownership_uses_actual_milliseconds_and_hyperliquid_proxy(self):
        funding, prices = fixture(hours=73, venues=('binance', 'hyperliquid'),
                                  rates=(0.0001, 0.0003), offsets_ms={'hyperliquid': 1})
        entry, end = START + pd.Timedelta(hours=1), prices.time.max()
        extras = pd.DataFrame([dict(base='BTC', exchange='hyperliquid', settlement_time=t,
            funding_rate=.0003, settlement_mark_price=np.nan, normalized_8h=999)
            for t in (entry, end)])
        funding = pd.concat([funding, extras], ignore_index=True)
        prices.loc[prices.exchange == 'hyperliquid', 'mark_open'] = 900
        # Isolate cash ownership from deliberately duplicated nominal-hour events.
        forecasts = bt.forecast_pairs(funding[~funding.settlement_time.isin([entry, end])], START)
        with patch.object(bt, 'cached_forecasts', return_value=forecasts):
            ledger, _, _, payments, summary = run(funding, prices)
        hl = payments[payments.exchange == 'hyperliquid']
        self.assertNotIn(entry, set(hl.settlement_time))
        self.assertIn(entry + pd.Timedelta(milliseconds=1), set(hl.settlement_time))
        self.assertIn(end, set(hl.settlement_time))
        self.assertNotIn(end + pd.Timedelta(milliseconds=1), set(hl.settlement_time))
        self.assertEqual(set(hl.valuation_source), {'trade_open_oracle_proxy'})
        self.assertTrue((hl.reference_price == 100).all())
        self.assertAlmostEqual(hl.iloc[0].cash, ledger.iloc[0].quantity * 100 * .0003)
        self.assertEqual(summary['oracle_proxy_payments'], len(hl))
        self.assertTrue((payments.settlement_time <= ledger.iloc[0].exit_time).all())

    def test_endpoint_mark_and_cex_hourly_mark_override_trade_open(self):
        funding, prices = fixture(hours=73, rates=(0.001, 0.003))
        time = START + pd.Timedelta(hours=8)
        funding.loc[(funding.exchange == 'binance') & (funding.settlement_time == time),
                    'settlement_mark_price'] = 120
        prices.loc[(prices.exchange == 'bybit') & (prices.time == time), 'mark_open'] = 130
        paid = run(funding, prices)[3]
        paid = paid[paid.settlement_time == time].set_index('exchange')
        self.assertAlmostEqual(paid.loc['binance', 'cash'], -100 * 120 * .001)
        self.assertAlmostEqual(paid.loc['bybit', 'cash'], 100 * 130 * .003)
        self.assertEqual(paid.loc['binance', 'valuation_source'], 'endpoint_settlement_mark')
        self.assertEqual(paid.loc['bybit', 'valuation_source'], 'hourly_mark_open_proxy')

    def test_empty_funding_and_rejected_forecasts_preserve_capital(self):
        funding, prices = fixture(hours=74, rates=(0, 0.00001))
        for events in (funding, funding.iloc[:0]):
            ledger, curve, _, payments, summary = run(events, prices)
            self.assertTrue(ledger.empty)
            self.assertTrue(payments.empty)
            self.assertTrue((curve.equity == 100000).all())
            self.assertEqual(summary['net_pnl'], 0)
            self.assertEqual(summary['max_drawdown_pct'], 0)
            self.assertIsNone(summary['mean_fee_per_fill_bps'])

    def test_full_72_hour_horizon_must_fit_after_next_hour_fill(self):
        for hours, trades in ((72, 0), (73, 1)):
            funding, prices = fixture(hours=hours)
            ledger, _, decisions, _, _ = run(funding, prices)
            self.assertEqual(len(ledger), trades)
            if trades:
                self.assertEqual(ledger.iloc[0].entry_time + pd.Timedelta(hours=72), prices.time.max())
            else:
                self.assertTrue((decisions.action == 'insufficient_time').all())

    def test_capital_reserve_pair_cap_and_nonnegative_sizing(self):
        funding, prices = fixture(hours=73, bases=tuple('ABCDEFG'))
        cfg = bt.Config(capital=31000, max_pairs=5)
        ledger, curve, _, _, summary = run(funding, prices, cfg)
        self.assertLessEqual(summary['max_open_pairs'], 5)
        self.assertGreater(len(ledger), 0)
        self.assertTrue((ledger.quantity > 0).all())
        self.assertTrue((ledger.quantity <= 100).all())
        active = curve[curve.open_pairs > 0]
        self.assertTrue((active.gross_exposure <= active.equity - cfg.reserve + 1e-6).all())
        ledger2, _, _, _, _ = run(funding, prices, replace(cfg, capital=1000))
        self.assertTrue(ledger2.empty)


class HoldingPolicyTests(unittest.TestCase):
    def test_maximum_hold_and_cooldown_prevent_same_hour_churn(self):
        funding, prices = fixture(hours=270)
        ledger, _, _, _, _ = run(funding, prices)
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger.iloc[0].exit_reason, 'max_holding')
        self.assertEqual(ledger.iloc[0].exit_time - ledger.iloc[0].entry_time,
                         pd.Timedelta(hours=168))
        self.assertGreaterEqual(ledger.iloc[1].entry_time,
                                ledger.iloc[0].exit_time + pd.Timedelta(hours=24))

    def controlled(self, mode):
        funding, prices = fixture(hours=100, bases=('BTC', 'ETH'))
        original = bt.cached_forecasts
        def forecasts(events, decision, cfg, cache):
            rows = original(events, decision, cfg, cache)
            hour = (decision - START).total_seconds() / 3600
            for row in rows:
                if row['base'] == 'ETH':
                    row['expected_72h_bps'] = 1000 if hour >= 2 else 0
                    row['threshold_passed'] = hour >= 2 and row['long_exchange'] == 'binance'
                if row['base'] == 'BTC' and row['long_exchange'] == 'binance':
                    if mode in ('cover', 'boundary', 'sunk') and hour >= 2:
                        row['expected_24h_bps'] = {'cover': 18.4, 'boundary': 18.5,
                                                   'sunk': 19.0}[mode]
                    if mode == 'reverse' and hour >= 25:
                        row['mean_spread_8h_bps'] = -1
                        row['expected_24h_bps'] = 100
            return sorted(rows, key=lambda row: -row['expected_72h_bps'])
        with patch.object(bt, 'cached_forecasts', side_effect=forecasts):
            return run(funding, prices, bt.Config(max_pairs=1))[0]

    def test_better_ranked_pair_alone_does_not_exit_held_pair(self):
        ledger = self.controlled('rank')
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger.iloc[0].base, 'BTC')
        self.assertEqual(ledger.iloc[0].exit_reason, 'sample_end')

    def test_future_exit_cost_only_and_minimum_hold(self):
        # 5 + 5.5 + 2*3 + 2 = 18.5 bps; entry costs are sunk.
        ledger = self.controlled('cover')
        trade = ledger[ledger.base == 'BTC'].iloc[0]
        self.assertEqual(trade.exit_reason, 'insufficient_24h_cost_cover')
        self.assertEqual(trade.exit_time - trade.entry_time, pd.Timedelta(hours=24))

    def test_exit_cost_coverage_equality_does_not_trigger_below_threshold_exit(self):
        ledger = self.controlled('boundary')
        self.assertEqual(ledger.iloc[0].exit_reason, 'sample_end')

    def test_exit_ignores_sunk_entry_fees_and_penalties(self):
        ledger = self.controlled('sunk')
        self.assertEqual(ledger.iloc[0].exit_reason, 'sample_end')

    def test_reversal_requires_three_consecutive_hourly_decisions(self):
        ledger = self.controlled('reverse')
        trade = ledger[ledger.base == 'BTC'].iloc[0]
        self.assertEqual(trade.exit_reason, 'direction_reversed_3_decisions')
        self.assertEqual(trade.exit_time, START + pd.Timedelta(hours=28))


if __name__ == '__main__':
    unittest.main()
