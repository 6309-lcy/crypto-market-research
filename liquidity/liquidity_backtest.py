"""
backtest.py - Orderbook volatility backtest.

Classifies stress directly from orderbook snapshot mid-price changes so the
stress/calm comparison is driven by the available orderbook sample itself.
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent


def load_orderbook() -> pd.DataFrame:
    ob_path = DATA_DIR / "liquidity_orderbook_snapshots.csv"
    ob = pd.read_csv(ob_path)
    ob["timestamp"] = pd.to_datetime(ob["timestamp"], format="ISO8601", utc=True).dt.tz_localize(None)
    okx_mask = ob["exchange"] == "okx"
    ob.loc[okx_mask, "quantity"] = ob.loc[okx_mask, "quantity"] * 0.001
    return ob


def build_snapshot_metrics(ex_ob: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    rows = []
    for ts, snap in ex_ob.groupby("timestamp"):
        bids = snap[snap["side"] == "bid"].sort_values("price", ascending=False)
        asks = snap[snap["side"] == "ask"].sort_values("price", ascending=True)
        if bids.empty or asks.empty:
            continue

        mid = snap["mid_price"].iloc[0]
        if mid <= 0:
            continue

        rows.append(
            {
                "timestamp": ts,
                "mid_price": mid,
                "spread_bps": (asks.iloc[0]["price"] - bids.iloc[0]["price"]) / mid * 10000,
                "depth_5lvl": snap[snap["level"] <= 5]["quantity"].sum() * mid,
            }
        )

    if not rows:
        return pd.DataFrame(), np.nan

    snap_df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    snap_df["mid_return_pct"] = snap_df["mid_price"].pct_change().abs() * 100
    snap_df["rolling_vol_pct"] = (
        snap_df["mid_return_pct"]
        .rolling(window=5, min_periods=2)
        .std()
        .fillna(snap_df["mid_return_pct"])
        .fillna(0)
    )
    vol_threshold = snap_df["rolling_vol_pct"].quantile(0.9) if len(snap_df) > 1 else 0
    snap_df["is_stress"] = snap_df["mid_return_pct"] >= 0.2
    if vol_threshold > 0:
        snap_df["is_stress"] = snap_df["is_stress"] | (snap_df["rolling_vol_pct"] >= vol_threshold)

    if not snap_df["is_stress"].any():
        top_n = max(1, int(np.ceil(len(snap_df) * 0.1)))
        stress_idx = snap_df["rolling_vol_pct"].nlargest(top_n).index
        snap_df["is_stress"] = False
        snap_df.loc[stress_idx, "is_stress"] = True

    return snap_df, float(vol_threshold)


def main():
    print("=" * 60)
    print("LIQUIDITY RESILIENCE BACKTEST")
    print("=" * 60)

    ob = load_orderbook()
    exchanges = sorted(ob["exchange"].unique())
    print(f"Exchanges: {exchanges}")

    # ─── Backtest: Trade execution during stress periods ───────────────
    print("\n" + "=" * 60)
    print("STRESS PERIOD EXECUTION SIMULATION")
    print("=" * 60)

    # For each stress period, compare spread and depth across exchanges
    results = []
    stress_snapshot_total = 0
    for ex in exchanges:
        ex_ob = ob[ob["exchange"] == ex]
        snapshot_metrics, vol_threshold = build_snapshot_metrics(ex_ob)
        stress_df = snapshot_metrics[snapshot_metrics["is_stress"]]
        calm_df = snapshot_metrics[~snapshot_metrics["is_stress"]]
        stress_snapshot_total += len(stress_df)

        stress_avg = stress_df[["spread_bps", "depth_5lvl"]].mean() if not stress_df.empty else pd.Series(dtype=float)
        calm_avg = calm_df[["spread_bps", "depth_5lvl"]].mean() if not calm_df.empty else pd.Series(dtype=float)
        calm_spread = float(calm_avg.get("spread_bps", np.nan))
        stress_spread = float(stress_avg.get("spread_bps", np.nan))
        calm_depth = float(calm_avg.get("depth_5lvl", np.nan))
        stress_depth = float(stress_avg.get("depth_5lvl", np.nan))
        spread_widening = (
            ((stress_spread - calm_spread) / calm_spread) * 100
            if pd.notna(calm_spread) and calm_spread > 0 and pd.notna(stress_spread)
            else np.nan
        )
        depth_decline = (
            ((calm_depth - stress_depth) / calm_depth) * 100
            if pd.notna(calm_depth) and calm_depth > 0 and pd.notna(stress_depth)
            else np.nan
        )

        print(
            f"  [{ex}] orderbook stress snapshots={len(stress_df)}, calm snapshots={len(calm_df)}, "
            f"vol threshold={vol_threshold:.5f}%"
        )

        results.append({
            "exchange": ex,
            "stress_spread_bps": stress_spread,
            "calm_spread_bps": calm_spread,
            "spread_widening_pct": spread_widening,
            "stress_depth_5lvl": stress_depth,
            "calm_depth_5lvl": calm_depth,
            "depth_decline_pct": depth_decline,
            "stress_samples": len(stress_df),
            "calm_samples": len(calm_df),
        })

    results_df = pd.DataFrame(results)
    print("\n  Stress vs Calm Comparison:")
    for _, row in results_df.iterrows():
        depth_label = "decline" if pd.notna(row["depth_decline_pct"]) and row["depth_decline_pct"] >= 0 else "increase"
        print(f"  [{row['exchange']}]")
        print(f"    Spread: calm={row['calm_spread_bps']:.2f}bps -> stress={row['stress_spread_bps']:.2f}bps "
              f"({row['spread_widening_pct']:+.1f}%)")
        print(f"    Depth: calm=${row['calm_depth_5lvl']:,.0f} -> stress=${row['stress_depth_5lvl']:,.0f} "
              f"({depth_label}={abs(row['depth_decline_pct']):.1f}%)")

    # ─── Execution Cost Backtest ───────────────────────────────────────
    print("\n" + "=" * 60)
    print("EXECUTION COST SIMULATION")
    print("=" * 60)

    # Simulate market orders of various sizes using actual orderbook data
    trade_sizes = [10_000, 50_000, 100_000, 500_000]
    exec_results = []

    for ex in exchanges:
        ex_ob = ob[ob["exchange"] == ex]
        for ts in sorted(ex_ob["timestamp"].unique())[:50]:
            snap = ex_ob[ex_ob["timestamp"] == ts]
            asks = snap[snap["side"] == "ask"].sort_values("price")
            mid = snap["mid_price"].iloc[0]
            if mid <= 0 or asks.empty:
                continue

            for size in trade_sizes:
                remaining = size
                cost = 0
                for _, level in asks.iterrows():
                    available = level["quantity"] * level["price"]
                    fill = min(remaining, available)
                    cost += fill * (level["price"] / mid - 1)
                    remaining -= fill
                    if remaining <= 0:
                        break
                slippage_bps = (cost / size) * 10000
                exec_results.append({
                    "exchange": ex, "timestamp": ts,
                    "trade_size": size, "slippage_bps": slippage_bps,
                    "filled_pct": ((size - remaining) / size) * 100,
                })

    exec_df = pd.DataFrame(exec_results)
    print("  Average Slippage by Exchange and Trade Size:")
    pivot = exec_df.pivot_table(index="exchange", columns="trade_size",
                                 values="slippage_bps", aggfunc="mean")
    print(pivot.to_string())

    # ─── Generate Charts ───────────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # Stress vs calm spread comparison
    x = np.arange(len(results_df))
    w = 0.35
    axes[0].bar(x - w / 2, results_df["calm_spread_bps"], w, label="Calm", color="steelblue", alpha=0.8)
    axes[0].bar(x + w / 2, results_df["stress_spread_bps"], w, label="Stress", color="coral", alpha=0.8)
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(results_df["exchange"])
    axes[0].set_ylabel("Spread (bps)")
    axes[0].set_title("Bid-Ask Spread: Calm vs Stress Periods")
    axes[0].legend()

    # Execution cost by size
    for ex in exchanges:
        ex_exec = exec_df[exec_df["exchange"] == ex]
        avg_slip = ex_exec.groupby("trade_size")["slippage_bps"].mean()
        axes[1].plot([f"${s // 1000}k" for s in avg_slip.index], avg_slip.values,
                     marker="o", label=ex, linewidth=2)
    axes[1].set_xlabel("Trade Size")
    axes[1].set_ylabel("Average Slippage (bps)")
    axes[1].set_title("Execution Cost by Trade Size")
    axes[1].legend()

    plt.tight_layout()
    plt.savefig(DATA_DIR / "liquidity_backtest_results.png", dpi=150)
    plt.close()
    print("\n  Chart saved: backtest_results.png")

    # Save to Excel
    path = DATA_DIR / "liquidity_backtest_results.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        results_df.to_excel(writer, sheet_name="Stress vs Calm", index=False)
        pivot.to_excel(writer, sheet_name="Execution Costs")
        exec_df.head(500).to_excel(writer, sheet_name="Raw Execution Data", index=False)

        # Performance summary
        summary = {
            "Total OB snapshots": len(ob["timestamp"].unique()),
            "Exchanges": ", ".join(exchanges),
            "Orderbook stress snapshots": stress_snapshot_total,
            "Trade sizes tested": ", ".join([f"${s // 1000}k" for s in trade_sizes]),
        }
        for _, row in results_df.iterrows():
            summary[f"{row['exchange']} - calm spread (bps)"] = f"{row['calm_spread_bps']:.2f}"
            summary[f"{row['exchange']} - stress spread (bps)"] = f"{row['stress_spread_bps']:.2f}"
            summary[f"{row['exchange']} - spread widening"] = f"{row['spread_widening_pct']:.1f}%"

        pd.DataFrame(list(summary.items()), columns=["Metric", "Value"]).to_excel(
            writer, sheet_name="Performance Summary", index=False)

    print(f"  Excel saved: {path}")

    print("\n" + "=" * 60)
    print("BACKTEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
