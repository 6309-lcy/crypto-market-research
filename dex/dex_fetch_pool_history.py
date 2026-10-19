"""
fetch_pool_history.py - Fetch 30-day pool and protocol TVL history for Task 4.

Improvements over prior version:
- Attempts to fetch hourly pool data from The Graph (Uniswap v3 subgraph).
  If available, the hourly dataset is written to dex_pool_history_hourly.csv.
- Falls back to DeFiLlama daily TVL when The Graph is unavailable.
"""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
POOL_HISTORY_PATH = DATA_DIR / "dex_pool_history.csv"
POOL_HISTORY_HOURLY_PATH = DATA_DIR / "dex_pool_history_hourly.csv"
PROTOCOL_HISTORY_PATH = DATA_DIR / "dex_historical_tvl.csv"
POOL_COLUMNS = ["date", "dex", "pair", "pool_id", "tvl_usd"]
POOL_HOURLY_COLUMNS = ["datetime", "dex", "pair", "pool_id", "tvl_usd", "volume_usd", "tx_count"]
PROTOCOL_COLUMNS = ["protocol", "date", "tvl_usd"]
PROTOCOL_SLUGS = ["uniswap", "curve-dex", "sushiswap"]

# Well-known Uniswap v3 Ethereum pool addresses for the target pairs
UNISWAP_V3_POOLS = {
    "ETH_USDC": [
        "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",  # ETH/USDC 0.05%
        "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8",  # ETH/USDC 0.3%
    ],
    "WBTC_USDC": [
        "0x99ac8ca7087fa4a2a1fb6357269965a2014abc35",  # WBTC/USDC 0.3%
        "0xfbaa9d1dfd3ab8a9deab5cdc8fc3d0e5e95f6a09",  # WBTC/USDC 0.05%
    ],
}

# The Graph: Uniswap v3 hosted subgraph (public, no API key required for hosted service)
UNISWAP_SUBGRAPH_URL = "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3"


def get_date_window():
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=29)
    return start_date, end_date


def parse_timestamp(value):
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(ts) else ts


def build_synthetic_history(pool_row: pd.Series, start_date, end_date) -> pd.DataFrame:
    """Fallback modelled series if the pool chart API is unavailable."""
    pool_id = str(pool_row["pool_id"])
    seed = int(hashlib.sha256(pool_id.encode("utf-8")).hexdigest()[:16], 16)
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start_date, end_date, freq="D")
    base_tvl = max(float(pool_row.get("tvl_usd", 0) or 0), 1.0)
    shocks = rng.uniform(-0.05, 0.05, size=len(dates))
    trend = np.linspace(-0.015, 0.015, len(dates))
    tvls = np.maximum(base_tvl * (1 + shocks + trend), 1.0)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "dex": pool_row["dex"],
        "pair": pool_row["pair"],
        "pool_id": pool_id,
        "tvl_usd": tvls,
    })


async def fetch_pool_chart(session: aiohttp.ClientSession, pool_row: pd.Series, start_date, end_date):
    pool_id = str(pool_row["pool_id"]).strip()
    url = f"https://yields.llama.fi/chart/{pool_id}"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            payload = await resp.json()

        rows = []
        for item in payload.get("data", []):
            ts = parse_timestamp(item.get("timestamp"))
            if ts is None:
                continue
            rows.append({
                "timestamp": ts,
                "date": ts.date(),
                "tvl_usd": float(item.get("tvlUsd") or 0),
            })

        if rows:
            df = pd.DataFrame(rows).sort_values("timestamp")
            df = df.groupby("date", as_index=False).last()
            df = df[(df["date"] >= start_date) & (df["date"] <= end_date)].copy()
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                df["dex"] = pool_row["dex"]
                df["pair"] = pool_row["pair"]
                df["pool_id"] = pool_id
                return df[POOL_COLUMNS], False
    except Exception as exc:
        print(f"  [Pool API fallback] {pool_row['dex']} {pool_row['pair']} {pool_id}: {exc}")

    synthetic = build_synthetic_history(pool_row, start_date, end_date)
    return synthetic[POOL_COLUMNS], True


async def fetch_protocol_history(session: aiohttp.ClientSession, slug: str, start_date, end_date) -> pd.DataFrame:
    url = f"https://api.llama.fi/protocol/{slug}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    eth_tvl = payload.get("chainTvls", {}).get("Ethereum", {}).get("tvl", [])
    rows = []
    for item in eth_tvl:
        timestamp = item.get("date")
        if timestamp is None:
            continue
        dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()
        if not (start_date <= dt <= end_date):
            continue
        rows.append({
            "protocol": slug,
            "date": dt,
            "tvl_usd": float(item.get("totalLiquidityUSD") or 0),
        })

    if not rows:
        return pd.DataFrame(columns=PROTOCOL_COLUMNS)

    df = pd.DataFrame(rows).sort_values("date")
    df = df.groupby(["protocol", "date"], as_index=False).last()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df[PROTOCOL_COLUMNS]


