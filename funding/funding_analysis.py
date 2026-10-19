"""
analysis.py - Analyses cross-exchange funding rate differentials.

Performs:
  1. Funding rate comparison across exchanges for each coin
  2. Identification of arbitrage opportunities (spread analysis)
  3. Trading fee impact assessment
  4. Liquidity considerations
  5. Annualised return calculations net of costs
"""

import glob
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
import matplotlib.pyplot as plt

DATA_DIR = Path(__file__).resolve().parent

# Trading fees per exchange (taker fees as fraction)
EXCHANGE_FEES = {
    "binance": 0.0004,      # 4 bps
    "bybit": 0.00055,       # 5.5 bps
    "okx": 0.0005,          # 5 bps
    "hyperliquid": 0.00035, # 3.5 bps
}

EXCHANGE_FUNDING_HOURS = {
    "binance": 8,
    "bybit": 8,
    "okx": 8,
    "hyperliquid": 1,
}


def load_funding_data() -> pd.DataFrame:
    """Load funding rate CSV data."""
    csv_path = DATA_DIR / "funding_funding_rates.csv"
    if not csv_path.exists():
        raise FileNotFoundError("No funding_rates.csv found. Run fetch_funding_rates.py first.")
    df = pd.read_csv(csv_path)
    df["fetch_datetime"] = pd.to_datetime(df["fetch_datetime"])
    for column in ["volume_24h_base", "volume_24h_usd"]:
        if column not in df.columns:
            df[column] = np.nan
    return df


def load_historical_data() -> pd.DataFrame:
    """Load historical funding rate data."""
    csv_path = DATA_DIR / "funding_funding_rates_history.csv"
    if not csv_path.exists():
        return pd.DataFrame()
    df = pd.read_csv(csv_path)
    df["datetime"] = pd.to_datetime(df["datetime"], format="ISO8601", utc=True).dt.tz_localize(None)
    return df


def analyze_current_spreads(df: pd.DataFrame) -> pd.DataFrame:
    """Compute funding rate spreads between all exchange pairs for each coin."""
    print("\n" + "=" * 60)
    print("FUNDING RATE SPREAD ANALYSIS")
    print("=" * 60)

    # Use the latest snapshot
    latest = df.sort_values("fetch_timestamp").groupby(["exchange", "coin"]).last().reset_index()

    # Pivot: rows=coins, columns=exchanges, values=funding_rate
    pivot = latest.pivot_table(
        index="coin", columns="exchange", values="funding_rate"
    )

    print(f"\n  Coins with data across exchanges: {len(pivot)}")
    print(f"  Exchanges: {list(pivot.columns)}")

    # Compute pairwise spreads
    exchanges = list(pivot.columns)
    spread_records = []
    for i, ex1 in enumerate(exchanges):
        for ex2 in exchanges[i + 1:]:
            for coin in pivot.index:
                r1 = pivot.loc[coin, ex1] if not pd.isna(pivot.loc[coin].get(ex1)) else None
                r2 = pivot.loc[coin, ex2] if not pd.isna(pivot.loc[coin].get(ex2)) else None
                if r1 is not None and r2 is not None:
                    spread = r1 - r2
                    # Annualise spread
                    h1 = EXCHANGE_FUNDING_HOURS.get(ex1, 8)
                    h2 = EXCHANGE_FUNDING_HOURS.get(ex2, 8)
                    # Normalise to per-8h equivalent for comparison
                    r1_8h = r1 * (8 / h1)
                    r2_8h = r2 * (8 / h2)
                    spread_8h = r1_8h - r2_8h
                    annualised_spread = spread_8h * (365 * 3)  # 3 periods per day at 8h

                    # Cost: round-trip fees on both exchanges
                    fee1 = EXCHANGE_FEES.get(ex1, 0.0005)
                    fee2 = EXCHANGE_FEES.get(ex2, 0.0005)
                    round_trip_cost = 2 * (fee1 + fee2)  # entry + exit on both legs

                    # Net profit per period
                    net_per_period = abs(spread_8h) - round_trip_cost
                    annualised_net = net_per_period * (365 * 3)

                    spread_records.append({
                        "coin": coin,
                        "long_exchange": ex2 if spread_8h > 0 else ex1,
                        "short_exchange": ex1 if spread_8h > 0 else ex2,
                        "spread_8h": spread_8h,
                        "abs_spread_8h": abs(spread_8h),
                        "annualised_spread_pct": annualised_spread * 100,
                        "round_trip_cost": round_trip_cost,
                        "net_per_period_8h": net_per_period,
                        "annualised_net_pct": annualised_net * 100,
                        "profitable": net_per_period > 0,
                    })

    spreads_df = pd.DataFrame(spread_records)

    # Summary
    profitable = spreads_df[spreads_df["profitable"]]
    print(f"\n  Total pair-coin combinations: {len(spreads_df)}")
    print(f"  Profitable opportunities (net of fees): {len(profitable)}")
    if not profitable.empty:
        print(f"  Best opportunity:")
        best = profitable.loc[profitable["annualised_net_pct"].idxmax()]
        print(f"    {best['coin']}: Long {best['long_exchange']}, Short {best['short_exchange']}")
        print(f"    Spread (8h): {best['abs_spread_8h'] * 10000:.2f} bps")
        print(f"    Annualised net: {best['annualised_net_pct']:.1f}%")
        print(f"    Round-trip cost: {best['round_trip_cost'] * 10000:.1f} bps")

    return spreads_df, pivot


