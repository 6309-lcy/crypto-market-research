"""
fetch_history.py - Fetch 1-minute BTC kline history for Binance Futures and Hyperliquid.

This extends the price-context dataset to roughly 10 trading days using public REST APIs.
Historical L2 order book snapshots are not available from these public endpoints, so the
orderbook sample remains the 5-minute live collection captured by fetch_orderbook.py.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(exist_ok=True)

BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
SYMBOL = "BTCUSDT"
COIN = "BTC"
INTERVAL = "1m"
LOOKBACK_DAYS = 10
LOOKBACK_MINUTES = LOOKBACK_DAYS * 24 * 60
BINANCE_LIMIT = 1500
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def request_json(session: aiohttp.ClientSession, method: str, url: str, **kwargs):
    last_error = None
    for attempt in range(3):
        try:
            async with session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs) as resp:
                if 400 <= resp.status < 500:
                    message = await resp.text()
                    raise RuntimeError(f"{resp.status} client error from {url}: {message[:200]}")
                resp.raise_for_status()
                return await resp.json()
        except Exception as exc:  # pragma: no cover - network retry path
            last_error = exc
            if isinstance(exc, RuntimeError) and "client error" in str(exc):
                break
            await asyncio.sleep(1 + attempt)
    raise RuntimeError(f"Request failed after retries: {url}") from last_error


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


async def fetch_binance_history(session: aiohttp.ClientSession, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows = []
    cursor_end = end_ms

    while cursor_end >= start_ms:
        params = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "limit": BINANCE_LIMIT,
            "endTime": cursor_end,
        }
        data = await request_json(session, "GET", BINANCE_URL, params=params)
        if not data:
            break

        batch = [
            {
                "open_time": _iso_utc(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "trades": int(k[8]),
                "exchange": "binance",
                "_open_ms": int(k[0]),
            }
            for k in data
        ]
        rows.extend(batch)

        oldest_open_ms = min(item["_open_ms"] for item in batch)
        if oldest_open_ms <= start_ms:
            break

        cursor_end = oldest_open_ms - 1
        await asyncio.sleep(0.2)

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    frame = (
        frame.drop_duplicates(subset=["exchange", "open_time"])
        .sort_values("_open_ms")
        .loc[lambda df: df["_open_ms"].between(start_ms, end_ms)]
        .drop(columns="_open_ms")
        .reset_index(drop=True)
    )
    return frame


async def fetch_hyperliquid_history(session: aiohttp.ClientSession, start_ms: int, end_ms: int) -> pd.DataFrame:
    payload_candidates = [
        {
            "type": "candleSnapshot",
            "req": {
                "coin": COIN,
                "resolution": INTERVAL,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        },
        {
            "type": "candleSnapshot",
            "req": {
                "coin": COIN,
                "interval": INTERVAL,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        },
    ]

    last_error = None
    data = None
    for payload in payload_candidates:
        try:
            data = await request_json(session, "POST", HYPERLIQUID_URL, json=payload)
            break
        except Exception as exc:  # pragma: no cover - depends on API schema
            last_error = exc

    if data is None:
        raise RuntimeError("Hyperliquid candle request failed") from last_error

    rows = [
        {
            "open_time": _iso_utc(int(k["t"])),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "trades": int(k["n"]),
            "exchange": "hyperliquid",
            "_open_ms": int(k["t"]),
        }
        for k in data
    ]

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    frame = (
        frame.drop_duplicates(subset=["exchange", "open_time"])
        .sort_values("_open_ms")
        .loc[lambda df: df["_open_ms"].between(start_ms, end_ms)]
        .drop(columns="_open_ms")
        .reset_index(drop=True)
    )
    return frame


def print_summary(klines: pd.DataFrame, requested_start: datetime, requested_end: datetime):
    print("\nSaved kline history to data\\klines_1m.csv")
    print(f"Requested window: {requested_start.isoformat()} to {requested_end.isoformat()}")

    if klines.empty:
        print("No rows were fetched.")
        return

    for exchange, group in klines.groupby("exchange"):
        start = pd.to_datetime(group["open_time"], utc=True).min()
        end = pd.to_datetime(group["open_time"], utc=True).max()
        print(f"  [{exchange}] rows={len(group):,} range={start.isoformat()} to {end.isoformat()}")

    if "hyperliquid" in klines["exchange"].values:
        hl = klines[klines["exchange"] == "hyperliquid"]
        if len(hl) < LOOKBACK_MINUTES:
            print("  Note: Hyperliquid public candle history returned fewer than 10 full days.")
            print("  This reflects API availability limits, not synthetic backfilling.")


async def main():
    requested_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    requested_start = requested_end - timedelta(minutes=LOOKBACK_MINUTES - 1)
    start_ms = int(requested_start.timestamp() * 1000)
    end_ms = int(requested_end.timestamp() * 1000)

    print("=" * 60)
    print("FETCHING 1-MINUTE BTC KLINE HISTORY")
    print("=" * 60)
    print(f"Window: {requested_start.isoformat()} -> {requested_end.isoformat()}")

    async with aiohttp.ClientSession() as session:
        binance_task = fetch_binance_history(session, start_ms, end_ms)
        hyperliquid_task = fetch_hyperliquid_history(session, start_ms, end_ms)
        binance_df, hyperliquid_df = await asyncio.gather(binance_task, hyperliquid_task)

    combined = pd.concat([binance_df, hyperliquid_df], ignore_index=True)
    if not combined.empty:
        combined = combined[
            ["open_time", "open", "high", "low", "close", "volume", "trades", "exchange"]
        ].sort_values(["exchange", "open_time"]).reset_index(drop=True)

    output_path = DATA_DIR / "ghost_klines_1m.csv"
    combined.to_csv(output_path, index=False)
    print_summary(combined, requested_start, requested_end)


if __name__ == "__main__":
    asyncio.run(main())
