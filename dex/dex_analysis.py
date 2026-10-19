"""
analysis.py - DEX Liquidity Fragmentation Analysis
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
plt.style.use("seaborn-v0_8-whitegrid")


def load_data():
    pools = pd.read_csv(DATA_DIR / "dex_dex_pools.csv")

    protocol_tvl_path = DATA_DIR / "dex_historical_tvl.csv"
    protocol_tvl = pd.read_csv(protocol_tvl_path) if protocol_tvl_path.exists() else pd.DataFrame()

    pool_history_path = DATA_DIR / "dex_pool_history.csv"
    pool_history = pd.read_csv(pool_history_path) if pool_history_path.exists() else pd.DataFrame()

    # Load hourly pool history if available from The Graph
    hourly_path = DATA_DIR / "dex_pool_history_hourly.csv"
    pool_history_hourly = pd.read_csv(hourly_path) if hourly_path.exists() else pd.DataFrame()

    for frame in [protocol_tvl, pool_history]:
        if not frame.empty and "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"])

    if not pool_history_hourly.empty:
        pool_history_hourly["datetime"] = pd.to_datetime(pool_history_hourly["datetime"])
        print(f"  Hourly pool data available: {len(pool_history_hourly)} rows "
              f"({pool_history_hourly['datetime'].min()} → {pool_history_hourly['datetime'].max()})")
    else:
        print("  Hourly pool data: not available (run fetch_pool_history.py to attempt The Graph query)")

    return pools, protocol_tvl, pool_history, pool_history_hourly


def gini_coefficient(values: np.ndarray) -> float:
    values = np.sort(values[values > 0])
    n = len(values)
    if n <= 1:
        return 0.0
    index = np.arange(1, n + 1)
    return (2 * np.sum(index * values) / (n * np.sum(values))) - (n + 1) / n


def analyze_fragmentation(pools: pd.DataFrame):
    print("\n" + "=" * 60)
    print("LIQUIDITY FRAGMENTATION ANALYSIS")
    print("=" * 60)

    results = []
    for pair in pools["pair"].unique():
        pair_pools = pools[pools["pair"] == pair].copy()
        pair_pools = pair_pools[pair_pools["tvl_usd"] > 0]
        if pair_pools.empty:
            continue

        total_tvl = pair_pools["tvl_usd"].sum()
        gini = gini_coefficient(pair_pools["tvl_usd"].values)
        dex_tvl = pair_pools.groupby("dex")["tvl_usd"].sum().sort_values(ascending=False)
        top_dex = dex_tvl.index[0]
        top_share = dex_tvl.iloc[0] / total_tvl * 100
        shares = pair_pools["tvl_usd"] / total_tvl
        hhi = (shares ** 2).sum() * 10000

        print(f"\n  [{pair}]")
        print(f"    Pools: {len(pair_pools)}")
        print(f"    Total TVL: ${total_tvl:,.0f}")
        print(f"    Gini: {gini:.4f}")
        print(f"    HHI: {hhi:.0f}")
        print(f"    Top DEX: {top_dex} ({top_share:.1f}% share)")
        for dex, tvl in dex_tvl.items():
            print(f"      {dex}: ${tvl:,.0f} ({tvl / total_tvl * 100:.1f}%)")

        results.append({
            "pair": pair,
            "num_pools": len(pair_pools),
            "total_tvl_usd": total_tvl,
            "gini_coefficient": gini,
            "hhi": hhi,
            "top_dex": top_dex,
            "top_dex_share_pct": top_share,
        })

    return pd.DataFrame(results)


def estimate_execution_costs(pools: pd.DataFrame):
    print("\n" + "=" * 60)
    print("EXECUTION COST ESTIMATION")
    print("=" * 60)

    trade_sizes = [10_000, 50_000, 250_000, 1_000_000]
    results = []

    for pair in pools["pair"].unique():
        pair_venues = (
            pools[pools["pair"] == pair]
            .groupby("dex", as_index=False)["tvl_usd"]
            .sum()
            .sort_values("tvl_usd", ascending=False)
        )
        pair_venues = pair_venues[pair_venues["tvl_usd"] > 0]
        if pair_venues.empty:
            continue

        print(f"\n  [{pair}]")
        for size in trade_sizes:
            largest_tvl = pair_venues.iloc[0]["tvl_usd"]
            naive_impact = (size / (2 * largest_tvl)) * 10000 if largest_tvl > 0 else 0.0

            total_tvl = pair_venues["tvl_usd"].sum()
            optimal_impact = 0.0
            for _, venue in pair_venues.iterrows():
                venue_share = venue["tvl_usd"] / total_tvl
                venue_trade = size * venue_share
                venue_impact = (venue_trade / (2 * venue["tvl_usd"])) * venue_trade
                optimal_impact += venue_impact
            optimal_impact_bps = (optimal_impact / size) * 10000 if size > 0 else 0.0

            savings_bps = naive_impact - optimal_impact_bps
            savings_pct = (savings_bps / naive_impact * 100) if naive_impact > 0 else 0.0

            routing_note = ""
            if pair == "WBTC_USDC" and savings_pct < 1:
                routing_note = "Note: WBTC/USDC routing savings are minimal at this pool depth."

            print(
                f"    ${size // 1000}k: naive={naive_impact:.2f}bps, "
                f"optimal={optimal_impact_bps:.2f}bps, savings={savings_bps:.2f}bps ({savings_pct:.1f}%)"
            )
            if routing_note:
                print(f"      {routing_note}")

            results.append({
                "pair": pair,
                "trade_size": size,
                "naive_slippage_bps": naive_impact,
                "optimal_slippage_bps": optimal_impact_bps,
                "savings_bps": savings_bps,
                "savings_pct": savings_pct,
                "routing_note": routing_note,
            })

    return pd.DataFrame(results)


def analyze_tvl_trends(protocol_tvl: pd.DataFrame, pool_history: pd.DataFrame):
    print("\n" + "=" * 60)
    print("HISTORICAL TVL TREND ANALYSIS")
    print("=" * 60)

    protocol_daily = pd.DataFrame()
    if not protocol_tvl.empty:
        protocol_daily = protocol_tvl.copy()
        protocol_daily["date"] = pd.to_datetime(protocol_daily["date"]).dt.normalize()
        protocol_daily = (
            protocol_daily.groupby(["date", "protocol"], as_index=False)["tvl_usd"]
            .mean()
            .sort_values(["protocol", "date"])
        )

    if pool_history.empty:
        print("  No pool_history.csv available for venue leadership analysis")
        return protocol_daily, pd.DataFrame()

    pool_history = pool_history.copy()
    pool_history["date"] = pd.to_datetime(pool_history["date"]).dt.normalize()
    daily_venues = (
        pool_history.groupby(["date", "pair", "dex"], as_index=False)["tvl_usd"]
        .sum()
        .sort_values(["pair", "date", "dex"])
    )
    leadership = daily_venues.loc[daily_venues.groupby(["date", "pair"])["tvl_usd"].idxmax()]
    leadership = leadership.sort_values(["pair", "date"]).reset_index(drop=True)

    days_analysed = int(leadership["date"].nunique())
    print(f"  Days analysed: {days_analysed}")

    for pair, pair_leaders in leadership.groupby("pair"):
        pair_leaders = pair_leaders.sort_values("date").reset_index(drop=True)
        changes = int(pair_leaders["dex"].ne(pair_leaders["dex"].shift()).sum() - 1)
        changes = max(changes, 0)
        leader_counts = pair_leaders["dex"].value_counts()
        current_leader = pair_leaders.iloc[-1]["dex"]
        print(f"  [{pair}] Venue leadership changes: {changes}")
        print(f"    Current leader: {current_leader}")
        for dex, count in leader_counts.items():
            print(f"    {dex}: led on {count} day(s)")

    return protocol_daily, leadership


def analyze_pool_fragmentation_over_time(pool_history: pd.DataFrame):
    print("\n" + "=" * 60)
    print("POOL FRAGMENTATION OVER TIME")
    print("=" * 60)

    if pool_history.empty:
        print("  No pool_history.csv available")
        return pd.DataFrame()

    pool_history = pool_history.copy()
    pool_history["date"] = pd.to_datetime(pool_history["date"]).dt.normalize()
    daily_venues = pool_history.groupby(["date", "pair", "dex"], as_index=False)["tvl_usd"].sum()

    rows = []
    for (pair, date), group in daily_venues.groupby(["pair", "date"]):
        rows.append({
            "pair": pair,
            "date": date,
            "gini": gini_coefficient(group["tvl_usd"].to_numpy(dtype=float)),
        })

    frag_time = pd.DataFrame(rows).sort_values(["pair", "date"]).reset_index(drop=True)
    if frag_time.empty:
        print("  No fragmentation time series produced")
        return frag_time

    fig, ax = plt.subplots(figsize=(12, 5))
    for pair, pair_data in frag_time.groupby("pair"):
        pair_data = pair_data.sort_values("date")
        ax.plot(pair_data["date"], pair_data["gini"], marker="o", linewidth=2, label=pair.replace("_", "/"))

        avg_gini = pair_data["gini"].mean()
        if len(pair_data) > 1:
            slope = np.polyfit(np.arange(len(pair_data)), pair_data["gini"], 1)[0]
        else:
            slope = 0.0
        direction = "upward" if slope > 0.001 else "downward" if slope < -0.001 else "flat"
        print(f"  [{pair}] Average Gini: {avg_gini:.4f}, trend: {direction} ({slope:+.5f}/day)")

    ax.set_xlabel("Date")
    ax.set_ylabel("Gini Coefficient")
    ax.set_title("Liquidity Fragmentation Over Time (30-Day Pool History)")
    ax.legend()
    plt.tight_layout()
    plt.savefig(DATA_DIR / "dex_fragmentation_over_time.png", dpi=150)
    plt.close()
    print("  Saved: fragmentation_over_time.png")

    return frag_time


def simulate_time_of_day(pools: pd.DataFrame):
    print("\n" + "=" * 60)
    print("TIME-OF-DAY FRAGMENTATION (MODELLED)")
    print("=" * 60)
    print("  NOTE: This is a model based on known DeFi session patterns, not measured from real hourly data.")

    multipliers = {
        0: 0.85, 1: 0.82, 2: 0.80, 3: 0.78, 4: 0.80, 5: 0.82, 6: 0.85, 7: 0.90,
        8: 0.95, 9: 1.00, 10: 1.05, 11: 1.08, 12: 1.10, 13: 1.12, 14: 1.15, 15: 1.18,
        16: 1.20, 17: 1.25, 18: 1.22, 19: 1.20, 20: 1.15, 21: 1.10, 22: 1.00, 23: 0.90,
    }

    records = []
    for pair in pools["pair"].unique():
        pair_pools = pools[pools["pair"] == pair]
        if pair_pools.empty:
            continue

        base_tvls = pair_pools.groupby("dex")["tvl_usd"].sum()
        for hour in range(24):
            mult = multipliers[hour]
            for dex, base_tvl in base_tvls.items():
                if "curve" in dex:
                    dex_mult = mult * 0.95 + 0.05
                elif "sushi" in dex:
                    dex_mult = mult * 1.1 - 0.1
                else:
                    dex_mult = mult
                records.append({
                    "pair": pair,
                    "hour_utc": hour,
                    "dex": dex,
                    "tvl_usd": base_tvl * dex_mult,
                    "session": "Asia" if hour < 8 else ("EU" if hour < 16 else "US"),
                })

    tod_df = pd.DataFrame(records)
    if tod_df.empty:
        return tod_df

    for pair in tod_df["pair"].unique():
        pair_data = tod_df[tod_df["pair"] == pair]
        ginis = []
        for hour in range(24):
            hour_data = pair_data[pair_data["hour_utc"] == hour]
            ginis.append(gini_coefficient(hour_data["tvl_usd"].values))
        print(f"  [{pair}] Fragmentation by session:")
        print(f"    Asia (00-08 UTC): Gini={np.mean(ginis[:8]):.4f}")
        print(f"    EU   (08-16 UTC): Gini={np.mean(ginis[8:16]):.4f}")
        print(f"    US   (16-00 UTC): Gini={np.mean(ginis[16:]):.4f}")

    return tod_df


def generate_charts(pools, exec_costs, protocol_daily, tod):
    print("\n  Generating charts...")

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for i, pair in enumerate(pools["pair"].unique()):
        pair_data = pools[pools["pair"] == pair]
        dex_tvl = pair_data.groupby("dex")["tvl_usd"].sum().sort_values(ascending=True)
        colors = {"uniswap_v3": "#FF007A", "curve": "#0038FF", "sushiswap": "#FA52A0"}
        bars = axes[i].barh(
            dex_tvl.index,
            dex_tvl.values / 1e6,
            color=[colors.get(d, "gray") for d in dex_tvl.index],
            alpha=0.8,
        )
        axes[i].set_xlabel("TVL ($M)")
        axes[i].set_title(f"{pair.replace('_', '/')} — TVL by DEX")
        for bar, val in zip(bars, dex_tvl.values):
            axes[i].text(bar.get_width() + 0.1, bar.get_y() + bar.get_height() / 2, f"${val / 1e6:.1f}M", va="center", fontsize=9)
    plt.tight_layout()
    plt.savefig(DATA_DIR / "dex_tvl_by_dex.png", dpi=150)
    plt.close()
    print("  Saved: tvl_by_dex.png")

    if not exec_costs.empty:
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        for i, pair in enumerate(exec_costs["pair"].unique()):
            pair_data = exec_costs[exec_costs["pair"] == pair]
            sizes = [f"${s // 1000}k" for s in pair_data["trade_size"]]
            axes[i].plot(sizes, pair_data["naive_slippage_bps"], "o-", label="Naive (single venue)", color="coral", linewidth=2)
            axes[i].plot(sizes, pair_data["optimal_slippage_bps"], "s-", label="Optimal (multi-DEX)", color="seagreen", linewidth=2)
            axes[i].set_xlabel("Trade Size")
            axes[i].set_ylabel("Slippage (bps)")
            axes[i].set_title(f"{pair.replace('_', '/')} — Routing Comparison")
            axes[i].legend()
        plt.tight_layout()
        plt.savefig(DATA_DIR / "dex_routing_comparison.png", dpi=150)
        plt.close()
        print("  Saved: routing_comparison.png")

    if not protocol_daily.empty:
        fig, ax = plt.subplots(figsize=(12, 5))
        for proto in protocol_daily["protocol"].unique():
            proto_data = protocol_daily[protocol_daily["protocol"] == proto].sort_values("date")
            ax.plot(proto_data["date"], proto_data["tvl_usd"] / 1e9, label=proto, linewidth=1.5, alpha=0.8)
        ax.set_ylabel("TVL ($B)")
        ax.set_title("DEX Protocol TVL on Ethereum (Last 30 Days)")
        ax.legend()
        plt.tight_layout()
        plt.savefig(DATA_DIR / "dex_tvl_trends.png", dpi=150)
        plt.close()
        print("  Saved: tvl_trends.png")

    if not tod.empty:
        fig, ax = plt.subplots(figsize=(12, 5))
        for pair in tod["pair"].unique():
            pair_data = tod[tod["pair"] == pair]
            ginis = []
            for hour in range(24):
                hour_data = pair_data[pair_data["hour_utc"] == hour]
                ginis.append(gini_coefficient(hour_data["tvl_usd"].values))
            ax.plot(range(24), ginis, marker="o", label=pair.replace("_", "/"), linewidth=2)
        ax.set_xlabel("Hour (UTC)")
        ax.set_ylabel("Gini Coefficient")
        ax.set_title("Liquidity Fragmentation by Time of Day (Modelled)")
        ax.axvspan(0, 8, alpha=0.1, color="red", label="Asia")
        ax.axvspan(8, 16, alpha=0.1, color="blue", label="EU")
        ax.axvspan(16, 24, alpha=0.1, color="green", label="US")
        ax.legend()
        plt.tight_layout()
        plt.savefig(DATA_DIR / "dex_tod_fragmentation.png", dpi=150)
        plt.close()
        print("  Saved: tod_fragmentation.png")


def save_to_excel(pools, frag, exec_costs, protocol_daily, pool_history, leadership, frag_time, tod):
    path = DATA_DIR / "dex_analysis_results.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pools.to_excel(writer, sheet_name="Pool Data", index=False)
        frag.to_excel(writer, sheet_name="Fragmentation Metrics", index=False)
        exec_costs.to_excel(writer, sheet_name="Execution Costs", index=False)
        if not protocol_daily.empty:
            protocol_daily.to_excel(writer, sheet_name="TVL Trends", index=False)
        if not pool_history.empty:
            pool_history.to_excel(writer, sheet_name="Pool History", index=False)
        if not leadership.empty:
            leadership.to_excel(writer, sheet_name="Venue Leadership", index=False)
        if not frag_time.empty:
            frag_time.to_excel(writer, sheet_name="Fragmentation Time", index=False)
        if not tod.empty:
            tod.to_excel(writer, sheet_name="Time-of-Day", index=False)
    print(f"\n  Excel saved: {path}")


def main():
    print("=" * 60)
    print("DEX LIQUIDITY FRAGMENTATION ANALYSIS")
    print("=" * 60)

    pools, protocol_tvl, pool_history, pool_history_hourly = load_data()
    print(
        f"Loaded {len(pools)} pool records, "
        f"{len(protocol_tvl)} protocol TVL records, {len(pool_history)} pool-history records"
    )
    if not pool_history_hourly.empty:
        print(f"  + {len(pool_history_hourly)} hourly pool records (from The Graph)")

    frag = analyze_fragmentation(pools)
    exec_costs = estimate_execution_costs(pools)
    protocol_daily, leadership = analyze_tvl_trends(protocol_tvl, pool_history)
    frag_time = analyze_pool_fragmentation_over_time(pool_history)
    tod = simulate_time_of_day(pools)
    generate_charts(pools, exec_costs, protocol_daily, tod)
    save_to_excel(pools, frag, exec_costs, protocol_daily, pool_history, leadership, frag_time, tod)

    print("\n" + "=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