def analyze_fee_comparison() -> pd.DataFrame:
    """Create trading fee comparison across exchanges."""
    print("\n" + "=" * 60)
    print("TRADING FEE COMPARISON")
    print("=" * 60)

    fee_data = []
    for ex, fee in EXCHANGE_FEES.items():
        fee_data.append({
            "exchange": ex,
            "taker_fee_bps": fee * 10000,
            "round_trip_bps": fee * 10000 * 2,
            "funding_interval_hours": EXCHANGE_FUNDING_HOURS.get(ex, 8),
        })
    fee_df = pd.DataFrame(fee_data)
    print(fee_df.to_string(index=False))
    return fee_df


def build_liquidity_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Summarise available 24h liquidity metrics from exchange ticker data."""
    latest = df.sort_values("fetch_timestamp").groupby(["exchange", "coin"]).last().reset_index()
    liquidity = latest[["exchange", "coin", "volume_24h_base", "volume_24h_usd", "mark_price"]].copy()
    liquidity["volume_24h_usd"] = pd.to_numeric(liquidity["volume_24h_usd"], errors="coerce")
    liquidity["volume_24h_base"] = pd.to_numeric(liquidity["volume_24h_base"], errors="coerce")
    liquidity["liquidity_bucket"] = pd.cut(
        liquidity["volume_24h_usd"],
        bins=[-np.inf, 1_000_000, 10_000_000, 50_000_000, np.inf],
        labels=["< $1m", "$1m-$10m", "$10m-$50m", "> $50m"],
    )
    return liquidity.sort_values(["volume_24h_usd", "exchange", "coin"], ascending=[False, True, True])


def plot_funding_heatmap(pivot: pd.DataFrame):
    """Plot a heatmap of funding rates across exchanges and coins."""
    # Sort by mean absolute funding rate
    pivot_sorted = pivot.loc[pivot.abs().mean(axis=1).sort_values(ascending=False).index]

    fig, ax = plt.subplots(figsize=(10, max(8, len(pivot_sorted) * 0.35)))
    im = ax.imshow(pivot_sorted.values * 10000, cmap="RdYlGn_r", aspect="auto")
    ax.set_xticks(range(len(pivot_sorted.columns)))
    ax.set_xticklabels(pivot_sorted.columns, rotation=45, ha="right")
    ax.set_yticks(range(len(pivot_sorted.index)))
    ax.set_yticklabels(pivot_sorted.index, fontsize=8)
    ax.set_title("Funding Rates by Coin and Exchange (bps per period)")
    plt.colorbar(im, ax=ax, label="Funding Rate (bps)")
    plt.tight_layout()
    plt.savefig(DATA_DIR / "funding_funding_heatmap.png", dpi=150)
    plt.close()
    print(f"  Heatmap saved: funding_heatmap.png")


def plot_top_opportunities(spreads_df: pd.DataFrame):
    """Plot top arbitrage opportunities."""
    profitable = spreads_df[spreads_df["profitable"]].sort_values("annualised_net_pct", ascending=False)
    if profitable.empty:
        print("  No profitable opportunities to plot.")
        return

    top = profitable.head(20)
    labels = [f"{r['coin']}\n{r['long_exchange'][:3]}↔{r['short_exchange'][:3]}" for _, r in top.iterrows()]

    fig, ax = plt.subplots(figsize=(12, 6))
    x = range(len(top))
    bars = ax.bar(x, top["annualised_net_pct"], color="seagreen", alpha=0.8, edgecolor="white")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Annualised Net Return (%)")
    ax.set_title("Top 20 Funding Rate Arbitrage Opportunities (Net of Fees)")
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(DATA_DIR / "funding_top_opportunities.png", dpi=150)
    plt.close()
    print(f"  Top opportunities chart saved: top_opportunities.png")


def plot_spread_distribution(spreads_df: pd.DataFrame):
    """Plot distribution of funding rate spreads."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    ax = axes[0]
    ax.hist(spreads_df["abs_spread_8h"] * 10000, bins=50, color="steelblue", alpha=0.7, edgecolor="white")
    ax.axvline(x=spreads_df["round_trip_cost"].mean() * 10000, color="red", linestyle="--",
               label=f"Avg round-trip cost ({spreads_df['round_trip_cost'].mean() * 10000:.1f} bps)")
    ax.set_xlabel("Absolute Spread (bps per 8h)")
    ax.set_ylabel("Count")
    ax.set_title("Distribution of Funding Rate Spreads")
    ax.legend()

    ax = axes[1]
    ax.hist(spreads_df["annualised_net_pct"], bins=50, color="coral", alpha=0.7, edgecolor="white")
    ax.axvline(x=0, color="gray", linestyle="-")
    ax.set_xlabel("Annualised Net Return (%)")
    ax.set_ylabel("Count")
    ax.set_title("Distribution of Annualised Net Returns")

    plt.tight_layout()
    plt.savefig(DATA_DIR / "funding_spread_distribution.png", dpi=150)
    plt.close()
    print(f"  Spread distribution chart saved: spread_distribution.png")


