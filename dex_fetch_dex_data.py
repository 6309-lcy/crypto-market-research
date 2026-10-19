"""
fetch_dex_data.py - Fetches DEX pool data from public subgraph APIs.

Uses The Graph hosted subgraphs for Uniswap v3 and Curve on Ethereum.
Also uses DEX Screener and CoinGecko public APIs for supplementary data.
"""

import asyncio
import aiohttp
import json
import csv
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(exist_ok=True)

# ─── Subgraph endpoints ────────────────────────────────────────────────────
UNISWAP_V3_SUBGRAPH = "https://gateway.thegraph.com/api/subgraphs/id/5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
# Fallback: DeFiLlama / CoinGecko / DEXScreener

PAIRS = {
    "ETH_USDC": {
        "coingecko_id": "ethereum",
        "uniswap_token0": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "uniswap_token1": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    },
    "WBTC_USDC": {
        "coingecko_id": "wrapped-bitcoin",
        "uniswap_token0": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
        "uniswap_token1": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    },
}


async def fetch_uniswap_pools(session: aiohttp.ClientSession) -> list:
    """Fetch Uniswap v3 pool data via DeFiLlama."""
    print("  [Uniswap v3] Fetching pool data via DeFiLlama...")
    pools = []
    try:
        url = "https://yields.llama.fi/pools"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
            all_pools = data.get("data", [])
            # Filter for Uniswap v3 on Ethereum with our pairs
            for pool in all_pools:
                project = pool.get("project", "")
                chain = pool.get("chain", "")
                symbol = pool.get("symbol", "")
                if chain != "Ethereum":
                    continue
                if "uniswap" not in project.lower():
                    continue
                # Check if pool matches our pairs
                sym_upper = symbol.upper()
                if any(t in sym_upper for t in ["WETH-USDC", "ETH-USDC", "USDC-WETH", "USDC-ETH"]):
                    pools.append({
                        "dex": "uniswap_v3",
                        "pair": "ETH_USDC",
                        "pool_id": pool.get("pool", ""),
                        "tvl_usd": pool.get("tvlUsd", 0),
                        "apy": pool.get("apy", 0),
                        "volume_1d": pool.get("volumeUsd1d", 0),
                        "volume_7d": pool.get("volumeUsd7d", 0),
                        "fee_tier": symbol,
                    })
                elif any(t in sym_upper for t in ["WBTC-USDC", "USDC-WBTC", "BTC-USDC"]):
                    pools.append({
                        "dex": "uniswap_v3",
                        "pair": "WBTC_USDC",
                        "pool_id": pool.get("pool", ""),
                        "tvl_usd": pool.get("tvlUsd", 0),
                        "apy": pool.get("apy", 0),
                        "volume_1d": pool.get("volumeUsd1d", 0),
                        "volume_7d": pool.get("volumeUsd7d", 0),
                        "fee_tier": symbol,
                    })
        print(f"  [Uniswap v3] Found {len(pools)} relevant pools")
    except Exception as e:
        print(f"  [Uniswap v3] Error: {e}")
    return pools


async def fetch_curve_pools(session: aiohttp.ClientSession) -> list:
    """Fetch Curve pool data via DeFiLlama."""
    print("  [Curve] Fetching pool data via DeFiLlama...")
    pools = []
    try:
        url = "https://yields.llama.fi/pools"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
            all_pools = data.get("data", [])
            for pool in all_pools:
                project = pool.get("project", "")
                chain = pool.get("chain", "")
                symbol = pool.get("symbol", "")
                if chain != "Ethereum":
                    continue
                if "curve" not in project.lower():
                    continue
                sym_upper = symbol.upper()
                if any(t in sym_upper for t in ["ETH", "WETH"]) and "USDC" in sym_upper:
                    pools.append({
                        "dex": "curve",
                        "pair": "ETH_USDC",
                        "pool_id": pool.get("pool", ""),
                        "tvl_usd": pool.get("tvlUsd", 0),
                        "apy": pool.get("apy", 0),
                        "volume_1d": pool.get("volumeUsd1d", 0),
                        "volume_7d": pool.get("volumeUsd7d", 0),
                        "fee_tier": symbol,
                    })
                elif any(t in sym_upper for t in ["WBTC", "BTC"]) and "USDC" in sym_upper:
                    pools.append({
                        "dex": "curve",
                        "pair": "WBTC_USDC",
                        "pool_id": pool.get("pool", ""),
                        "tvl_usd": pool.get("tvlUsd", 0),
                        "apy": pool.get("apy", 0),
                        "volume_1d": pool.get("volumeUsd1d", 0),
                        "volume_7d": pool.get("volumeUsd7d", 0),
                        "fee_tier": symbol,
                    })
        print(f"  [Curve] Found {len(pools)} relevant pools")
    except Exception as e:
        print(f"  [Curve] Error: {e}")
    return pools


