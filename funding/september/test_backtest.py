"""Synthetic financial and causality fixtures for the September four-venue replay.

Run: python -B -m unittest discover -s funding/september -p test_backtest.py -v
These tests establish synthetic accounting behavior, not real-data replication.
"""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


_SPEC = importlib.util.spec_from_file_location(
    "adjacentseptmodule", Path(__file__).with_name("backtest.py"))
bt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bt
_SPEC.loader.exec_module(bt)

START = pd.Timestamp("2026-09-01T00:00:00Z")


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
    def test_three_venue_pair_selection_uses_largest_qualifying_gap(self):
        funding,_=fixture()
        extra=funding[funding.exchange=='bybit'].copy()
        extra['exchange']='okx'
        extra['funding_rate']=.006
        rates=bt.known_rates(pd.concat([funding,extra],ignore_index=True),START,bt.Config())
        ranked=bt.rank_pairs(rates,START,bt.Config())
        self.assertEqual(ranked[0]['long_exchange'],'binance')
        self.assertEqual(ranked[0]['short_exchange'],'okx')
        self.assertAlmostEqual(ranked[0]['entry_threshold_bps'],22)
        self.assertTrue(ranked[0]['threshold_passed'])

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
        expected_q = 500 / (200 + 2 * (100 * 5 + 100 * 5.5) / 10000)
        self.assertAlmostEqual(trade.quantity, expected_q)
        entry = curve[curve.time == trade.entry_time].iloc[0]
        self.assertLessEqual(entry.gross_exposure + trade.entry_fees, 500 + 1e-9)
        self.assertLess(trade.quantity * 100, config.leg_notional)
        no_budget = replace(config, capital=1049)
        self.assertTrue(run_fixture(funding, prices, no_budget)[0].empty)


def four_venue_fixture(hours=26, bases=("BTC",), prehistory=True):
    funding, prices = [], []
    settings = (("binance", 8, 0.0), ("bybit", 8, 0.001),
                ("okx", 8, 0.002), ("hyperliquid", 1, 0.0005))
    for base in bases:
        for exchange, interval, rate in settings:
            for hour in range(-2 * interval if prehistory else 0, hours + 9, interval):
                funding.append(dict(exchange=exchange, base=base,
                                    settlement_time=START + pd.Timedelta(hours=hour),
                                    funding_rate=rate, settlement_mark_price=np.nan))
            for time in pd.date_range(START, periods=hours + 1, freq="h"):
                prices.append(dict(exchange=exchange, base=base, time=time,
                                   trade_open=100.0, mark_open=120.0))
    return pd.DataFrame(funding), pd.DataFrame(prices)


