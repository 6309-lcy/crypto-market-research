"""
fetch_orderbook.py - Collects top-10 level orderbook snapshots from Binance & Hyperliquid.

Polls REST APIs every 1 second for 5 minutes to capture orderbook dynamics
including large orders that appear and disappear (ghost levels).

For long-running multi-day collection use ws_collector.py instead, which
connects via WebSocket and appends to ws_orderbook_snapshots.csv indefinitely.

Flags:
    --append    Append to the existing CSV instead of overwriting it.
    --duration  Collection duration in seconds (default 300).
"""

import argparse
import asyncio
import aiohttp
import time
import csv
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(exist_ok=True)

POLL_INTERVAL = 1  # 1 second for fine-grained ghost detection
TOTAL_DURATION = 300  # 5 minutes
NUM_LEVELS = 10


async def fetch_binance(session):
    url = "https://fapi.binance.com/fapi/v1/depth"
    params = {"symbol": "BTCUSDT", "limit": 10}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            rows = []
            for i, (p, q) in enumerate(data.get("bids", [])[:NUM_LEVELS]):
                rows.append(("bid", i + 1, float(p), float(q)))
            for i, (p, q) in enumerate(data.get("asks", [])[:NUM_LEVELS]):
                rows.append(("ask", i + 1, float(p), float(q)))
            return rows
    except Exception as e:
        return []


async def fetch_hyperliquid(session):
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "l2Book", "coin": "BTC"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            rows = []
            levels = data.get("levels", [[], []])
            for i, level in enumerate(levels[0][:NUM_LEVELS]):  # bids
                rows.append(("bid", i + 1, float(level["px"]), float(level["sz"])))
            for i, level in enumerate(levels[1][:NUM_LEVELS]):  # asks
                rows.append(("ask", i + 1, float(level["px"]), float(level["sz"])))
            return rows
    except Exception as e:
        return []


async def main(append: bool = False, duration: int = TOTAL_DURATION):
    print("=" * 60)
    print("ORDERBOOK SNAPSHOT COLLECTOR (Spoofing Detection)")
    print("=" * 60)
    print(f"Exchanges: Binance, Hyperliquid")
    print(f"Duration: {duration}s, Interval: {POLL_INTERVAL}s")
    print(f"Mode: {'APPEND to existing CSV' if append else 'OVERWRITE'}")

    all_rows = []
    errors = 0
    snapshots = 0

    async with aiohttp.ClientSession() as session:
        start = time.time()
        while time.time() - start < duration:
            ts = datetime.now(timezone.utc).isoformat()

            bn_rows = await fetch_binance(session)
            hl_rows = await fetch_hyperliquid(session)

            for exchange, rows in [("binance", bn_rows), ("hyperliquid", hl_rows)]:
                if rows:
                    bids = [r for r in rows if r[0] == "bid"]
                    asks = [r for r in rows if r[0] == "ask"]
                    mid = (bids[0][2] + asks[0][2]) / 2 if bids and asks else 0
                    for side, lvl, price, qty in rows:
                        notional = qty * price
                        all_rows.append([ts, exchange, side, lvl, price, qty, notional, mid])
                else:
                    errors += 1

            snapshots += 1
            elapsed = time.time() - start
            if snapshots % 30 == 0:
                print(f"  {snapshots} snapshots ({elapsed:.0f}s, {len(all_rows)} rows)")

            await asyncio.sleep(POLL_INTERVAL)

    # Write CSV (append or overwrite)
    output = DATA_DIR / "ghost_orderbook_snapshots.csv"
    header = ["timestamp", "exchange", "side", "level", "price", "quantity", "notional_usd", "mid_price"]
    write_header = (not append) or (not output.exists())
    mode = "a" if append else "w"
    with open(output, mode, newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
        writer.writerows(all_rows)

    print(f"\nCollected {snapshots} snapshots, {len(all_rows)} rows, {errors} errors")
    print(f"Saved to {output} (mode={mode})")

    # Also fetch klines for price movement analysis
    print("\nFetching 1-second klines from Binance...")
    await fetch_klines()


async def fetch_klines():
    """Fetch 1-second aggregate trades for fine-grained price analysis."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    async with aiohttp.ClientSession() as session:
        # 1-minute klines (finest available via REST)
        params = {"symbol": "BTCUSDT", "interval": "1m", "limit": 1500}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                import pandas as pd
                klines = []
                for k in data:
                    klines.append({
                        "open_time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).isoformat(),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "trades": int(k[8]),
                    })
                pd.DataFrame(klines).to_csv(DATA_DIR / "ghost_klines_1m.csv", index=False)
                print(f"  Saved {len(klines)} klines")
        except Exception as e:
            print(f"  Kline error: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Orderbook snapshot collector")
    parser.add_argument("--append", action="store_true",
                        help="Append to existing CSV instead of overwriting")
    parser.add_argument("--duration", type=int, default=TOTAL_DURATION,
                        help="Collection duration in seconds (default 300)")
    args = parser.parse_args()
    asyncio.run(main(append=args.append, duration=args.duration))
