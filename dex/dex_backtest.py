"""
backtest.py - Naive vs Optimal Routing Backtest
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent


def gini_coefficient(values):
    values = np.sort(values[values > 0])
    n = len(values)
    if n <= 1:
        return 0.0
    index = np.arange(1, n + 1)
    return (2 * np.sum(index * values) / (n * np.sum(values))) - (n + 1) / n


def load_daily_venues(pools: pd.DataFrame):
    pool_history_path = DATA_DIR / "dex_pool_history.csv"
    if pool_history_path.exists():
        pool_history = pd.read_csv(pool_history_path)
        if not pool_history.empty:
            pool_history["date"] = pd.to_datetime(pool_history["date"]).dt.normalize()
            daily_venues = (
                pool_history.groupby(["date", "pair", "dex"], as_index=False)["tvl_usd"]
                .sum()
                .sort_values(["pair", "date", "dex"])
            )
            print(f"  Using pool_history.csv for backtest ({daily_venues['date'].nunique()} days)")
            return daily_venues, False

    print("  No pool_history.csv available — using current snapshot for modelled 30-day simulation")
    np.random.seed(42)
    records = []
    for pair in pools["pair"].unique():
        pair_venues = pools[pools["pair"] == pair].groupby("dex")["tvl_usd"].sum()
        for day in range(30):
            date = pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=29 - day)
            shocks = np.random.normal(0, 0.05, size=len(pair_venues))
            daily_tvls = (pair_venues * (1 + shocks)).clip(lower=0)
            for dex, tvl in daily_tvls.items():
                records.append({"date": date, "pair": pair, "dex": dex, "tvl_usd": tvl})
    return pd.DataFrame(records), True


def summarize_pair_results(pair_data: pd.DataFrame, trade_sizes):
    pivot = pair_data.pivot_table(
        index="trade_size",
        values=["naive_bps", "optimal_bps", "smart_bps"],
        aggfunc="mean",
    )
    print(pivot.to_string())
    for size in trade_sizes:
        size_data = pair_data[pair_data["trade_size"] == size]
        naive_avg = size_data["naive_bps"].mean()
        optimal_avg = size_data["optimal_bps"].mean()
        savings = naive_avg - optimal_avg
        pct = (savings / naive_avg * 100) if naive_avg > 0 else 0
        print(f"    ${size // 1000}k: optimal saves {savings:.2f}bps ({pct:.1f}%)")
        if pair_data["pair"].iloc[0] == "WBTC_USDC" and pct < 1:
            print("      Note: WBTC/USDC routing savings are minimal at this pool depth.")


def main():
    print("=" * 60)
    print("DEX ROUTING BACKTEST")
    print("=" * 60)

    pools = pd.read_csv(DATA_DIR / "dex_dex_pools.csv")
    trade_sizes = [10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000]
    pairs = pools["pair"].unique()

    daily_venues, is_modelled = load_daily_venues(pools)

    print("\n" + "=" * 60)
    print("30-DAY ROUTING SIMULATION")
    print("=" * 60)
    if is_modelled:
        print("  NOTE: Backtest inputs are modelled from the current snapshot because measured pool history was unavailable.")

    results = []
    for pair in pairs:
        pair_daily = daily_venues[daily_venues["pair"] == pair].copy()
        if pair_daily.empty:
            continue

        for date, day_frame in pair_daily.groupby("date"):
            daily_tvls = day_frame.set_index("dex")["tvl_usd"].sort_values(ascending=False)
            total_tvl = daily_tvls.sum()
            if total_tvl <= 0:
                continue

            gini = gini_coefficient(daily_tvls.values)

            for size in trade_sizes:
                best_tvl = daily_tvls.iloc[0]
                naive_impact = (size / (2 * best_tvl)) * 10000 if best_tvl > 0 else 0

                optimal_impact = 0.0
                for dex_tvl in daily_tvls.values:
                    if dex_tvl <= 0:
                        continue
                    share = dex_tvl / total_tvl
                    dex_trade = size * share
                    optimal_impact += (dex_trade / (2 * dex_tvl)) * dex_trade
                optimal_impact_bps = (optimal_impact / size) * 10000 if size > 0 else 0

                smart_impact = 0.0
                remaining = size
                for dex_tvl in daily_tvls.values:
                    if dex_tvl <= 0 or remaining <= 0:
                        continue
                    max_trade = dex_tvl * 0.02
                    dex_trade = min(remaining, max_trade)
                    impact = (dex_trade / (2 * dex_tvl)) * 10000
                    smart_impact += impact * dex_trade / size
                    remaining -= dex_trade
                if remaining > 0:
                    smart_impact += 50 * remaining / size

                results.append({
                    "pair": pair,
                    "date": pd.to_datetime(date),
                    "trade_size": size,
                    "naive_bps": naive_impact,
                    "optimal_bps": optimal_impact_bps,
                    "smart_bps": smart_impact,
                    "gini": gini,
                    "total_tvl": total_tvl,
                })

    results_df = pd.DataFrame(results).sort_values(["pair", "date", "trade_size"]).reset_index(drop=True)

    print("\nAverage Execution Cost (bps):")
    for pair in pairs:
        pair_data = results_df[results_df["pair"] == pair]
        if pair_data.empty:
            continue
        print(f"\n  [{pair}]")
        summarize_pair_results(pair_data, trade_sizes)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for i, pair in enumerate(pairs[:2]):
        pair_data = results_df[results_df["pair"] == pair]
        if pair_data.empty:
            continue
        pivot = pair_data.pivot_table(
            index="trade_size",
            values=["naive_bps", "optimal_bps", "smart_bps"],
            aggfunc="mean",
        )
        sizes = [f"${s // 1000}k" for s in pivot.index]
        axes[i].plot(sizes, pivot["naive_bps"], "o-", label="Naive", color="coral", linewidth=2)
        axes[i].plot(sizes, pivot["optimal_bps"], "s-", label="Optimal Split", color="seagreen", linewidth=2)
        axes[i].plot(sizes, pivot["smart_bps"], "^-", label="Smart Route", color="steelblue", linewidth=2)
        axes[i].set_xlabel("Trade Size")
        axes[i].set_ylabel("Slippage (bps)")
        axes[i].set_title(f"{pair.replace('_', '/')} — Routing Strategy Comparison")
        axes[i].legend()
    plt.tight_layout()
    plt.savefig(DATA_DIR / "dex_backtest_results.png", dpi=150)
    plt.close()
    print("\n  Chart saved: backtest_results.png")

    fig, ax = plt.subplots(figsize=(12, 5))
    for pair in pairs:
        pair_data = results_df[results_df["pair"] == pair]
        if pair_data.empty:
            continue
        daily_gini = pair_data.groupby("date")["gini"].mean().sort_index()
        ax.plot(daily_gini.index, daily_gini.values, label=pair.replace("_", "/"), linewidth=2, alpha=0.8)
    ax.set_xlabel("Date")
    ax.set_ylabel("Gini Coefficient")
    ax.set_title("Backtest Fragmentation Over Time")
    ax.legend()
    plt.tight_layout()
    plt.savefig(DATA_DIR / "dex_backtest_fragmentation_over_time.png", dpi=150)
    plt.close()
    print("  Chart saved: backtest_fragmentation_over_time.png")

    path = DATA_DIR / "dex_backtest_results.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for pair in pairs:
            pair_data = results_df[results_df["pair"] == pair]
            if pair_data.empty:
                continue
            pivot = pair_data.pivot_table(
                index="trade_size",
                values=["naive_bps", "optimal_bps", "smart_bps"],
                aggfunc="mean",
            )
            pivot.to_excel(writer, sheet_name=f"{pair} Summary")

        results_df.to_excel(writer, sheet_name="Raw Results", index=False)

        metrics = {}
        for pair in pairs:
            pair_data = results_df[results_df["pair"] == pair]
            if pair_data.empty:
                continue
            for size in [50_000, 250_000]:
                sd = pair_data[pair_data["trade_size"] == size]
                metrics[f"{pair} ${size // 1000}k naive (bps)"] = f"{sd['naive_bps'].mean():.2f}"
                metrics[f"{pair} ${size // 1000}k optimal (bps)"] = f"{sd['optimal_bps'].mean():.2f}"
                metrics[f"{pair} ${size // 1000}k savings (bps)"] = f"{sd['naive_bps'].mean() - sd['optimal_bps'].mean():.2f}"
        pd.DataFrame(list(metrics.items()), columns=["Metric", "Value"]).to_excel(
            writer,
            sheet_name="Performance Metrics",
            index=False,
        )

    print(f"  Excel saved: {path}")

    print("\n" + "=" * 60)
    print("BACKTEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
