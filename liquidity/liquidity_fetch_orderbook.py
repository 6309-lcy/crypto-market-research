"""
fetch_orderbook.py - Collects L2 orderbook snapshots from Binance, OKX, and Bybit.

Polls REST APIs every 2 seconds for ~5 minutes to build a real dataset of
orderbook snapshots across 3 exchanges for BTC/USDT perpetual futures.
"""

import asyncio
import aiohttp
import time
import csv
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_CSV = DATA_DIR / "liquidity_orderbook_snapshots.csv"

EXCHANGES = {
    "binance": {
        "url": "https://fapi.binance.com/fapi/v1/depth",
        "params": {"symbol": "BTCUSDT", "limit": 20},
    },
    "okx": {
        "url": "https://www.okx.com/api/v5/market/books",
        "params": {"instId": "BTC-USDT-SWAP", "sz": "20"},
    },
    "bybit": {
        "url": "https://api.bybit.com/v5/market/orderbook",
        "params": {"category": "linear", "symbol": "BTCUSDT", "limit": "20"},
    },
}

POLL_INTERVAL = 2  # seconds
TOTAL_DURATION = 300  # 5 minutes
NUM_LEVELS = 20


def parse_binance(data: dict) -> list:
    """Parse Binance depth response into bid/ask rows."""
    rows = []
    for i, (price, qty) in enumerate(data.get("bids", [])[:NUM_LEVELS]):
        rows.append(("bid", i + 1, float(price), float(qty)))
    for i, (price, qty) in enumerate(data.get("asks", [])[:NUM_LEVELS]):
        rows.append(("ask", i + 1, float(price), float(qty)))
    return rows


def parse_okx(data: dict) -> list:
    """Parse OKX books response."""
    rows = []
    books = data.get("data", [{}])
    if not books:
        return rows
    book = books[0]
    for i, level in enumerate(book.get("bids", [])[:NUM_LEVELS]):
        rows.append(("bid", i + 1, float(level[0]), float(level[1])))
    for i, level in enumerate(book.get("asks", [])[:NUM_LEVELS]):
        rows.append(("ask", i + 1, float(level[0]), float(level[1])))
    return rows


def parse_bybit(data: dict) -> list:
    """Parse Bybit orderbook response."""
    rows = []
    result = data.get("result", {})
    for i, level in enumerate(result.get("b", [])[:NUM_LEVELS]):
        rows.append(("bid", i + 1, float(level[0]), float(level[1])))
    for i, level in enumerate(result.get("a", [])[:NUM_LEVELS]):
        rows.append(("ask", i + 1, float(level[0]), float(level[1])))
    return rows


PARSERS = {"binance": parse_binance, "okx": parse_okx, "bybit": parse_bybit}


async def fetch_snapshot(session: aiohttp.ClientSession, exchange: str, config: dict):
    """Fetch one orderbook snapshot from an exchange."""
    try:
        async with session.get(config["url"], params=config["params"], timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            return PARSERS[exchange](data)
    except Exception as e:
        print(f"  [{exchange}] Error: {e}")
        return []


async def main():
    print("=" * 60)
    print("CROSS-EXCHANGE ORDERBOOK COLLECTOR")
    print("=" * 60)
    print(f"Exchanges: {', '.join(EXCHANGES.keys())}")
    print(f"Duration: {TOTAL_DURATION}s, Interval: {POLL_INTERVAL}s")

    all_rows = []
    errors = 0
    snapshots = 0

    async with aiohttp.ClientSession() as session:
        start = time.time()
        while time.time() - start < TOTAL_DURATION:
            ts = datetime.now(timezone.utc).isoformat()
            tasks = {ex: fetch_snapshot(session, ex, cfg) for ex, cfg in EXCHANGES.items()}
            results = {}
            for ex, coro in tasks.items():
                results[ex] = await coro

            for ex, levels in results.items():
                if levels:
                    # Compute mid price
                    bids = [r for r in levels if r[0] == "bid"]
                    asks = [r for r in levels if r[0] == "ask"]
                    if bids and asks:
                        mid = (bids[0][2] + asks[0][2]) / 2
                    else:
                        mid = 0
                    for side, lvl, price, qty in levels:
                        all_rows.append([ts, ex, side, lvl, price, qty, mid])
                else:
                    errors += 1

            snapshots += 1
            elapsed = time.time() - start
            if snapshots % 15 == 0:
                print(f"  {snapshots} snapshots ({elapsed:.0f}s elapsed, {len(all_rows)} rows)")

            await asyncio.sleep(POLL_INTERVAL)

    # Write to CSV
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "exchange", "side", "level", "price", "quantity", "mid_price"])
        writer.writerows(all_rows)

    print(f"\nCollected {snapshots} snapshots, {len(all_rows)} rows, {errors} errors")
    print(f"Saved to {OUTPUT_CSV}")

    print("\nRun src\\fetch_history.py separately to refresh the 30-day kline history.")


if __name__ == "__main__":
    asyncio.run(main())
