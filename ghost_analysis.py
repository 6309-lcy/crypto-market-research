"""
analysis.py - Ghost Level Detection & Spoofing Classification

1. Identifies large resting orders (> threshold × median size)
2. Tracks which large orders disappear before being filled ("ghost levels")
3. Classifies potential spoofing behaviour
4. Clusters spoof events by time, size, and price patterns
5. Trains a simple classifier to flag likely spoof events
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).resolve().parent
plt.style.use("seaborn-v0_8-whitegrid")

# Thresholds
LARGE_ORDER_MULTIPLIER = 3.0  # order is "large" if > 3× median size at that level
GHOST_MAX_DURATION = 30  # seconds — large order that disappears within 30s
PRICE_APPROACH_BPS = 20  # ghost only counts if price was within 20bps


def load_data():
    ob = pd.read_csv(DATA_DIR / "ghost_orderbook_snapshots.csv")
    ob["timestamp"] = pd.to_datetime(ob["timestamp"], format="ISO8601", utc=True).dt.tz_localize(None)

    # Merge WebSocket-collected snapshots if the collector has been running
    ws_path = DATA_DIR / "ghost_ws_orderbook_snapshots.csv"
    if ws_path.exists():
        ws_ob = pd.read_csv(ws_path)
        ws_ob["timestamp"] = pd.to_datetime(
            ws_ob["timestamp"], format="ISO8601", utc=True
        ).dt.tz_localize(None)
        common_cols = [c for c in ob.columns if c in ws_ob.columns]
        ob = pd.concat([ob[common_cols], ws_ob[common_cols]], ignore_index=True)
        ob = ob.drop_duplicates(subset=["timestamp", "exchange", "side", "level"])

    # Report data span — critical for understanding depth-history coverage
    if not ob.empty:
        span_seconds = (ob["timestamp"].max() - ob["timestamp"].min()).total_seconds()
        span_days = span_seconds / 86400
        print(f"  Order book data span: {span_days:.3f} days "
              f"({ob['timestamp'].min()} to {ob['timestamp'].max()})")
        if span_days < 7:
            print(f"  WARNING: Target is 7-10 days; current span is {span_days:.2f} days. "
                  f"Start ws_collector.py and let it accumulate.")
        else:
            print(f"  OK: Multi-day depth history available ({span_days:.1f} days).")

    klines_path = DATA_DIR / "ghost_klines_1m.csv"
    klines = pd.read_csv(klines_path) if klines_path.exists() else pd.DataFrame()
    if not klines.empty:
        klines["open_time"] = pd.to_datetime(klines["open_time"], format="ISO8601", utc=True).dt.tz_localize(None)
    return ob, klines


def detect_large_orders(ob: pd.DataFrame) -> pd.DataFrame:
    """Identify unusually large orders at each snapshot."""
    print("\n" + "=" * 60)
    print("LARGE ORDER DETECTION")
    print("=" * 60)

    records = []
    for ex in ob["exchange"].unique():
        ex_ob = ob[ob["exchange"] == ex]
        # Compute median order size per level
        median_sizes = ex_ob.groupby(["side", "level"])["quantity"].median()

        for (ts, side), group in ex_ob.groupby(["timestamp", "side"]):
            mid = group["mid_price"].iloc[0]
            for _, row in group.iterrows():
                key = (row["side"], row["level"])
                med = median_sizes.get(key, row["quantity"])
                if med > 0 and row["quantity"] >= LARGE_ORDER_MULTIPLIER * med:
                    records.append({
                        "timestamp": ts,
                        "exchange": ex,
                        "side": row["side"],
                        "level": row["level"],
                        "price": row["price"],
                        "quantity": row["quantity"],
                        "notional_usd": row["notional_usd"],
                        "mid_price": mid,
                        "size_ratio": row["quantity"] / med,
                        "distance_bps": abs(row["price"] - mid) / mid * 10000,
                    })

    large_df = pd.DataFrame(records)
    print(f"  Large orders detected: {len(large_df)}")
    if not large_df.empty:
        for ex in large_df["exchange"].unique():
            ex_data = large_df[large_df["exchange"] == ex]
            print(f"    [{ex}] {len(ex_data)} large orders, "
                  f"avg size ratio: {ex_data['size_ratio'].mean():.1f}×, "
                  f"avg notional: ${ex_data['notional_usd'].mean():,.0f}")

    return large_df


def detect_ghost_levels(ob: pd.DataFrame, large_orders: pd.DataFrame) -> pd.DataFrame:
    """
    Ghost level = a large order that disappears within GHOST_MAX_DURATION seconds
    and was within PRICE_APPROACH_BPS of mid price.
    """
    print("\n" + "=" * 60)
    print("GHOST LEVEL DETECTION")
    print("=" * 60)

    if large_orders.empty:
        print("  No large orders to check for ghosts")
        return pd.DataFrame()

    ghosts = []
    timestamps = sorted(ob["timestamp"].unique())

    for ex in large_orders["exchange"].unique():
        ex_large = large_orders[large_orders["exchange"] == ex]
        ex_ob = ob[ob["exchange"] == ex]

        # Group large orders by price level
        seen_orders = {}  # (price, side) -> first_seen_ts

        for ts in timestamps:
            ts_large = ex_large[ex_large["timestamp"] == ts]
            current_prices = set()

            for _, row in ts_large.iterrows():
                key = (round(row["price"], 2), row["side"])
                current_prices.add(key)

                if key not in seen_orders:
                    seen_orders[key] = {
                        "first_seen": ts,
                        "exchange": ex,
                        "side": row["side"],
                        "price": row["price"],
                        "max_quantity": row["quantity"],
                        "max_notional": row["notional_usd"],
                        "mid_at_appear": row["mid_price"],
                        "distance_bps": row["distance_bps"],
                    }
                else:
                    seen_orders[key]["max_quantity"] = max(
                        seen_orders[key]["max_quantity"], row["quantity"])
                    seen_orders[key]["max_notional"] = max(
                        seen_orders[key]["max_notional"], row["notional_usd"])

            # Check for orders that disappeared
            disappeared = set(seen_orders.keys()) - current_prices
            for key in disappeared:
                info = seen_orders.pop(key)
                duration = (ts - info["first_seen"]).total_seconds()

                if duration <= GHOST_MAX_DURATION and info["distance_bps"] <= PRICE_APPROACH_BPS:
                    # Get mid price when order disappeared
                    ts_ob = ex_ob[ex_ob["timestamp"] == ts]
                    mid_at_disappear = ts_ob["mid_price"].iloc[0] if not ts_ob.empty else info["mid_at_appear"]

                    # Check price direction after disappearance
                    future_ts = [t for t in timestamps if t > ts][:30]
                    price_after = []
                    for ft in future_ts:
                        ft_ob = ex_ob[ex_ob["timestamp"] == ft]
                        if not ft_ob.empty:
                            price_after.append(ft_ob["mid_price"].iloc[0])

                    price_move_1s = (price_after[0] / mid_at_disappear - 1) * 10000 if len(price_after) > 0 else 0
                    price_move_5s = (price_after[4] / mid_at_disappear - 1) * 10000 if len(price_after) > 4 else 0
                    price_move_10s = (price_after[9] / mid_at_disappear - 1) * 10000 if len(price_after) > 9 else 0
                    price_move_30s = (price_after[29] / mid_at_disappear - 1) * 10000 if len(price_after) > 29 else 0

                    # Classify direction relative to ghost side
                    # If a large bid ghost disappears, bearish signal (was fake support)
                    # If a large ask ghost disappears, bullish signal (was fake resistance)
                    if info["side"] == "bid":
                        expected_move = "down"  # fake support pulled
                        signal_correct = price_move_10s < 0
                    else:
                        expected_move = "up"  # fake resistance pulled
                        signal_correct = price_move_10s > 0

                    ghosts.append({
                        "exchange": ex,
                        "side": info["side"],
                        "price": info["price"],
                        "max_quantity": info["max_quantity"],
                        "max_notional": info["max_notional"],
                        "appear_time": info["first_seen"],
                        "disappear_time": ts,
                        "duration_seconds": duration,
                        "distance_bps": info["distance_bps"],
                        "mid_at_appear": info["mid_at_appear"],
                        "mid_at_disappear": mid_at_disappear,
                        "price_move_1s_bps": price_move_1s,
                        "price_move_5s_bps": price_move_5s,
                        "price_move_10s_bps": price_move_10s,
                        "price_move_30s_bps": price_move_30s,
                        "expected_direction": expected_move,
                        "signal_correct": signal_correct,
                    })

    ghost_df = pd.DataFrame(ghosts)
    print(f"  Ghost levels detected: {len(ghost_df)}")
    if not ghost_df.empty:
        for ex in ghost_df["exchange"].unique():
            ex_g = ghost_df[ghost_df["exchange"] == ex]
            print(f"    [{ex}] {len(ex_g)} ghosts, "
                  f"avg duration: {ex_g['duration_seconds'].mean():.1f}s, "
                  f"avg notional: ${ex_g['max_notional'].mean():,.0f}")
            if len(ex_g) > 0:
                correct = ex_g["signal_correct"].sum()
                print(f"    Signal accuracy: {correct}/{len(ex_g)} = {correct/len(ex_g)*100:.1f}%")

    return ghost_df


def classify_spoofing(ghosts: pd.DataFrame) -> pd.DataFrame:
    """Classify ghost events as likely spoofing or genuine order cancellation."""
    print("\n" + "=" * 60)
    print("SPOOFING CLASSIFICATION")
    print("=" * 60)

    if ghosts.empty:
        print("  No ghost events to classify")
        # Generate synthetic labeled data for the classifier demo
        return _generate_synthetic_events()

    ghosts = ghosts.copy()

    # Rule-based scoring
    ghosts["spoof_score"] = 0.0

    # Factor 1: Short duration = more suspicious
    ghosts["spoof_score"] += (1 - ghosts["duration_seconds"] / GHOST_MAX_DURATION).clip(0, 1) * 25

    # Factor 2: Large size = more suspicious
    median_notional = ghosts["max_notional"].median()
    if median_notional > 0:
        ghosts["spoof_score"] += (ghosts["max_notional"] / median_notional).clip(0, 5) * 5

    # Factor 3: Close to mid = more suspicious (more visible/impactful)
    ghosts["spoof_score"] += (1 - ghosts["distance_bps"] / PRICE_APPROACH_BPS).clip(0, 1) * 25

    # Factor 4: Price moved in expected direction after disappearance
    ghosts["spoof_score"] += ghosts["signal_correct"].astype(float) * 25

    # Classify
    ghosts["is_likely_spoof"] = ghosts["spoof_score"] >= 50
    ghosts["classification"] = ghosts["is_likely_spoof"].map({True: "likely_spoof", False: "genuine_cancel"})

    n_spoof = ghosts["is_likely_spoof"].sum()
    n_total = len(ghosts)
    print(f"  Total events: {n_total}")
    print(f"  Likely spoofs: {n_spoof} ({n_spoof/n_total*100:.1f}%)")
    print(f"  Genuine cancels: {n_total - n_spoof} ({(n_total-n_spoof)/n_total*100:.1f}%)")
    print(f"  Average spoof score: {ghosts['spoof_score'].mean():.1f}/100")
    print("  Note: Participant-level clustering is not feasible from L2 order book snapshots")
    print("  (no account/participant identifiers available in public REST data).")

    return ghosts


def _generate_synthetic_events():
    """Generate synthetic labeled events for classifier demonstration."""
    print("  Generating synthetic labeled events for classifier demo...")
    np.random.seed(42)
    n = 100

    events = pd.DataFrame({
        "exchange": np.random.choice(["binance", "hyperliquid"], n),
        "side": np.random.choice(["bid", "ask"], n),
        "price": np.random.normal(84000, 200, n),
        "max_quantity": np.abs(np.random.exponential(2, n)),
        "max_notional": np.abs(np.random.exponential(150000, n)),
        "duration_seconds": np.random.uniform(1, 30, n),
        "distance_bps": np.random.uniform(1, 20, n),
        "price_move_1s_bps": np.random.normal(0, 2, n),
        "price_move_5s_bps": np.random.normal(0, 3, n),
        "price_move_10s_bps": np.random.normal(0, 5, n),
        "price_move_30s_bps": np.random.normal(0, 8, n),
    })

    # Synthetic spoofing: short duration, large, close to mid, price moves after
    spoof_mask = (events["duration_seconds"] < 10) & (events["max_notional"] > 100000) & (events["distance_bps"] < 10)
    events["is_likely_spoof"] = spoof_mask
    events["spoof_score"] = np.where(spoof_mask, np.random.uniform(55, 95, n), np.random.uniform(10, 45, n))
    events["classification"] = np.where(spoof_mask, "likely_spoof", "genuine_cancel")
    events["signal_correct"] = np.random.random(n) > 0.4

    n_spoof = events["is_likely_spoof"].sum()
    print(f"  Generated {n} synthetic events ({n_spoof} spoofs, {n - n_spoof} genuine)")
    return events


def build_classifier(events: pd.DataFrame):
    """Train a simple classifier to flag spoof events."""
    print("\n" + "=" * 60)
    print("SPOOF EVENT CLASSIFIER")
    print("=" * 60)

    if events.empty or len(events) < 20:
        print("  Insufficient data for classifier training")
        return {}

    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score, train_test_split
        from sklearn.metrics import classification_report, confusion_matrix
    except ImportError:
        print("  scikit-learn not available, skipping ML classifier")
        return {}

    features = [
        "duration_seconds",
        "distance_bps",
        "max_notional",
        "price_move_1s_bps",
        "price_move_5s_bps",
        "price_move_10s_bps",
    ]
    available = [f for f in features if f in events.columns]

    if not available:
        print("  No features available")
        return {}

    X = events[available].fillna(0)
    y = events["is_likely_spoof"].astype(int)

    if y.nunique() < 2:
        print("  Only one class present; skipping classifier evaluation")
        return {}

    stratify = y if y.value_counts().min() >= 2 else None
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=0.2,
        random_state=42,
        stratify=stratify,
    )

    clf = RandomForestClassifier(
        n_estimators=100,
        random_state=42,
        max_depth=4,
        class_weight="balanced",
    )

    # Cross-validation
    min_class_count = y.value_counts().min()
    cv_folds = min(5, int(min_class_count)) if min_class_count >= 2 else 0
    print(f"  Features: {available}")
    if cv_folds >= 2:
        scores = cross_val_score(clf, X, y, cv=cv_folds, scoring="accuracy")
        print(f"  CV Accuracy: {scores.mean():.3f} ± {scores.std():.3f}")
        accuracy = scores.mean()
    else:
        print("  CV Accuracy: skipped (insufficient minority samples)")
        accuracy = float("nan")
    print("  Note: Classifier trained on 80% data, evaluated on held-out 20% test set.")

    clf.fit(X_train, y_train)
    y_pred = clf.predict(X_test)

    # Confusion matrix
    cm = confusion_matrix(y_test, y_pred, labels=[0, 1])
    report = classification_report(
        y_test,
        y_pred,
        labels=[0, 1],
        target_names=["genuine", "spoof"],
        output_dict=True,
        zero_division=0,
    )

    print(f"\n  Confusion Matrix:")
    print(f"    TN={cm[0][0]}  FP={cm[0][1]}")
    print(f"    FN={cm[1][0]}  TP={cm[1][1]}")

    fp_rate = cm[0][1] / (cm[0][0] + cm[0][1]) if (cm[0][0] + cm[0][1]) > 0 else 0
    print(f"\n  False Positive Rate: {fp_rate*100:.1f}%")
    print(f"  Precision (spoof): {report['spoof']['precision']:.3f}")
    print(f"  Recall (spoof): {report['spoof']['recall']:.3f}")
    print(f"  F1 (spoof): {report['spoof']['f1-score']:.3f}")

    # Feature importance
    importances = dict(zip(available, clf.feature_importances_))
    print(f"\n  Feature Importance:")
    for feat, imp in sorted(importances.items(), key=lambda x: -x[1]):
        print(f"    {feat}: {imp:.3f}")

    return {
        "accuracy": accuracy,
        "false_positive_rate": fp_rate,
        "precision": report["spoof"]["precision"],
        "recall": report["spoof"]["recall"],
        "f1": report["spoof"]["f1-score"],
        "importances": importances,
    }


def generate_charts(large_orders, ghosts, events):
    """Generate analysis charts."""
    print("\n  Generating charts...")

    # Chart 1: Large order distribution
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    if not large_orders.empty:
        for ex in large_orders["exchange"].unique():
            ex_data = large_orders[large_orders["exchange"] == ex]
            axes[0].hist(ex_data["notional_usd"] / 1000, bins=30, alpha=0.5, label=ex)
        axes[0].set_xlabel("Notional ($k)")
        axes[0].set_title("Large Order Size Distribution")
        axes[0].legend()

        for ex in large_orders["exchange"].unique():
            ex_data = large_orders[large_orders["exchange"] == ex]
            axes[1].hist(ex_data["distance_bps"], bins=30, alpha=0.5, label=ex)
        axes[1].set_xlabel("Distance from Mid (bps)")
        axes[1].set_title("Large Order Distance Distribution")
        axes[1].legend()
    plt.tight_layout()
    plt.savefig(DATA_DIR / "ghost_large_orders.png", dpi=150)
    plt.close()
    print("  Saved: large_orders.png")

    # Chart 2: Ghost event classification
    if not events.empty:
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        # Spoof score distribution
        axes[0].hist(events["spoof_score"], bins=30, color="steelblue", alpha=0.7, edgecolor="white")
        axes[0].axvline(x=50, color="red", linestyle="--", label="Spoof threshold")
        axes[0].set_xlabel("Spoof Score")
        axes[0].set_title("Spoof Score Distribution")
        axes[0].legend()

        # Classification breakdown
        class_counts = events["classification"].value_counts()
        axes[1].pie(class_counts.values, labels=class_counts.index, autopct="%1.1f%%",
                     colors=["#e74c3c", "#2ecc71"])
        axes[1].set_title("Event Classification")
        plt.tight_layout()
        plt.savefig(DATA_DIR / "ghost_spoofing_classification.png", dpi=150)
        plt.close()
        print("  Saved: spoofing_classification.png")

    # Chart 3: Price movement after events
    if not events.empty and "price_move_10s_bps" in events.columns:
        fig, ax = plt.subplots(figsize=(10, 5))
        for cls in ["likely_spoof", "genuine_cancel"]:
            cls_data = events[events["classification"] == cls]
            if not cls_data.empty:
                ax.hist(cls_data["price_move_10s_bps"], bins=30, alpha=0.5, label=cls)
        ax.set_xlabel("Price Move 10s After (bps)")
        ax.set_title("Price Movement After Ghost Events")
        ax.axvline(x=0, color="gray", linestyle="--")
        ax.legend()
        plt.tight_layout()
        plt.savefig(DATA_DIR / "ghost_price_after_events.png", dpi=150)
        plt.close()
        print("  Saved: price_after_events.png")

    # Chart 4: Time-of-day spoof clustering
    # Uses real hourly distribution when multi-day data is available, otherwise
    # shows the distribution of events within the available session.
    if not events.empty and "appear_time" in events.columns:
        ts_col = pd.to_datetime(events["appear_time"], errors="coerce")
        if ts_col.notna().any():
            span_h = (ts_col.max() - ts_col.min()).total_seconds() / 3600
            events_with_hour = events.copy()
            events_with_hour["hour"] = ts_col.dt.hour

            fig, axes = plt.subplots(1, 2, figsize=(14, 5))

            # All events by hour
            hourly_all = events_with_hour.groupby("hour").size()
            hourly_spoof = (
                events_with_hour[events_with_hour["is_likely_spoof"]]
                .groupby("hour")
                .size()
            )
            all_hours = range(24)
            axes[0].bar(
                list(all_hours),
                [hourly_all.get(h, 0) for h in all_hours],
                alpha=0.5, label="All ghost events", color="steelblue",
            )
            axes[0].bar(
                list(all_hours),
                [hourly_spoof.get(h, 0) for h in all_hours],
                alpha=0.7, label="Likely spoofs", color="red",
            )
            axes[0].set_xlabel("Hour of Day (UTC)")
            axes[0].set_ylabel("Event Count")
            if span_h >= 2:
                axes[0].set_title(
                    f"Time-of-Day Spoof Clustering "
                    f"({span_h:.1f}h of data)"
                )
            else:
                axes[0].set_title(
                    "Time-of-Day Distribution (within-session; run ws_collector for full 24h)"
                )
            axes[0].legend()

            # By exchange
            for ex in events_with_hour["exchange"].unique():
                ex_h = events_with_hour[events_with_hour["exchange"] == ex].groupby("hour").size()
                axes[1].plot(
                    list(all_hours),
                    [ex_h.get(h, 0) for h in all_hours],
                    marker="o", label=ex,
                )
            axes[1].set_xlabel("Hour of Day (UTC)")
            axes[1].set_ylabel("Event Count")
            axes[1].set_title("Spoof Events per Exchange by Hour")
            axes[1].legend()

            plt.tight_layout()
            plt.savefig(DATA_DIR / "ghost_tod_spoof_clustering.png", dpi=150)
            plt.close()
            print("  Saved: tod_spoof_clustering.png")


def save_to_excel(large_orders, ghosts, events, clf_results):
    path = DATA_DIR / "ghost_analysis_results.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        if not large_orders.empty:
            large_orders.head(2000).to_excel(writer, sheet_name="Large Orders", index=False)
        if not ghosts.empty:
            ghosts.to_excel(writer, sheet_name="Ghost Levels", index=False)
        if not events.empty:
            events.to_excel(writer, sheet_name="Classified Events", index=False)

        # Classifier metrics
        if clf_results:
            metrics = {k: v for k, v in clf_results.items() if k != "importances"}
            pd.DataFrame(list(metrics.items()), columns=["Metric", "Value"]).to_excel(
                writer, sheet_name="Classifier Metrics", index=False)

    print(f"\n  Excel saved: {path}")


def main():
    print("=" * 60)
    print("LATENT LIQUIDITY & SPOOFING BEHAVIOUR DETECTOR")
    print("=" * 60)

    ob, klines = load_data()
    print(f"Loaded {len(ob)} orderbook rows, {len(klines)} kline rows")

    large_orders = detect_large_orders(ob)
    ghosts = detect_ghost_levels(ob, large_orders)
    events = classify_spoofing(ghosts)
    clf_results = build_classifier(events)
    generate_charts(large_orders, ghosts, events)
    save_to_excel(large_orders, ghosts, events, clf_results)

    # Report time-of-day clustering status
    if not ob.empty:
        span_days = (ob["timestamp"].max() - ob["timestamp"].min()).total_seconds() / 86400
        status = "multi-day OK" if span_days >= 1 else "sub-day -- run ws_collector.py for multi-day analysis"
        print(f"\n  Data coverage: {span_days:.3f} days ({status})")

    print("\n" + "=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
