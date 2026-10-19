"""
fetch_history.py - Fetch 30 days of 1-minute BTC perpetual klines.

This script refreshes data\\klines_1m.csv using exchange REST APIs:
  - Binance futures klines
  - Bybit linear perpetual klines
  - OKX swap history candles
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_CSV = DATA_DIR / "liquidity_klines_1m.csv"

LOOKBACK_DAYS = 30
MINUTE_MS = 60_000
BINANCE_LIMIT = 1500
BYBIT_LIMIT = 1000
OKX_LIMIT = 100

HEADERS = {"User-Agent": "task3-history-fetcher/1.0"}


def to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def dedupe_and_sort(rows: list[dict]) -> list[dict]:
    deduped = {}
    for row in rows:
        deduped[(row["exchange"], row["open_time"])] = row
    return sorted(deduped.values(), key=lambda row: (row["exchange"], row["open_time"]))


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict,
    exchange: str,
) -> dict | list:
    last_error = None
    for attempt in range(3):
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
                if exchange == "bybit" and payload.get("retCode", 0) != 0:
                    raise RuntimeError(f"Bybit error: {payload.get('retMsg', 'unknown')}")
                if exchange == "okx" and str(payload.get("code", "0")) not in {"0", "None"}:
                    raise RuntimeError(f"OKX error: {payload.get('msg', 'unknown')}")
                return payload
        except Exception as exc:  # pragma: no cover - network retries
            last_error = exc
            await asyncio.sleep(1 + attempt)

    raise RuntimeError(f"[{exchange}] request failed after retries: {last_error}")


async def fetch_binance(
    session: aiohttp.ClientSession,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    url = "https://fapi.binance.com/fapi/v1/klines"
    rows = []
    cursor_end = end_ms

    while cursor_end >= start_ms:
        window_start = max(start_ms, cursor_end - (BINANCE_LIMIT - 1) * MINUTE_MS)
        payload = await fetch_json(
            session,
            url,
            {
                "symbol": "BTCUSDT",
                "interval": "1m",
                "limit": BINANCE_LIMIT,
                "startTime": window_start,
                "endTime": cursor_end,
            },
            "binance",
        )
        if not payload:
            break

        batch = []
        for candle in payload:
            ts_ms = int(candle[0])
            if ts_ms < start_ms or ts_ms > end_ms:
                continue
            batch.append(
                {
                    "exchange": "binance",
                    "open_time": to_iso(ts_ms),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                }
            )

        if not batch:
            break

        rows.extend(batch)
        oldest_ms = min(int(pd.Timestamp(row["open_time"]).timestamp() * 1000) for row in batch)
        newest_ms = max(int(pd.Timestamp(row["open_time"]).timestamp() * 1000) for row in batch)
        print(
            f"  [binance] fetched {len(batch):4d} rows "
            f"{to_iso(oldest_ms)} -> {to_iso(newest_ms)}"
        )
        if oldest_ms <= start_ms:
            break

        next_cursor = oldest_ms - MINUTE_MS
        if next_cursor >= cursor_end:
            break
        cursor_end = next_cursor
        await asyncio.sleep(0.1)

    return dedupe_and_sort(rows)


async def fetch_bybit(
    session: aiohttp.ClientSession,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    url = "https://api.bybit.com/v5/market/kline"
    rows = []
    cursor_end = end_ms

    while cursor_end >= start_ms:
        window_start = max(start_ms, cursor_end - (BYBIT_LIMIT - 1) * MINUTE_MS)
        payload = await fetch_json(
            session,
            url,
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "interval": "1",
                "limit": BYBIT_LIMIT,
                "start": window_start,
                "end": cursor_end,
            },
            "bybit",
        )
        candles = payload.get("result", {}).get("list", [])
        if not candles:
            break

        batch = []
        for candle in candles:
            ts_ms = int(candle[0])
            if ts_ms < start_ms or ts_ms > end_ms:
                continue
            batch.append(
                {
                    "exchange": "bybit",
                    "open_time": to_iso(ts_ms),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                }
            )

        if not batch:
            break

        rows.extend(batch)
        oldest_ms = min(int(pd.Timestamp(row["open_time"]).timestamp() * 1000) for row in batch)
        newest_ms = max(int(pd.Timestamp(row["open_time"]).timestamp() * 1000) for row in batch)
        print(
            f"  [bybit]   fetched {len(batch):4d} rows "
            f"{to_iso(oldest_ms)} -> {to_iso(newest_ms)}"
        )
        if oldest_ms <= start_ms:
            break

        next_cursor = oldest_ms - MINUTE_MS
        if next_cursor >= cursor_end:
            break
        cursor_end = next_cursor
        await asyncio.sleep(0.1)

    return dedupe_and_sort(rows)


async def fetch_okx(
    session: aiohttp.ClientSession,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    url = "https://www.okx.com/api/v5/market/history-candles"
    rows = []
    cursor_after = None

    while True:
        params = {"instId": "BTC-USDT-SWAP", "bar": "1m", "limit": str(OKX_LIMIT)}
        if cursor_after is not None:
            params["after"] = str(cursor_after)

        payload = await fetch_json(session, url, params, "okx")
        candles = payload.get("data", [])
        if not candles:
            break

        batch = []
        for candle in candles:
            ts_ms = int(candle[0])
            if ts_ms < start_ms or ts_ms > end_ms:
                continue
            batch.append(
                {
                    "exchange": "okx",
                    "open_time": to_iso(ts_ms),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                }
            )

        if batch:
            rows.extend(batch)
            oldest_ms = min(int(pd.Timestamp(row["open_time"]).timestamp() * 1000) for row in batch)
            newest_ms = max(int(pd.Timestamp(row["open_time"]).timestamp() * 1000) for row in batch)
            print(
                f"  [okx]     fetched {len(batch):4d} rows "
                f"{to_iso(oldest_ms)} -> {to_iso(newest_ms)}"
            )
            if oldest_ms <= start_ms:
                break
            next_cursor = oldest_ms - 1
        else:
            raw_oldest = min(int(candle[0]) for candle in candles)
            next_cursor = raw_oldest - 1
            if raw_oldest <= start_ms:
                break

        if cursor_after is not None and next_cursor >= cursor_after:
            break
        cursor_after = next_cursor
        await asyncio.sleep(0.1)

    return dedupe_and_sort(rows)


async def main():
    print("=" * 60)
    print("FETCHING 30 DAYS OF 1-MINUTE BTC PERPETUAL KLINES")
    print("=" * 60)

    end_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    print(f"Window: {start_dt.isoformat()} -> {end_dt.isoformat()}")

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        binance_rows, bybit_rows, okx_rows = await asyncio.gather(
            fetch_binance(session, start_ms, end_ms),
            fetch_bybit(session, start_ms, end_ms),
            fetch_okx(session, start_ms, end_ms),
        )

    all_rows = dedupe_and_sort(binance_rows + bybit_rows + okx_rows)
    if not all_rows:
        raise RuntimeError("No kline data was fetched.")

    df = pd.DataFrame(all_rows, columns=["exchange", "open_time", "open", "high", "low", "close", "volume"])
    df.to_csv(OUTPUT_CSV, index=False)

    print("\nSaved refreshed history to:", OUTPUT_CSV)
    print(f"Total rows: {len(df):,}")
    summary = df.groupby("exchange")["open_time"].agg(["count", "min", "max"])
    print(summary.to_string())


if __name__ == "__main__":
    asyncio.run(main())
