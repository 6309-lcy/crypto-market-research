"""
strategy.py - Funding rate arbitrage strategy logic.

Implements a delta-neutral funding rate arbitrage:
  - Long the perpetual on the exchange paying funding (lower/negative rate)
  - Short the perpetual on the exchange receiving funding (higher/positive rate)
  - Collect the funding rate differential each period
  - Account for fees, holding costs, and position rebalancing
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FundingArbConfig:
    """Configuration for the funding rate arbitrage strategy."""
    min_spread_bps: float = 2.0          # Min 8h-normalised spread (bps) to enter
    exit_spread_bps: float = 0.5         # Close when spread narrows to this
    max_holding_periods: int = 6         # Max funding periods to hold (6 × 8h = 2 days)
    position_size_usd: float = 10000.0   # USD per leg
    exchange_fees: dict = field(default_factory=lambda: {
        "binance": 0.0004,
        "bybit": 0.00055,
        "okx": 0.0005,
        "hyperliquid": 0.00035,
    })
    funding_hours: dict = field(default_factory=lambda: {
        "binance": 8,
        "bybit": 8,
        "okx": 8,
        "hyperliquid": 1,
    })


@dataclass
class ArbPosition:
    """An open arbitrage position (two legs)."""
    entry_time: str
    coin: str
    long_exchange: str
    short_exchange: str
    entry_spread_8h: float
    position_size_usd: float
    periods_held: int = 0
    cumulative_funding_pnl: float = 0.0
    entry_fees: float = 0.0


@dataclass
class ArbTrade:
    """A completed arbitrage trade."""
    coin: str
    long_exchange: str
    short_exchange: str
    entry_time: str
    exit_time: str
    entry_spread_8h: float
    exit_spread_8h: float
    periods_held: int
    position_size_usd: float
    funding_pnl: float
    total_fees: float
    net_pnl: float
    net_pnl_pct: float
    exit_reason: str


class FundingRateArbitrageStrategy:
    """
    Delta-neutral funding rate arbitrage strategy.

    For each funding period:
    1. Scan all coin/exchange pairs for spread opportunities
    2. Enter positions where spread exceeds threshold (after fees)
    3. Accumulate funding rate payments each period
    4. Exit when spread narrows, reverses, or max holding reached
    """

    def __init__(self, config: Optional[FundingArbConfig] = None):
        self.config = config or FundingArbConfig()
        self.positions: list[ArbPosition] = []
        self.trades: list[ArbTrade] = []

    def _normalise_rate_8h(self, rate: float, exchange: str) -> float:
        """Normalise a funding rate to an 8-hour equivalent."""
        h = self.config.funding_hours.get(exchange, 8)
        return rate * (8 / h)

    def _round_trip_fees(self, ex1: str, ex2: str) -> float:
        """Total round-trip fee for entering and exiting both legs."""
        f1 = self.config.exchange_fees.get(ex1, 0.0005)
        f2 = self.config.exchange_fees.get(ex2, 0.0005)
        return 2 * (f1 + f2)

    def on_funding_period(self, period_time: str, rates: pd.DataFrame):
        """
        Process one funding period.

        rates: DataFrame with columns [exchange, coin, funding_rate]
        """
        # Build spread matrix for this period
        pivot = rates.pivot_table(index="coin", columns="exchange", values="funding_rate")

        # Update existing positions
        self._update_positions(period_time, pivot)

        # Look for new entries
        self._scan_entries(period_time, pivot)

    def _update_positions(self, period_time: str, pivot: pd.DataFrame):
        """Update existing positions: accumulate funding and check exits."""
        closed = []
        for pos in self.positions:
            pos.periods_held += 1

            # Get current rates for both exchanges
            long_rate = pivot.loc[pos.coin].get(pos.long_exchange)
            short_rate = pivot.loc[pos.coin].get(pos.short_exchange)

            if pd.isna(long_rate) or pd.isna(short_rate):
                # Data missing — force exit
                closed.append(self._close_position(pos, period_time, 0, "data_missing"))
                continue

            long_rate_8h = self._normalise_rate_8h(long_rate, pos.long_exchange)
            short_rate_8h = self._normalise_rate_8h(short_rate, pos.short_exchange)

            # Funding PnL this period:
            # Long leg: if funding rate > 0, longs pay shorts, so we pay
            # Short leg: if funding rate > 0, we receive as short
            funding_this_period = (short_rate_8h - long_rate_8h) * pos.position_size_usd
            pos.cumulative_funding_pnl += funding_this_period

            current_spread_8h = short_rate_8h - long_rate_8h

            # Check exit conditions
            if current_spread_8h * 10000 < self.config.exit_spread_bps:
                closed.append(self._close_position(pos, period_time, current_spread_8h, "spread_narrowed"))
            elif current_spread_8h < 0:
                closed.append(self._close_position(pos, period_time, current_spread_8h, "spread_reversed"))
            elif pos.periods_held >= self.config.max_holding_periods:
                closed.append(self._close_position(pos, period_time, current_spread_8h, "max_holding"))

        for pos in closed:
            if pos in self.positions:
                self.positions.remove(pos)

    def _close_position(self, pos: ArbPosition, exit_time: str,
                        exit_spread: float, reason: str) -> ArbPosition:
        """Close a position and record the trade."""
        exit_fees = self._round_trip_fees(pos.long_exchange, pos.short_exchange) / 2 * pos.position_size_usd
        total_fees = pos.entry_fees + exit_fees
        net_pnl = pos.cumulative_funding_pnl - total_fees

        trade = ArbTrade(
            coin=pos.coin,
            long_exchange=pos.long_exchange,
            short_exchange=pos.short_exchange,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            entry_spread_8h=pos.entry_spread_8h,
            exit_spread_8h=exit_spread,
            periods_held=pos.periods_held,
            position_size_usd=pos.position_size_usd,
            funding_pnl=pos.cumulative_funding_pnl,
            total_fees=total_fees,
            net_pnl=net_pnl,
            net_pnl_pct=net_pnl / pos.position_size_usd * 100,
            exit_reason=reason,
        )
        self.trades.append(trade)
        return pos

    def _scan_entries(self, period_time: str, pivot: pd.DataFrame):
        """Scan for new arbitrage entry opportunities."""
        exchanges = list(pivot.columns)
        held_coins = {p.coin for p in self.positions}

        for coin in pivot.index:
            if coin in held_coins:
                continue

            best_spread = 0
            best_long = best_short = None

            for i, ex1 in enumerate(exchanges):
                for ex2 in exchanges[i + 1:]:
                    r1 = pivot.loc[coin].get(ex1)
                    r2 = pivot.loc[coin].get(ex2)
                    if pd.isna(r1) or pd.isna(r2):
                        continue

                    r1_8h = self._normalise_rate_8h(r1, ex1)
                    r2_8h = self._normalise_rate_8h(r2, ex2)
                    spread = r2_8h - r1_8h  # positive if ex2 rate > ex1 rate

                    # Consider both directions
                    for s, long_ex, short_ex in [(spread, ex1, ex2), (-spread, ex2, ex1)]:
                        if s > best_spread:
                            rt_cost = self._round_trip_fees(long_ex, short_ex)
                            if s * 10000 > self.config.min_spread_bps + rt_cost * 10000:
                                best_spread = s
                                best_long = long_ex
                                best_short = short_ex

            if best_long and best_short:
                entry_fees = self._round_trip_fees(best_long, best_short) / 2 * self.config.position_size_usd
                pos = ArbPosition(
                    entry_time=period_time,
                    coin=coin,
                    long_exchange=best_long,
                    short_exchange=best_short,
                    entry_spread_8h=best_spread,
                    position_size_usd=self.config.position_size_usd,
                    entry_fees=entry_fees,
                )
                self.positions.append(pos)

    def get_results(self) -> pd.DataFrame:
        """Return all completed trades as a DataFrame."""
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.__dict__ for t in self.trades])

    def print_summary(self):
        """Print strategy performance summary."""
        df = self.get_results()
        if df.empty:
            print("  No trades executed.")
            return

        total_funding = df["funding_pnl"].sum()
        total_fees = df["total_fees"].sum()
        total_net = df["net_pnl"].sum()
        win_rate = (df["net_pnl"] > 0).mean() * 100
        avg_holding = df["periods_held"].mean()

        print("\n" + "=" * 60)
        print("FUNDING RATE ARBITRAGE - PERFORMANCE SUMMARY")
        print("=" * 60)
        print(f"  Total trades:           {len(df)}")
        print(f"  Win rate:               {win_rate:.1f}%")
        print(f"  Total funding PnL:      ${total_funding:.2f}")
        print(f"  Total fees:             ${total_fees:.2f}")
        print(f"  Total net PnL:          ${total_net:.2f}")
        print(f"  Avg net PnL/trade:      ${df['net_pnl'].mean():.2f} ({df['net_pnl_pct'].mean():.3f}%)")
        print(f"  Avg holding periods:    {avg_holding:.1f}")
        print(f"  Position size:          ${self.config.position_size_usd:,.0f} per leg")

        print(f"\n  Exit reasons:")
        for reason, count in df["exit_reason"].value_counts().items():
            print(f"    {reason}: {count} ({count / len(df) * 100:.1f}%)")

        print(f"\n  Top coins by net PnL:")
        top = df.groupby("coin")["net_pnl"].sum().sort_values(ascending=False).head(5)
        for coin, pnl in top.items():
            print(f"    {coin}: ${pnl:.2f}")
