"""
backtest.py - Price Movement Analysis After Ghost/Spoof Events

Measures price movement 1-30s after ghost levels appear to determine
whether they attract fills or fake out traders. Also evaluates the
predictive power of the spoof classifier.
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent


def main():
    print("=" * 60)
    print("SPOOFING PREDICTION BACKTEST")
    print("=" * 60)

    # Load classified events
    analysis_path = DATA_DIR / "ghost_analysis_results.xlsx"
    if not analysis_path.exists():
        print("  No analysis results found. Run analysis.py first.")
        return

    try:
        events = pd.read_excel(analysis_path, sheet_name="Classified Events")
    except Exception:
        print("  Could not load classified events")
        return

    print(f"Loaded {len(events)} classified events")

    # ─── Price Impact Analysis ─────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PRICE IMPACT ANALYSIS")
    print("=" * 60)

    horizons = {
        "1s": "price_move_1s_bps",
        "5s": "price_move_5s_bps",
        "10s": "price_move_10s_bps",
        "30s": "price_move_30s_bps",
    }

    available_horizons = {k: v for k, v in horizons.items() if v in events.columns}

    for cls in ["likely_spoof", "genuine_cancel"]:
        cls_data = events[events["classification"] == cls]
        if cls_data.empty:
            continue
        print(f"\n  [{cls}] (n={len(cls_data)})")
        for label, col in available_horizons.items():
            if col in cls_data.columns:
                mean_move = cls_data[col].mean()
                std_move = cls_data[col].std()
                pct_correct = (cls_data.get("signal_correct", pd.Series([False])).sum() / len(cls_data) * 100)
                print(f"    {label}: mean={mean_move:.2f}bps, std={std_move:.2f}bps, "
                      f"signal_correct={pct_correct:.1f}%")

    # ─── Predictive Power Assessment ──────────────────────────────────
    print("\n" + "=" * 60)
    print("PREDICTIVE POWER ASSESSMENT")
    print("=" * 60)

    # Can we profit from spoofing signals?
    spoofs = events[events["classification"] == "likely_spoof"]
    genuine = events[events["classification"] == "genuine_cancel"]

    if not spoofs.empty and "price_move_10s_bps" in spoofs.columns:
        # Spoofs: trade in expected direction
        spoof_pnl = []
        for _, row in spoofs.iterrows():
            side = row.get("side", "bid")
            move = row.get("price_move_10s_bps", 0)
            # If bid ghost (fake support), we should short
            # If ask ghost (fake resistance), we should long
            if side == "bid":
                pnl = -move  # profit if price goes down
            else:
                pnl = move  # profit if price goes up
            spoof_pnl.append(pnl)

        spoof_pnl = np.array(spoof_pnl)
        win_rate = (spoof_pnl > 0).mean() * 100
        avg_pnl = spoof_pnl.mean()
        sharpe = spoof_pnl.mean() / spoof_pnl.std() if spoof_pnl.std() > 0 else 0

        print(f"  Spoof signal trading (10s horizon):")
        print(f"    Trades: {len(spoof_pnl)}")
        print(f"    Win rate: {win_rate:.1f}%")
        print(f"    Avg PnL: {avg_pnl:.2f} bps")
        print(f"    Sharpe (per-trade): {sharpe:.2f}")
        print(f"    Total PnL: {spoof_pnl.sum():.2f} bps")
    else:
        spoof_pnl = np.array([0])
        win_rate = 0
        avg_pnl = 0
        sharpe = 0

    # ─── Time Clustering Analysis ─────────────────────────────────────
    print("\n" + "=" * 60)
    print("TIME CLUSTERING ANALYSIS")
    print("=" * 60)

    if "appear_time" in events.columns:
        events["appear_time"] = pd.to_datetime(events["appear_time"])
        events["hour"] = events["appear_time"].dt.hour
        span_hours = (
            (events["appear_time"].max() - events["appear_time"].min()).total_seconds() / 3600
            if len(events) > 1
            else 0
        )
        n_hours = events["hour"].nunique()
        if span_hours < 2:
            if n_hours <= 1:
                print("  Note: All events are in a single UTC hour because the order book sample spans only ~5 minutes.")
            else:
                print("  Note: Events span less than 2 UTC hours because the order book sample covers only ~5 minutes.")
            print("  Time clustering across trading hours requires a multi-day order book dataset.")
            print("  Using kline-based proxy: simulate hourly pattern from 10-day klines.")
            klines_path = DATA_DIR / "ghost_klines_1m.csv"
            if klines_path.exists():
                klines = pd.read_csv(klines_path)
                if not klines.empty:
                    klines["open_time"] = pd.to_datetime(klines["open_time"], format="ISO8601", utc=True)
                    klines["hour"] = klines["open_time"].dt.hour
                    if {"open", "close"}.issubset(klines.columns):
                        klines["move_pct"] = (
                            (klines["close"].astype(float) - klines["open"].astype(float)).abs()
                            / klines["open"].astype(float)
                            * 100
                        )
                        vol_by_hour = klines.groupby("hour")["move_pct"].mean()
                        print("  High-vol periods by UTC hour (from 10-day klines):")
                        for h, v in vol_by_hour.sort_values(ascending=False).head(6).items():
                            print(f"    Hour {h:02d}: avg move={v:.4f}%")
        hourly_counts = events.groupby("hour")["classification"].value_counts().unstack(fill_value=0)
        print("  Events by hour (UTC):")
        print(hourly_counts.to_string())
    else:
        print("  Using synthetic data — time clustering based on exchange distribution")
        for ex in events["exchange"].unique():
            ex_data = events[events["exchange"] == ex]
            print(f"    [{ex}] {len(ex_data)} events: "
                  f"{ex_data['classification'].value_counts().to_dict()}")

    # ─── Size Pattern Analysis ────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SIZE PATTERN ANALYSIS")
    print("=" * 60)

    for cls in ["likely_spoof", "genuine_cancel"]:
        cls_data = events[events["classification"] == cls]
        if cls_data.empty:
            continue
        print(f"\n  [{cls}]")
        if "max_notional" in cls_data.columns:
            print(f"    Notional: mean=${cls_data['max_notional'].mean():,.0f}, "
                  f"median=${cls_data['max_notional'].median():,.0f}")
        if "duration_seconds" in cls_data.columns:
            print(f"    Duration: mean={cls_data['duration_seconds'].mean():.1f}s, "
                  f"median={cls_data['duration_seconds'].median():.1f}s")
        if "distance_bps" in cls_data.columns:
            print(f"    Distance: mean={cls_data['distance_bps'].mean():.1f}bps, "
                  f"median={cls_data['distance_bps'].median():.1f}bps")

    # ─── Charts ───────────────────────────────────────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # Price impact comparison
    if available_horizons:
        horizon_labels = list(available_horizons.keys())
        for cls, color in [("likely_spoof", "coral"), ("genuine_cancel", "steelblue")]:
            cls_data = events[events["classification"] == cls]
            if cls_data.empty:
                continue
            means = [abs(cls_data[available_horizons[h]].mean()) for h in horizon_labels if available_horizons[h] in cls_data.columns]
            axes[0][0].plot(horizon_labels[:len(means)], means, "o-", label=cls, color=color, linewidth=2)
        axes[0][0].set_xlabel("Horizon")
        axes[0][0].set_ylabel("Abs Price Move (bps)")
        axes[0][0].set_title("Price Impact by Event Type")
        axes[0][0].legend()

    # PnL distribution
    if len(spoof_pnl) > 1:
        axes[0][1].hist(spoof_pnl, bins=30, color="seagreen", alpha=0.7, edgecolor="white")
        axes[0][1].axvline(x=0, color="red", linestyle="--")
        axes[0][1].set_xlabel("PnL (bps)")
        axes[0][1].set_title(f"Spoof Signal Trading PnL (WR={win_rate:.0f}%)")

    # Event type by exchange
    if "exchange" in events.columns:
        ex_class = events.groupby(["exchange", "classification"]).size().unstack(fill_value=0)
        ex_class.plot(kind="bar", ax=axes[1][0], alpha=0.8)
        axes[1][0].set_title("Events by Exchange and Type")
        axes[1][0].set_ylabel("Count")

    # Spoof score vs price impact
    if "spoof_score" in events.columns and "price_move_10s_bps" in events.columns:
        colors = events["is_likely_spoof"].map({True: "coral", False: "steelblue"})
        axes[1][1].scatter(events["spoof_score"], events["price_move_10s_bps"].abs(),
                           c=colors, alpha=0.5, s=30)
        axes[1][1].set_xlabel("Spoof Score")
        axes[1][1].set_ylabel("Abs Price Move 10s (bps)")
        axes[1][1].set_title("Spoof Score vs Price Impact")

    plt.tight_layout()
    plt.savefig(DATA_DIR / "ghost_backtest_results.png", dpi=150)
    plt.close()
    print("\n  Chart saved: backtest_results.png")

    # Save results
    path = DATA_DIR / "ghost_backtest_results.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        events.to_excel(writer, sheet_name="Events", index=False)

        summary = {
            "Total events": len(events),
            "Likely spoofs": int(events["is_likely_spoof"].sum()),
            "Genuine cancels": int((~events["is_likely_spoof"]).sum()),
            "Spoof signal win rate": f"{win_rate:.1f}%",
            "Avg PnL per spoof signal": f"{avg_pnl:.2f} bps",
            "Sharpe (per-trade)": f"{sharpe:.2f}",
        }
        pd.DataFrame(list(summary.items()), columns=["Metric", "Value"]).to_excel(
            writer, sheet_name="Performance Summary", index=False)

    print(f"  Excel saved: {path}")

    print("\n" + "=" * 60)
    print("BACKTEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
