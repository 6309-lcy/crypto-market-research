"""
backtest.py - Backtests the funding rate arbitrage strategy on historical data.

Uses historical funding rates from Binance and simulates Bybit, OKX, and
Hyperliquid funding histories from current cross-exchange offsets. The
simulation is deterministic so workbook metrics and report figures can be
reproduced across reruns. Results remain illustrative because only Binance
historical funding is sourced directly from exchange history.
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime, timezone

from funding_strategy import FundingRateArbitrageStrategy, FundingArbConfig
from funding_analysis import load_funding_data, load_historical_data, EXCHANGE_FEES

DATA_DIR = Path(__file__).resolve().parent
BACKTEST_RANDOM_SEED = 42


def prepare_backtest_data(
    hist_df: pd.DataFrame,
    current_df: pd.DataFrame,
    rng: np.random.Generator,
) -> list[tuple]:
    """
    Prepare funding rate data for backtesting.

    Uses Binance historical data as the base. Simulates Bybit, OKX, and Hyperliquid
    rates by adding realistic offsets derived from the current cross-exchange snapshot.
    The fixed-seed random generator keeps the simulation reproducible.
    """
    if hist_df.empty:
        print("  No historical data available. Using current snapshot for single-period test.")
        return prepare_single_period(current_df)

    # Get current cross-exchange offsets to use as simulation basis
    offsets = compute_exchange_offsets(current_df)

    # Build per-period rate tables from Binance historical data
    hist_bn = hist_df[hist_df["exchange"] == "binance"].copy()
    hist_bn["period"] = pd.to_datetime(hist_bn["datetime"]).dt.floor("8h")

    periods = []
    for period_time, group in hist_bn.groupby("period"):
        rows = []
        for _, row in group.iterrows():
            coin = row["coin"]
            bn_rate = row["funding_rate"]

            # Binance actual rate
            rows.append({"exchange": "binance", "coin": coin, "funding_rate": bn_rate})

            # Simulate other exchanges using current offsets + noise
            for ex in ["bybit", "okx", "hyperliquid"]:
                offset = offsets.get((coin, ex), 0)
                noise = rng.normal(0, abs(bn_rate) * 0.1 + 1e-6)
                sim_rate = bn_rate + offset + noise
                rows.append({"exchange": ex, "coin": coin, "funding_rate": sim_rate})

        period_df = pd.DataFrame(rows)
        periods.append((str(period_time), period_df))

    print(f"  Prepared {len(periods)} funding periods for backtest")
    return periods


def compute_exchange_offsets(current_df: pd.DataFrame) -> dict:
    """Compute funding rate offsets between Binance and other exchanges."""
    offsets = {}
    bn_rates = current_df[current_df["exchange"] == "binance"].set_index("coin")["funding_rate"]

    for ex in ["bybit", "okx", "hyperliquid"]:
        ex_rates = current_df[current_df["exchange"] == ex].set_index("coin")["funding_rate"]
        # Normalise Hyperliquid 1h rate to 8h equivalent
        if ex == "hyperliquid":
            ex_rates = ex_rates * 8

        for coin in bn_rates.index:
            if coin in ex_rates.index:
                offsets[(coin, ex)] = ex_rates.loc[coin] - bn_rates.loc[coin]
    return offsets


def prepare_single_period(current_df: pd.DataFrame) -> list[tuple]:
    """Create a single-period test from current data."""
    rates = current_df[["exchange", "coin", "funding_rate"]].copy()
    return [("current", rates)]


def run_backtest(seed: int = BACKTEST_RANDOM_SEED):
    """Run the full backtest."""
    print("=" * 60)
    print("FUNDING RATE ARBITRAGE BACKTEST")
    print("=" * 60)
    print("  Historical basis: Binance actual funding history")
    print("  Simulated basis: Bybit/OKX/Hyperliquid derived from snapshot offsets")
    print(f"  Random seed: {seed}")

    current_df = load_funding_data()
    hist_df = load_historical_data()

    print(f"\n  Current rates: {len(current_df)} records")
    print(f"  Historical rates: {len(hist_df)} records")

    rng = np.random.default_rng(seed)
    periods = prepare_backtest_data(hist_df, current_df, rng)

    # Run strategy
    config = FundingArbConfig(
        min_spread_bps=2.0,
        max_holding_periods=6,
        position_size_usd=10000,
    )
    strategy = FundingRateArbitrageStrategy(config)

    for period_time, rates_df in periods:
        strategy.on_funding_period(period_time, rates_df)

    # Force-close any remaining positions
    if strategy.positions:
        last_time = periods[-1][0] if periods else "end"
        last_rates = periods[-1][1] if periods else pd.DataFrame()
        for pos in list(strategy.positions):
            strategy._close_position(pos, last_time, 0, "backtest_end")
        strategy.positions.clear()

    strategy.print_summary()
    results = strategy.get_results()

    if not results.empty:
        plot_results(results)
        save_results(results)

    return results


def plot_results(results: pd.DataFrame):
    """Generate backtest visualisation charts."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # Cumulative PnL
    ax = axes[0, 0]
    results["cum_net_pnl"] = results["net_pnl"].cumsum()
    results["cum_funding_pnl"] = results["funding_pnl"].cumsum()
    ax.plot(results["cum_funding_pnl"].values, label="Gross (funding only)", alpha=0.8)
    ax.plot(results["cum_net_pnl"].values, label="Net (after fees)", alpha=0.8)
    ax.set_title("Cumulative PnL")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("PnL ($)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # PnL distribution
    ax = axes[0, 1]
    ax.hist(results["net_pnl_pct"], bins=30, color="steelblue", alpha=0.7, edgecolor="white")
    ax.axvline(x=0, color="red", linestyle="--")
    ax.set_title("Trade PnL Distribution")
    ax.set_xlabel("Net PnL (%)")
    ax.set_ylabel("Count")

    # PnL by coin
    ax = axes[1, 0]
    coin_pnl = results.groupby("coin")["net_pnl"].sum().sort_values(ascending=True)
    colors = ["green" if x > 0 else "red" for x in coin_pnl.values]
    ax.barh(coin_pnl.index, coin_pnl.values, color=colors, alpha=0.7)
    ax.set_title("Net PnL by Coin")
    ax.set_xlabel("Net PnL ($)")
    ax.axvline(x=0, color="gray", linestyle="-")

    # Exit reasons
    ax = axes[1, 1]
    exit_counts = results["exit_reason"].value_counts()
    ax.pie(exit_counts.values, labels=exit_counts.index, autopct="%1.1f%%")
    ax.set_title("Exit Reasons")

    plt.tight_layout()
    plt.savefig(DATA_DIR / "funding_backtest_results.png", dpi=150)
    plt.close()
    print(f"  Charts saved: backtest_results.png")


def save_results(results: pd.DataFrame):
    """Save backtest results to Excel."""
    excel_path = DATA_DIR / "funding_backtest_results.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        results.to_excel(writer, sheet_name="Trade Log", index=False)

        # Performance metrics
        total_net = results["net_pnl"].sum()
        total_fees = results["total_fees"].sum()
        total_gross = results["funding_pnl"].sum()

        metrics = {
            "Total Trades": len(results),
            "Win Rate (%)": f"{(results['net_pnl'] > 0).mean() * 100:.1f}",
            "Total Gross PnL ($)": f"{total_gross:.2f}",
            "Total Fees ($)": f"{total_fees:.2f}",
            "Total Net PnL ($)": f"{total_net:.2f}",
            "Avg Net PnL/Trade ($)": f"{results['net_pnl'].mean():.2f}",
            "Avg Net PnL/Trade (%)": f"{results['net_pnl_pct'].mean():.4f}",
            "Max Drawdown ($)": f"{(results['net_pnl'].cumsum().cummax() - results['net_pnl'].cumsum()).max():.2f}",
            "Avg Holding Periods": f"{results['periods_held'].mean():.1f}",
            "Unique Coins Traded": results["coin"].nunique(),
            "Position Size (USD/leg)": f"${results['position_size_usd'].iloc[0]:,.0f}",
            "Historical Data Basis": "Binance actual history; other exchanges simulated from snapshot offsets",
            "Random Seed": BACKTEST_RANDOM_SEED,
        }
        pd.DataFrame([metrics]).T.to_excel(writer, sheet_name="Performance Metrics", header=["Value"])

        # P&L by coin
        coin_summary = results.groupby("coin").agg(
            trades=("net_pnl", "count"),
            gross_pnl=("funding_pnl", "sum"),
            fees=("total_fees", "sum"),
            net_pnl=("net_pnl", "sum"),
            avg_holding=("periods_held", "mean"),
        ).sort_values("net_pnl", ascending=False)
        coin_summary.to_excel(writer, sheet_name="PnL by Coin")

        notes = pd.DataFrame(
            [
                {
                    "item": "Methodology note",
                    "detail": "Only Binance funding history is real. Bybit, OKX, and Hyperliquid histories are simulated from current cross-exchange offsets plus fixed-seed noise.",
                },
                {
                    "item": "Interpretation note",
                    "detail": "Use the backtest to compare strategy behaviour under consistent assumptions, not as a prediction of live future returns.",
                },
            ]
        )
        notes.to_excel(writer, sheet_name="Backtest Notes", index=False)

    print(f"  Excel results saved: {excel_path}")


def main():
    run_backtest()
    print("\n" + "=" * 60)
    print("BACKTEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