class SeptemberTests(unittest.TestCase):
    def test_four_venue_ranking_and_fee_schedule(self):
        self.assertEqual(bt.FEES, {"binance": 5.0, "bybit": 5.5,
                                  "okx": 5.0, "hyperliquid": 4.5})
        funding, _ = four_venue_fixture()
        rates = bt.known_rates(funding, START, bt.Config())
        self.assertEqual(set(rates.exchange), set(bt.FEES))
        pair = bt.rank_pairs(rates, START, bt.Config())[0]
        self.assertEqual((pair["long_exchange"], pair["short_exchange"]),
                         ("binance", "hyperliquid"))
        self.assertAlmostEqual(pair["edge_8h_bps"], 40)
        self.assertEqual(pair["entry_threshold_bps"], 21)
        self.assertTrue(pair["threshold_passed"])

    def test_hyperliquid_normalized_once_and_strict_pair_thresholds(self):
        for other, threshold in (("binance", 21), ("okx", 21), ("bybit", 22)):
            for offset, passed in ((-0.01, False), (0, False), (0.01, True)):
                with self.subTest(other=other, offset=offset):
                    funding, _ = four_venue_fixture()
                    funding = funding[funding.exchange.isin([other, "hyperliquid"])].copy()
                    funding["normalized_8h"] = 999.0
                    funding.loc[funding.exchange == other, "funding_rate"] = 0.0
                    raw = (threshold + offset) / 80000
                    funding.loc[funding.exchange == "hyperliquid", "funding_rate"] = raw
                    rates = bt.known_rates(funding, START, bt.Config())
                    hl = rates.set_index("exchange").loc["hyperliquid"]
                    self.assertEqual(hl.interval_hours, 1)
                    self.assertAlmostEqual(hl.normalized_8h, raw * 8)
                    pair = bt.rank_pairs(rates, START, bt.Config())[0]
                    self.assertEqual(pair["entry_threshold_bps"], threshold)
                    self.assertEqual(pair["threshold_passed"], passed)

    def test_hyperliquid_hourly_raw_payments_and_explicit_trade_open_proxy(self):
        funding, prices = four_venue_fixture(hours=18)
        ledger, _, decisions, payments, summary = run_fixture(funding, prices)
        self.assertEqual(len(ledger), 1)
        self.assertAlmostEqual(decisions.iloc[0].edge_8h_bps, 40)
        hl = payments[payments.exchange == "hyperliquid"]
        self.assertEqual(len(hl), 17)
        self.assertEqual(set(hl.valuation_source), {"trade_open_oracle_proxy"})
        self.assertTrue((hl.reference_price == 100).all())
        self.assertTrue((hl.funding_rate == 0.0005).all())
        np.testing.assert_allclose(hl.cash, 100 * 100 * 0.0005)
        self.assertEqual(set(payments.loc[payments.exchange == "binance", "valuation_source"]),
                         {"hourly_mark_open_proxy"})
        self.assertEqual(summary["oracle_proxy_payments"], 17)
        self.assertAlmostEqual(summary["funding_pnl"], 85)

    def test_hyperliquid_four_fill_fees(self):
        funding, prices = four_venue_fixture(hours=18)
        trade = run_fixture(funding, prices, bt.Config(fill_cost_bps=3))[0].iloc[0]
        self.assertEqual(trade.short_exchange, "hyperliquid")
        expected_entry = 100 * (100.03 * 5 + 99.97 * 4.5) / 10000
        expected_exit = 100 * (99.97 * 5 + 100.03 * 4.5) / 10000
        self.assertAlmostEqual(trade.entry_fees, expected_entry)
        self.assertAlmostEqual(trade.fees, expected_entry + expected_exit)
        self.assertAlmostEqual(trade.execution_cost, 12)
        self.assertAlmostEqual(trade.net_pnl, trade.funding_pnl - 12 - trade.fees)

    def test_all_three_cex_mark_payments_ignore_trade_open_proxy_multiplier(self):
        for venue in ("binance", "bybit", "okx"):
            with self.subTest(venue=venue):
                funding, prices = four_venue_fixture(hours=18)
                funding["funding_rate"] = 0.0
                funding.loc[funding.exchange == venue, "funding_rate"] = 0.004
                funding.loc[funding.exchange == "hyperliquid", "funding_rate"] = -0.0001
                ledger, _, _, payments, _ = run_fixture(
                    funding, prices, bt.Config(fill_cost_bps=0, oracle_proxy_multiplier=1.2))
                self.assertEqual(ledger.iloc[0].short_exchange, venue)
                cex = payments[payments.exchange == venue]
                self.assertEqual(len(cex), 2)
                self.assertEqual(set(cex.valuation_source), {"hourly_mark_open_proxy"})
                np.testing.assert_allclose(cex.reference_price, 120)
                np.testing.assert_allclose(cex.cash, 100 * 120 * 0.004)

    def test_endpoint_settlement_mark_takes_precedence_over_proxy(self):
        funding, prices = four_venue_fixture(hours=18)
        funding["settlement_mark_price"] = 150.0
        baseline = run_fixture(funding, prices)
        changed = run_fixture(funding, prices, bt.Config(fill_cost_bps=0, oracle_proxy_multiplier=1.2))
        self.assertFalse(baseline[3].empty)
        self.assertEqual(set(baseline[3].valuation_source), {"endpoint_settlement_mark"})
        np.testing.assert_allclose(baseline[3].reference_price, 150)
        for left, right in zip(baseline[:4], changed[:4]):
            pd.testing.assert_frame_equal(left, right)
        self.assertEqual(changed[4]["oracle_proxy_payments"], 0)
        self.assertAlmostEqual(baseline[4]["funding_pnl"], changed[4]["funding_pnl"])

    def test_proxy_multiplier_changes_only_hl_cash_at_fixed_quantities(self):
        funding, prices = four_venue_fixture(hours=18, bases=("BTC", "ETH"))
        funding.loc[(funding.base == "ETH") & (funding.exchange == "hyperliquid"), "funding_rate"] = 0
        funding.loc[(funding.base == "ETH") & (funding.exchange == "bybit"), "funding_rate"] = 0.006
        baseline = run_fixture(funding, prices)
        for multiplier in (0.99, 1.01):
            with self.subTest(multiplier=multiplier):
                changed = run_fixture(funding, prices, bt.Config(fill_cost_bps=0,
                                      oracle_proxy_multiplier=multiplier))
                pd.testing.assert_frame_equal(baseline[2], changed[2])
                columns = ["base", "entry_time", "exit_time", "quantity", "long_entry_price",
                           "short_entry_price", "long_exit_price", "short_exit_price",
                           "fees", "price_pnl", "execution_cost"]
                pd.testing.assert_frame_equal(baseline[0][columns], changed[0][columns])
                original, altered = baseline[3], changed[3]
                hl = original.exchange == "hyperliquid"
                pd.testing.assert_frame_equal(original[~hl], altered[~hl])
                np.testing.assert_allclose(altered.loc[hl, "cash"], original.loc[hl, "cash"] * multiplier)
                self.assertAlmostEqual(changed[4]["net_pnl"] - baseline[4]["net_pnl"],
                                       original.loc[hl, "cash"].sum() * (multiplier - 1))

    def test_month_only_history_warms_up_without_prestart_funding(self):
        funding, prices = four_venue_fixture(prehistory=False)
        self.assertEqual(funding.settlement_time.min(), START)
        ledger, curve, decisions, _, summary = run_fixture(funding, prices)
        self.assertEqual(summary["eligible_markets"], [[e, "BTC"] for e in sorted(bt.FEES)])
        self.assertTrue((curve.loc[curve.time <= START + pd.Timedelta(hours=8), "open_pairs"] == 0).all())
        self.assertEqual(decisions.decision_time.min(), START + pd.Timedelta(hours=16))
        self.assertEqual(ledger.entry_time.min(), START + pd.Timedelta(hours=17))

    def test_empty_funding_preserves_initial_universe_and_capital(self):
        funding, prices = four_venue_fixture(hours=18)
        ledger, curve, decisions, payments, summary = run_fixture(funding.iloc[:0], prices)
        self.assertTrue(ledger.empty)
        self.assertTrue(decisions.empty)
        self.assertTrue(payments.empty)
        self.assertEqual(summary["eligible_markets"], [[e, "BTC"] for e in sorted(bt.FEES)])
        self.assertTrue((curve.equity == bt.Config().capital).all())
        self.assertEqual(summary["net_pnl"], 0)

    def test_initial_universe_excludes_later_listing_not_early_delisting(self):
        funding, prices = four_venue_fixture(hours=18, bases=("BTC", "LATE", "SHORT"))
        prices = prices[~((prices.base == "LATE") & (prices.time == START))].copy()
        prices = prices[~((prices.base == "SHORT") & (prices.time > START))].copy()
        funding.loc[funding.base == "SHORT", "funding_rate"] = 0
        ledger, _, _, payments, summary = run_fixture(funding, prices)
        self.assertEqual(summary["eligible_markets"],
                         [[e, b] for e in sorted(bt.FEES) for b in ("BTC", "SHORT")])
        self.assertEqual(set(ledger.base), {"BTC"})
        self.assertEqual(set(payments.base), {"BTC"})

    def test_millisecond_settlements_belong_to_hold_strict_entry_inclusive_exit(self):
        for offset in (pd.Timedelta(0), pd.Timedelta(milliseconds=20)):
            with self.subTest(offset=offset):
                funding, prices = fixture()
                funding.loc[(funding.exchange == "bybit") &
                            (funding.settlement_time == START + pd.Timedelta(hours=8)), "funding_rate"] = -0.001
                extras = [dict(exchange="bybit", base="BTC", funding_rate=0.0007,
                               settlement_mark_price=np.nan,
                               settlement_time=START + pd.Timedelta(hours=h) + offset)
                          for h in (1, 9)]
                funding = pd.concat([funding, pd.DataFrame(extras)], ignore_index=True)
                ledger, _, _, payments, summary = run_fixture(funding, prices)
                trade = ledger.iloc[0]
                self.assertEqual(trade.entry_time, START + pd.Timedelta(hours=1))
                self.assertEqual(trade.exit_time, START + pd.Timedelta(hours=9))
                expected = {START + pd.Timedelta(hours=8)}
                expected.add(START + pd.Timedelta(hours=1 if offset else 9) + offset)
                self.assertEqual(set(payments.settlement_time), expected)
                self.assertEqual(len(payments), 3)
                self.assertTrue((payments.settlement_time > trade.entry_time).all())
                self.assertTrue((payments.settlement_time <= trade.exit_time).all())
                self.assertAlmostEqual(summary["funding_pnl"], -3)

    def test_final_month_end_excludes_settlement_twenty_ms_after_23h(self):
        funding, prices = four_venue_fixture(hours=18)
        shift = bt.END - prices.time.max()
        funding["settlement_time"] += shift
        prices["time"] += shift
        extras = [dict(exchange="hyperliquid", base="BTC", funding_rate=0.5,
                       settlement_mark_price=np.nan, settlement_time=bt.END + offset)
                  for offset in (pd.Timedelta(milliseconds=20), pd.Timedelta(minutes=30))]
        baseline = bt.replay(funding, prices, bt.Config(fill_cost_bps=0), start=prices.time.min(), end=bt.END)
        changed = bt.replay(pd.concat([funding, pd.DataFrame(extras)], ignore_index=True),
                            prices, bt.Config(fill_cost_bps=0), start=prices.time.min(), end=bt.END)
        self.assertFalse(baseline[0].empty)
        self.assertEqual(baseline[0].iloc[-1].exit_time, bt.END)
        self.assertTrue((baseline[3].settlement_time == bt.END).any())
        for index, (left, right) in enumerate(zip(baseline[:4], changed[:4])):
            if index == 3:
                keys = ["settlement_time", "exchange", "base"]
                left = left.sort_values(keys).reset_index(drop=True)
                right = right.sort_values(keys).reset_index(drop=True)
            pd.testing.assert_frame_equal(left, right)
        self.assertTrue((changed[3].settlement_time <= bt.END).all())
        self.assertEqual(baseline[4], changed[4])

    def test_four_venue_future_mutations_preserve_prior_outputs_both_policies(self):
        funding, prices = four_venue_fixture()
        cutoff = START + pd.Timedelta(hours=8)
        mutated = funding.copy()
        future = mutated.settlement_time > cutoff
        mutated.loc[future, "funding_rate"] = -0.5
        mutated.loc[future, "settlement_time"] += pd.Timedelta(milliseconds=20)
        altered_prices = prices.copy()
        altered_prices.loc[altered_prices.time > cutoff, ["trade_open", "mark_open"]] *= 2
        for policy in ("original", "august_frozen"):
            with self.subTest(policy=policy):
                config = bt.Config(policy=policy, fill_cost_bps=0)
                original = run_fixture(funding, prices, config)
                changed = run_fixture(mutated, altered_prices, config)
                for index, time_column in ((1, "time"), (2, "decision_time"), (3, "settlement_time")):
                    left, right = original[index], changed[index]
                    pd.testing.assert_frame_equal(left[left[time_column] <= cutoff].reset_index(drop=True),
                                                  right[right[time_column] <= cutoff].reset_index(drop=True))
                columns = ["base", "entry_time", "quantity", "long_entry_price", "short_entry_price"]
                pd.testing.assert_frame_equal(original[0].loc[original[0].entry_time <= cutoff, columns].reset_index(drop=True),
                                              changed[0].loc[changed[0].entry_time <= cutoff, columns].reset_index(drop=True))

    def test_no_initial_markets_produces_empty_universe_without_later_entries(self):
        funding, prices = four_venue_fixture(hours=18)
        prices = prices[prices.time > START].copy()
        ledger, curve, decisions, payments, summary = run_fixture(funding, prices)
        self.assertEqual(summary["eligible_markets"], [])
        self.assertTrue(ledger.empty)
        self.assertTrue(decisions.empty)
        self.assertTrue(payments.empty)
        self.assertTrue((curve.equity == bt.Config().capital).all())

    def test_main_runs_exactly_six_policy_cost_scenarios(self):
        funding, prices = four_venue_fixture(hours=18)
        manifest = dict(dataset_id="synthetic", common_bases=["BTC"], hashes={},
                        end_exclusive=(prices.time.max() + pd.Timedelta(hours=1)).isoformat())
        directory, data_directory = MagicMock(), MagicMock()
        (data_directory / "dataset.json").read_bytes.return_value = b"synthetic"
        figure = MagicMock()
        axes = [MagicMock() for _ in range(3)]
        actual_replay = bt.replay
        calls = []

        def tiny_replay(actual_funding, actual_prices, config, **kwargs):
            self.assertIs(actual_funding, funding)
            self.assertIs(actual_prices, prices)
            calls.append(config)
            return actual_replay(funding, prices, config, start=START, end=prices.time.max(),
                                 **kwargs)

        with patch.object(bt, "load_inputs", return_value=(funding, prices, manifest)), \
                patch.object(bt, "replay", side_effect=tiny_replay), \
                patch.object(pd.DataFrame, "to_csv"), \
                patch.object(bt.plt, "subplots", return_value=(figure, axes)), \
                patch.object(bt.plt, "close"), patch("builtins.print"):
            bt.main(directory=directory, data_directory=data_directory)
        self.assertEqual([(c.policy, c.fill_cost_bps) for c in calls],
                         [(p, cost) for p in ("original", "august_frozen") for cost in (1, 3, 5)])
        self.assertTrue(all(c.oracle_proxy_multiplier == 1 for c in calls))
        report = bt.json.loads((directory / "results.json").write_text.call_args.args[0])
        self.assertEqual(len(report["results"]), 6)
        for policy in ("original", "august_frozen"):
            results = [r for r in report["results"] if r["config"]["policy"] == policy]
            self.assertTrue(all(r["trades"] == 1 for r in results))
            self.assertGreater(results[0]["net_pnl"], results[1]["net_pnl"])
            self.assertGreater(results[1]["net_pnl"], results[2]["net_pnl"])
            self.assertAlmostEqual(results[0]["funding_pnl"], results[2]["funding_pnl"])

    def test_august_frozen_strict_one_bp_entry_threshold(self):
        for edge, passed in ((0.99, False), (1.0, False), (1.01, True)):
            with self.subTest(edge=edge):
                funding, _ = fixture(short_rate=edge / 10000)
                config = bt.Config(policy="august_frozen")
                pair = bt.rank_pairs(bt.known_rates(funding, START, config), START, config)[0]
                self.assertEqual(pair["entry_threshold_bps"], 1)
                self.assertEqual(pair["threshold_passed"], passed)

    def test_august_frozen_minimum_eight_hour_hold_and_hourly_decisions(self):
        funding, prices = fixture()
        funding.loc[(funding.exchange == "bybit") & (funding.settlement_time >= START), "funding_rate"] = 0.00001
        config = bt.Config(policy="august_frozen", fill_cost_bps=0)
        ledger, _, decisions, _, _ = run_fixture(funding, prices, config)
        trade = ledger.iloc[0]
        self.assertEqual(trade.entry_time, START + pd.Timedelta(hours=1))
        self.assertEqual(trade.exit_time - trade.entry_time, pd.Timedelta(hours=8))
        self.assertEqual(trade.exit_reason, "spread_narrowed")
        self.assertIn(START + pd.Timedelta(hours=1), set(decisions.decision_time))

    def test_august_frozen_maximum_48_hour_hold(self):
        funding, prices = four_venue_fixture(hours=66)
        ledger, _, _, _, _ = run_fixture(funding, prices, bt.Config(policy="august_frozen", fill_cost_bps=0))
        held = ledger.exit_time - ledger.entry_time
        self.assertTrue((held <= pd.Timedelta(hours=48)).all())
        expired = ledger[ledger.exit_reason == "max_holding"]
        self.assertFalse(expired.empty)
        self.assertTrue(((expired.exit_time - expired.entry_time) == pd.Timedelta(hours=48)).all())

    def test_august_frozen_top_five_strongest_bases(self):
        bases = tuple("COIN%d" % i for i in range(7))
        funding, prices = four_venue_fixture(hours=18, bases=bases)
        for i, base in enumerate(bases):
            funding.loc[(funding.base == base) & (funding.exchange == "hyperliquid"), "funding_rate"] = (i + 4) / 10000
        ledger, curve, decisions, _, summary = run_fixture(
            funding, prices, bt.Config(policy="august_frozen", fill_cost_bps=0))
        self.assertEqual(set(ledger.base), set(bases[2:]))
        self.assertEqual(summary["max_open_pairs"], 5)
        self.assertTrue((curve.open_pairs <= 5).all())
        self.assertEqual(list(decisions[decisions.decision_time == START].base), list(reversed(bases)))


if __name__ == "__main__":
    unittest.main()
