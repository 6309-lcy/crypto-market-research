"""Tiny financial fixtures for the Binance/Bybit hourly-open proxy replay.

Run: python -B -m unittest discover -s funding/jan_mar -p test_backtest.py -v
These tests establish accounting behavior, not real-data or four-venue replication.
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
    "jan_mar_backtest_under_test", Path(__file__).with_name("backtest.py"))
bt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bt
_SPEC.loader.exec_module(bt)

START = pd.Timestamp("2026-01-24T08:00:00Z")


def fixture(hours=18, bases=("BTC",), long_rate=0.0, short_rate=0.004,
            long_price=100.0, short_price=100.0):
    """Actual settlement rows and hourly trade/mark opens on a tiny grid."""
    funding = []
    prices = []
    for base in bases:
        for exchange, rate, price in (("binance", long_rate, long_price),
                                      ("bybit", short_rate, short_price)):
            for hour in range(-16, hours + 9, 8):
                funding.append(dict(exchange=exchange, base=base,
                                    settlement_time=START + pd.Timedelta(hours=hour),
                                    funding_rate=rate, settlement_mark_price=np.nan))
            for time in pd.date_range(START, periods=hours + 1, freq="h"):
                prices.append(dict(exchange=exchange, base=base, time=time,
                                   trade_open=price, mark_open=price))
    return pd.DataFrame(funding), pd.DataFrame(prices)


def run_fixture(funding, prices, config=None):
    return bt.replay(funding, prices, config or bt.Config(fill_cost_bps=0),
                     start=START, end=prices.time.max())


class SignalTests(unittest.TestCase):
    def test_five_minute_publication_boundary(self):
        funding, _ = fixture()
        for minutes, expected_hour in ((4, -8), (5, 0)):
            with self.subTest(minutes=minutes):
                rates = bt.known_rates(funding, START + pd.Timedelta(minutes=minutes), bt.Config())
                self.assertEqual(set(rates.source_time),
                                 {START + pd.Timedelta(hours=expected_hour)})
                self.assertTrue((rates.available_time <= START + pd.Timedelta(minutes=minutes)).all())

    def test_interval_inference_uses_two_past_events_only(self):
        funding, _ = fixture()
        future = funding.settlement_time > START
        funding.loc[future, "settlement_time"] += pd.Timedelta(hours=3)
        rates = bt.known_rates(funding, START, bt.Config())
        self.assertEqual(set(rates.interval_hours), {8})
        self.assertEqual(set(rates.source_time), {START - pd.Timedelta(hours=8)})
        one = funding[funding.settlement_time == START - pd.Timedelta(hours=8)]
        self.assertTrue(bt.known_rates(one, START, bt.Config()).empty)

    def test_stale_and_irregular_intervals_are_rejected(self):
        funding, _ = fixture()
        past = funding[funding.settlement_time < START].copy()
        past.loc[past.settlement_time == START - pd.Timedelta(hours=16),
                 "settlement_time"] += pd.Timedelta(hours=1)
        self.assertTrue(bt.known_rates(past, START, bt.Config()).empty)
        old = funding[funding.settlement_time <= START]
        self.assertTrue(bt.known_rates(old, START + pd.Timedelta(hours=9), bt.Config()).empty)

    def test_rates_are_normalized_exactly_once(self):
        rows = []
        for exchange, interval, rate in (("binance", 1, 0.0001), ("bybit", 8, 0.004)):
            for hour in (-interval, 0):
                rows.append(dict(exchange=exchange, base="BTC", funding_rate=rate,
                                 normalized_8h=999.0,
                                 settlement_time=START + pd.Timedelta(hours=hour)))
        rates = bt.known_rates(pd.DataFrame(rows), START + pd.Timedelta(minutes=5), bt.Config())
        by_venue = rates.set_index("exchange")
        self.assertAlmostEqual(by_venue.loc["binance", "normalized_8h"], 0.0008)
        self.assertAlmostEqual(by_venue.loc["bybit", "normalized_8h"], 0.004)
        ranked = bt.rank_pairs(rates, START + pd.Timedelta(minutes=5), bt.Config())
        self.assertAlmostEqual(ranked[0]["edge_8h_bps"], 32.0)
        self.assertEqual(ranked[0]["long_exchange"], "binance")

    def test_threshold_is_original_two_bps_plus_roundtrip_fees(self):
        self.assertEqual(bt.Config().entry_buffer_bps, 2.0)
        for edge, passed in ((22.9, False), (23.0, False), (23.1, True)):
            with self.subTest(edge=edge):
                funding, _ = fixture(short_rate=edge / 10000)
                rates = bt.known_rates(funding, START, bt.Config())
                pair = bt.rank_pairs(rates, START, bt.Config())[0]
                self.assertAlmostEqual(pair["entry_threshold_bps"], 23.0)
                self.assertEqual(pair["threshold_passed"], passed)

    def test_future_mutations_cannot_change_earlier_signals_or_entries(self):
        funding, prices = fixture()
        changed = funding.copy()
        future = changed.settlement_time > START + pd.Timedelta(hours=8)
        changed.loc[future, "funding_rate"] = -0.5
        changed.loc[future, "settlement_time"] += pd.Timedelta(hours=1)
        changed_prices = prices.copy()
        changed_prices.loc[changed_prices.time > START + pd.Timedelta(hours=9),
                           ["trade_open", "mark_open"]] *= 2
        left = run_fixture(funding, prices)
        right = run_fixture(changed, changed_prices)
        cutoff = START + pd.Timedelta(hours=8)
        pd.testing.assert_frame_equal(left[2][left[2].decision_time <= cutoff].reset_index(drop=True),
                                      right[2][right[2].decision_time <= cutoff].reset_index(drop=True))
        pd.testing.assert_frame_equal(left[1][left[1].time <= cutoff].reset_index(drop=True),
                                      right[1][right[1].time <= cutoff].reset_index(drop=True))
        for col in ("entry_time", "quantity", "long_entry_price", "short_entry_price"):
            self.assertEqual(left[0].iloc[0][col], right[0].iloc[0][col])


class ReplayFinancialTests(unittest.TestCase):
    def test_entry_is_next_hour_open_with_equal_base_quantity(self):
        funding, prices = fixture(long_price=100, short_price=125)
        entry = START + pd.Timedelta(hours=1)
        prices.loc[(prices.time == entry) & (prices.exchange == "binance"), "trade_open"] = 110
        ledger, _, _, _, _ = run_fixture(funding, prices)
        trade = ledger.iloc[0]
        self.assertEqual(trade.entry_time, trade.decision_time + pd.Timedelta(hours=1))
        self.assertEqual(trade.long_entry_reference, 110)
        self.assertEqual(trade.short_entry_reference, 125)
        self.assertAlmostEqual(trade.quantity, 10000 / 125)
        self.assertNotEqual(trade.quantity * 110, trade.quantity * 125)
        self.assertAlmostEqual(trade.reference_price_pnl, trade.quantity * -10)

    def test_raw_settlement_rates_marks_and_payment_signs(self):
        funding, prices = fixture(long_rate=0.001, short_rate=0.005)
        event = START + pd.Timedelta(hours=8)
        funding.loc[(funding.exchange == "binance") & (funding.settlement_time == event),
                    "settlement_mark_price"] = 120
        prices.loc[(prices.exchange == "bybit") & (prices.time == event), "mark_open"] = 130
        _, _, _, payments, _ = run_fixture(funding, prices)
        paid = payments[payments.settlement_time == event].set_index("exchange")
        self.assertAlmostEqual(paid.loc["binance", "cash"], -100 * 120 * 0.001)
        self.assertAlmostEqual(paid.loc["bybit", "cash"], 100 * 130 * 0.005)
        self.assertEqual(paid.loc["binance", "valuation_source"], "endpoint_settlement_mark")
        self.assertEqual(paid.loc["bybit", "valuation_source"], "hourly_mark_open_proxy")

    def test_hourly_settlements_pay_raw_rates_not_eight_hour_signals(self):
        _, prices = fixture()
        rows = []
        for exchange, rate in (("binance", 0.0001), ("bybit", 0.0005)):
            for hour in range(-2, 20):
                rows.append(dict(exchange=exchange, base="BTC", funding_rate=rate,
                                 settlement_time=START + pd.Timedelta(hours=hour),
                                 settlement_mark_price=100.0))
        ledger, _, decisions, payments, summary = run_fixture(pd.DataFrame(rows), prices)
        self.assertEqual(len(ledger), 1)
        self.assertAlmostEqual(decisions.iloc[0].edge_8h_bps, 32)
        self.assertEqual(len(payments), 34)
        self.assertAlmostEqual(summary["funding_pnl"], 17 * 100 * 100 * (0.0005 - 0.0001))
        self.assertAlmostEqual(payments.cash.sum(), summary["funding_pnl"])

    def test_negative_rates_reverse_cash_signs(self):
        funding, prices = fixture(long_rate=-0.005, short_rate=-0.001)
        _, _, _, payments, _ = run_fixture(funding, prices)
        self.assertTrue((payments.loc[payments.side == 1, "cash"] > 0).all())
        self.assertTrue((payments.loc[payments.side == -1, "cash"] < 0).all())

    def test_only_actual_events_during_hold_are_paid(self):
        funding, prices = fixture(hours=18)
        extras = []
        for hour in (1, 9, 10, 19):
            extras.append(dict(exchange="bybit", base="BTC",
                               settlement_time=START + pd.Timedelta(hours=hour),
                               funding_rate=0.0007, settlement_mark_price=100.0))
        funding = pd.concat([funding, pd.DataFrame(extras)], ignore_index=True)
        # A negative new spread schedules an exit at hour 9.
        funding.loc[(funding.exchange == "bybit") &
                    (funding.settlement_time == START + pd.Timedelta(hours=8)), "funding_rate"] = -0.001
        ledger, _, _, payments, summary = run_fixture(funding, prices)
        self.assertEqual(len(ledger), 1)
        trade = ledger.iloc[0]
        self.assertEqual(trade.exit_time, START + pd.Timedelta(hours=9))
        self.assertTrue((payments.settlement_time > trade.entry_time).all())
        self.assertTrue((payments.settlement_time <= trade.exit_time).all())
        self.assertEqual(set(payments.settlement_time),
                         {START + pd.Timedelta(hours=8), START + pd.Timedelta(hours=9)})
        # The exit-hour settlement precedes execution of the pending close.
        self.assertEqual(len(payments), 3)
        self.assertAlmostEqual(summary["funding_pnl"], -10 + 7)
        self.assertEqual(trade.settlement_payments, 3)

    def test_four_fill_fees_slippage_and_net_reconcile(self):
        funding, prices = fixture()
        config = bt.Config(fill_cost_bps=3)
        ledger, curve, _, payments, summary = run_fixture(funding, prices, config)
        trade = ledger.iloc[0]
        q = 100.0
        self.assertAlmostEqual(trade.long_entry_price, 100.03)
        self.assertAlmostEqual(trade.short_entry_price, 99.97)
        self.assertAlmostEqual(trade.long_exit_price, 99.97)
        self.assertAlmostEqual(trade.short_exit_price, 100.03)
        expected_fees = q * (100.03 * 5 + 99.97 * 5.5 + 99.97 * 5 + 100.03 * 5.5) / 10000
        self.assertAlmostEqual(trade.entry_fees, q * (100.03 * 5 + 99.97 * 5.5) / 10000)
        self.assertAlmostEqual(trade.fees, expected_fees)
        self.assertAlmostEqual(trade.execution_cost, 12)
        self.assertAlmostEqual(trade.price_pnl, -12)
        self.assertAlmostEqual(trade.funding_pnl, 80)
        self.assertAlmostEqual(trade.funding_pnl, payments.cash.sum())
        self.assertAlmostEqual(trade.net_pnl, 80 - 12 - expected_fees)
        for column in ("funding_pnl", "reference_price_pnl", "execution_cost", "price_pnl", "fees", "net_pnl"):
            self.assertAlmostEqual(summary[column], ledger[column].sum())
        self.assertAlmostEqual(curve.iloc[-1].equity, config.capital + trade.net_pnl)
        self.assertEqual(curve.iloc[-1].open_pairs, 0)
        self.assertEqual(curve.iloc[-1].unrealized_liquidation_pnl, 0)
        entry_equity = curve[curve.time == trade.entry_time].iloc[0].equity
        self.assertAlmostEqual(entry_equity, config.capital - 12 - expected_fees)

    def test_fees_and_slippage_sensitivity(self):
        funding, prices = fixture(short_rate=0.008)
        results = [run_fixture(funding, prices, bt.Config(fill_cost_bps=cost))[4]
                   for cost in (1, 3, 5)]
        self.assertTrue(all(result["trades"] == 1 for result in results))
        self.assertGreater(results[0]["net_pnl"], results[1]["net_pnl"])
        self.assertGreater(results[1]["net_pnl"], results[2]["net_pnl"])
        self.assertAlmostEqual(results[0]["funding_pnl"], results[2]["funding_pnl"])
        baseline = run_fixture(funding, prices)[4]
        with patch.dict(bt.FEES, {"binance": 10.0, "bybit": 11.0}):
            higher = run_fixture(funding, prices)[4]
        self.assertEqual(higher["trades"], 1)
        self.assertAlmostEqual(higher["fees"], 2 * baseline["fees"])
        self.assertAlmostEqual(baseline["net_pnl"] - higher["net_pnl"], baseline["fees"])

    def test_zero_trades_preserves_capital(self):
        funding, prices = fixture(short_rate=0.0001)
        ledger, curve, decisions, payments, summary = run_fixture(funding, prices)
        self.assertTrue(ledger.empty)
        self.assertTrue(payments.empty)
        self.assertFalse(decisions.threshold_passed.any())
        self.assertTrue((curve.equity == bt.Config().capital).all())
        self.assertEqual(summary["net_pnl"], 0)
        self.assertEqual(summary["max_drawdown_pct"], 0)

    def test_default_maximum_hold_is_48_hours(self):
        funding, prices = fixture(hours=66)
        ledger, _, _, _, _ = run_fixture(funding, prices)
        held = (ledger.exit_time - ledger.entry_time).dt.total_seconds() / 3600
        self.assertTrue((held <= 48).all())
        expired = ledger[ledger.exit_reason == "max_holding"]
        self.assertFalse(expired.empty)
        self.assertTrue(((expired.exit_time - expired.entry_time) == pd.Timedelta(hours=48)).all())

    def test_pair_capacity_and_strongest_first(self):
        funding, prices = fixture(bases=("BTC", "ETH", "SOL"))
        funding.loc[(funding.base == "SOL") & (funding.exchange == "bybit"), "funding_rate"] = 0.006
        config = bt.Config(max_pairs=1, fill_cost_bps=0)
        ledger, curve, _, _, summary = run_fixture(funding, prices, config)
        self.assertEqual(set(ledger.base), {"SOL"})
        self.assertEqual(summary["max_open_pairs"], 1)
        self.assertTrue((curve.open_pairs <= 1).all())

    def test_capital_reserve_and_entry_fees_limit_quantity(self):
        funding, prices = fixture()
        config = bt.Config(capital=1500, reserve=1000, fill_cost_bps=0)
        ledger, curve, _, _, _ = run_fixture(funding, prices, config)
        trade = ledger.iloc[0]
        expected_q = 500 / (200 + (100 * 5 + 100 * 5.5) / 10000)
        self.assertAlmostEqual(trade.quantity, expected_q)
        entry = curve[curve.time == trade.entry_time].iloc[0]
        self.assertLessEqual(entry.gross_exposure + trade.entry_fees, 500 + 1e-9)
        self.assertLess(trade.quantity * 100, config.leg_notional)
        no_budget = replace(config, capital=1049)
        self.assertTrue(run_fixture(funding, prices, no_budget)[0].empty)


if __name__ == "__main__":
    unittest.main()