async def fetch_uniswap_hourly(
    session: aiohttp.ClientSession,
    start_date,
    end_date,
) -> pd.DataFrame:
    """
    Attempt to fetch hourly Uniswap v3 pool data via The Graph hosted subgraph.
    Returns an empty DataFrame if the endpoint is unavailable or returns no data.
    """
    print("\n  [The Graph] Fetching Uniswap v3 hourly pool data...")
    start_ts = int(datetime(start_date.year, start_date.month, start_date.day,
                            tzinfo=timezone.utc).timestamp())
    all_rows = []

    for pair, addresses in UNISWAP_V3_POOLS.items():
        for addr in addresses:
            skip = 0
            while True:
                query = """
                {
                  poolHourDatas(
                    first: 500
                    skip: %d
                    orderBy: periodStartUnix
                    orderDirection: asc
                    where: {pool: "%s", periodStartUnix_gt: %d}
                  ) {
                    periodStartUnix
                    tvlUSD
                    volumeUSD
                    txCount
                  }
                }
                """ % (skip, addr.lower(), start_ts)
                try:
                    async with session.post(
                        UNISWAP_SUBGRAPH_URL,
                        json={"query": query},
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        payload = await resp.json()
                        if "errors" in payload:
                            raise RuntimeError(str(payload["errors"]))
                        items = payload.get("data", {}).get("poolHourDatas", [])
                        if not items:
                            break
                        for item in items:
                            dt = datetime.fromtimestamp(
                                int(item["periodStartUnix"]), tz=timezone.utc
                            )
                            if dt.date() > end_date:
                                continue
                            all_rows.append({
                                "datetime": dt.strftime("%Y-%m-%dT%H:00:00"),
                                "dex": "uniswap_v3",
                                "pair": pair,
                                "pool_id": addr.lower(),
                                "tvl_usd": float(item.get("tvlUSD") or 0),
                                "volume_usd": float(item.get("volumeUSD") or 0),
                                "tx_count": int(item.get("txCount") or 0),
                            })
                        if len(items) < 500:
                            break
                        skip += 500
                except Exception as exc:
                    print(f"  [The Graph] {pair} {addr[:8]}…: {exc}")
                    break
                await asyncio.sleep(0.3)

    if not all_rows:
        print("  [The Graph] No hourly data returned -- subgraph may be unavailable.")
        return pd.DataFrame(columns=POOL_HOURLY_COLUMNS)

    df = pd.DataFrame(all_rows)
    df["tvl_usd"] = pd.to_numeric(df["tvl_usd"], errors="coerce").fillna(0.0)
    df = df.sort_values(["pair", "pool_id", "datetime"]).reset_index(drop=True)
    n_hours = df["datetime"].nunique()
    print(
        f"  [The Graph] Retrieved {len(df)} hourly rows across "
        f"{df['pool_id'].nunique()} pools, {n_hours} unique hours."
    )
    return df[POOL_HOURLY_COLUMNS]


async def main():
    print("=" * 60)
    print("FETCHING 30-DAY POOL HISTORY")
    print("=" * 60)

    DATA_DIR.mkdir(exist_ok=True)
    pools_path = DATA_DIR / "dex_dex_pools.csv"
    if not pools_path.exists():
        raise FileNotFoundError(f"Missing required input: {pools_path}")

    pools = pd.read_csv(pools_path)
    pools = pools[pools["pool_id"].notna()].copy()
    pools["pool_id"] = pools["pool_id"].astype(str).str.strip()
    pools = pools[pools["pool_id"] != ""]

    start_date, end_date = get_date_window()
    print(f"Date window: {start_date} to {end_date}")
    print(f"Pools with pool_id: {len(pools)}")

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    ) as session:
        pool_tasks = [
            fetch_pool_chart(session, row, start_date, end_date)
            for _, row in pools.iterrows()
        ]
        pool_results = await asyncio.gather(*pool_tasks)

        pool_frames = [frame for frame, _ in pool_results if not frame.empty]
        modelled_count = sum(1 for _, used_fallback in pool_results if used_fallback)

        protocol_frames = []
        for slug in PROTOCOL_SLUGS:
            try:
                protocol_frames.append(await fetch_protocol_history(session, slug, start_date, end_date))
                print(f"  [Protocol] {slug}: ok")
            except Exception as exc:
                print(f"  [Protocol] {slug}: {exc}")

        # Attempt hourly data from The Graph
        hourly_df = await fetch_uniswap_hourly(session, start_date, end_date)

    pool_history = pd.concat(pool_frames, ignore_index=True) if pool_frames else pd.DataFrame(columns=POOL_COLUMNS)
    if not pool_history.empty:
        pool_history["tvl_usd"] = pd.to_numeric(pool_history["tvl_usd"], errors="coerce").fillna(0.0)
        pool_history = pool_history.sort_values(["pair", "dex", "pool_id", "date"]).reset_index(drop=True)
    pool_history.to_csv(POOL_HISTORY_PATH, index=False)

    protocol_history = (
        pd.concat(protocol_frames, ignore_index=True)
        if protocol_frames else pd.DataFrame(columns=PROTOCOL_COLUMNS)
    )
    if not protocol_history.empty:
        protocol_history["tvl_usd"] = pd.to_numeric(protocol_history["tvl_usd"], errors="coerce").fillna(0.0)
        protocol_history = protocol_history.sort_values(["protocol", "date"]).reset_index(drop=True)
    protocol_history.to_csv(PROTOCOL_HISTORY_PATH, index=False)

    print(f"\nSaved pool history: {POOL_HISTORY_PATH} ({len(pool_history)} rows)")
    print(f"Saved protocol TVL: {PROTOCOL_HISTORY_PATH} ({len(protocol_history)} rows)")
    if modelled_count:
        print(f"NOTE: {modelled_count} pools used modelled fallback data (±5% noise around current TVL).")
    else:
        print("All pool histories were fetched from DeFiLlama pool chart API.")

    # Save hourly data if we got it
    if not hourly_df.empty:
        hourly_df.to_csv(POOL_HISTORY_HOURLY_PATH, index=False)
        print(f"Saved hourly pool data: {POOL_HISTORY_HOURLY_PATH} ({len(hourly_df)} rows)")
    else:
        print("Hourly pool data: not available from The Graph (daily data used).")


if __name__ == "__main__":
    asyncio.run(main())