async def fetch_sushiswap_pools(session: aiohttp.ClientSession) -> list:
    """Fetch SushiSwap pools via DeFiLlama for additional DEX coverage."""
    print("  [SushiSwap] Fetching pool data via DeFiLlama...")
    pools = []
    try:
        url = "https://yields.llama.fi/pools"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
            all_pools = data.get("data", [])
            for pool in all_pools:
                project = pool.get("project", "")
                chain = pool.get("chain", "")
                symbol = pool.get("symbol", "")
                if chain != "Ethereum":
                    continue
                if "sushi" not in project.lower():
                    continue
                sym_upper = symbol.upper()
                if any(t in sym_upper for t in ["WETH-USDC", "ETH-USDC", "USDC-WETH"]):
                    pools.append({
                        "dex": "sushiswap",
                        "pair": "ETH_USDC",
                        "pool_id": pool.get("pool", ""),
                        "tvl_usd": pool.get("tvlUsd", 0),
                        "apy": pool.get("apy", 0),
                        "volume_1d": pool.get("volumeUsd1d", 0),
                        "volume_7d": pool.get("volumeUsd7d", 0),
                        "fee_tier": symbol,
                    })
                elif any(t in sym_upper for t in ["WBTC-USDC", "USDC-WBTC"]):
                    pools.append({
                        "dex": "sushiswap",
                        "pair": "WBTC_USDC",
                        "pool_id": pool.get("pool", ""),
                        "tvl_usd": pool.get("tvlUsd", 0),
                        "apy": pool.get("apy", 0),
                        "volume_1d": pool.get("volumeUsd1d", 0),
                        "volume_7d": pool.get("volumeUsd7d", 0),
                        "fee_tier": symbol,
                    })
        print(f"  [SushiSwap] Found {len(pools)} relevant pools")
    except Exception as e:
        print(f"  [SushiSwap] Error: {e}")
    return pools


async def fetch_historical_tvl(session: aiohttp.ClientSession) -> list:
    """Fetch 30-day historical TVL for DEX protocols via DeFiLlama."""
    print("\n  Fetching historical TVL for DEX protocols...")
    protocols = ["uniswap", "curve-dex", "sushiswap"]
    records = []
    for proto in protocols:
        try:
            url = f"https://api.llama.fi/protocol/{proto}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                # Get Ethereum chain TVL
                chain_tvls = data.get("chainTvls", {})
                eth_tvl = chain_tvls.get("Ethereum", {}).get("tvl", [])
                # Last 30 days
                for entry in eth_tvl[-720:]:  # hourly data, ~30 days
                    records.append({
                        "protocol": proto,
                        "date": datetime.fromtimestamp(entry["date"], tz=timezone.utc).strftime("%Y-%m-%d"),
                        "tvl_usd": entry.get("totalLiquidityUSD", 0),
                    })
            print(f"  [{proto}] Got {len([r for r in records if r['protocol'] == proto])} TVL records")
        except Exception as e:
            print(f"  [{proto}] Error: {e}")
    return records


async def main():
    print("=" * 60)
    print("DEX LIQUIDITY FRAGMENTATION DATA COLLECTOR")
    print("=" * 60)
    print("Pairs: ETH/USDC, WBTC/USDC")
    print("DEXs: Uniswap v3, Curve, SushiSwap")

    async with aiohttp.ClientSession() as session:
        # Fetch current pool data
        uni_pools = await fetch_uniswap_pools(session)
        curve_pools = await fetch_curve_pools(session)
        sushi_pools = await fetch_sushiswap_pools(session)

        all_pools = uni_pools + curve_pools + sushi_pools
        print(f"\n  Total pools found: {len(all_pools)}")

        # Save pool data
        if all_pools:
            import pandas as pd
            df = pd.DataFrame(all_pools)
            df.to_csv(DATA_DIR / "dex_dex_pools.csv", index=False)
            print(f"  Saved to {DATA_DIR / 'dex_dex_pools.csv'}")

        # Fetch historical TVL
        tvl_records = await fetch_historical_tvl(session)
        if tvl_records:
            import pandas as pd
            tvl_df = pd.DataFrame(tvl_records)
            tvl_df.to_csv(DATA_DIR / "dex_historical_tvl.csv", index=False)
            print(f"  Saved {len(tvl_df)} TVL records to {DATA_DIR / 'dex_historical_tvl.csv'}")

    print("\n" + "=" * 60)
    print("DATA COLLECTION COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