def save_to_excel(
    spreads_df: pd.DataFrame,
    pivot: pd.DataFrame,
    fee_df: pd.DataFrame,
    hist_df: pd.DataFrame,
    liquidity_df: pd.DataFrame,
):
    """Export all analysis to Excel workbook with tabs."""
    excel_path = DATA_DIR / "funding_analysis_results.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        # Raw funding rates by exchange
        pivot.to_excel(writer, sheet_name="Funding Rates by Exchange")

        # Trading fee comparison
        fee_df.to_excel(writer, sheet_name="Trading Fee Comparison", index=False)

        # Spread calculations
        spreads_df.sort_values("annualised_net_pct", ascending=False).to_excel(
            writer, sheet_name="Spread Calculations", index=False
        )

        # Profitable opportunities
        profitable = spreads_df[spreads_df["profitable"]].sort_values("annualised_net_pct", ascending=False)
        profitable.to_excel(writer, sheet_name="Profitable Opportunities", index=False)

        # Historical funding rates (sample)
        if not hist_df.empty:
            hist_df.head(2000).to_excel(writer, sheet_name="Historical Rates Sample", index=False)

        liquidity_available = liquidity_df["volume_24h_usd"].notna().sum() if not liquidity_df.empty else 0
        if not liquidity_df.empty:
            liquidity_df.to_excel(writer, sheet_name="Liquidity Summary", index=False)

        # Risk metrics
        risk_data = {
            "Total coin-pair combinations": len(spreads_df),
            "Profitable opportunities": len(profitable),
            "Pct profitable": f"{len(profitable) / len(spreads_df) * 100:.1f}%",
            "Mean abs spread (bps/8h)": f"{spreads_df['abs_spread_8h'].mean() * 10000:.2f}",
            "Max abs spread (bps/8h)": f"{spreads_df['abs_spread_8h'].max() * 10000:.2f}",
            "Mean annualised net (%)": f"{spreads_df['annualised_net_pct'].mean():.1f}",
            "Best annualised net (%)": f"{spreads_df['annualised_net_pct'].max():.1f}",
            "Worst annualised net (%)": f"{spreads_df['annualised_net_pct'].min():.1f}",
            "Liquidity basis": "24h volume/turnover where exchange APIs expose it; otherwise conservative sizing assumptions",
            "Liquidity rows with direct data": int(liquidity_available),
            "Liquidity fallback": "Use $10,000 per leg where direct market-depth data is unavailable",
        }
        pd.DataFrame([risk_data]).T.to_excel(writer, sheet_name="Risk Metrics", header=["Value"])

    print(f"\n  Excel results saved: {excel_path}")


def main():
    print("=" * 60)
    print("CROSS-EXCHANGE FUNDING RATE ANALYSIS")
    print("=" * 60)

    df = load_funding_data()
    hist_df = load_historical_data()
    print(f"Loaded {len(df)} current funding rate records")
    if not hist_df.empty:
        print(f"Loaded {len(hist_df)} historical funding rate records")

    spreads_df, pivot = analyze_current_spreads(df)
    fee_df = analyze_fee_comparison()
    liquidity_df = build_liquidity_summary(df)
    available_liquidity = liquidity_df["volume_24h_usd"].notna().sum() if not liquidity_df.empty else 0
    print(f"Liquidity rows with direct 24h volume data: {available_liquidity}")

    print("\n  Generating charts...")
    plot_funding_heatmap(pivot)
    plot_top_opportunities(spreads_df)
    plot_spread_distribution(spreads_df)

    save_to_excel(spreads_df, pivot, fee_df, hist_df, liquidity_df)

    print("\n" + "=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
