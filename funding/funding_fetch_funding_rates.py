"""
fetch_funding_rates.py - Fetches funding rates from multiple exchanges.

Collects current and recent historical funding rates from Binance, Bybit, OKX,
and Hyperliquid for 30+ perpetual futures contracts.
"""

import json
import time
import csv
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(exist_ok=True)

# Universe of 30+ perpetual futures symbols (common across exchanges)
UNIVERSE = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK",
    "MATIC", "UNI", "LTC", "ATOM", "FIL", "APT", "ARB", "OP", "NEAR", "SUI",
    "SEI", "TIA", "INJ", "FET", "RNDR", "WLD", "PEPE", "WIF", "JUP", "AAVE",
    "MKR", "CRV", "RUNE", "ALGO", "SAND",
]

# Exchange-specific symbol mapping
BINANCE_MAP = {s: f"{s}USDT" for s in UNIVERSE}
BYBIT_MAP = {s: f"{s}USDT" for s in UNIVERSE}
OKX_MAP = {s: f"{s}-USDT-SWAP" for s in UNIVERSE}


def api_get(url: str, headers: dict = None) -> dict:
    """Simple GET request returning parsed JSON."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def api_post(url: str, payload: dict) -> dict:
    """Simple POST request returning parsed JSON."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def safe_float(value, default: float = 0.0) -> float:
    """Convert API values to float without raising on missing data."""
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_binance_ticker_map() -> dict:
    """Fetch 24h ticker data from Binance Futures keyed by symbol."""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    return {item["symbol"]: item for item in api_get(url)}


def fetch_binance_funding() -> list[dict]:
    """Fetch current funding rates from Binance Futures."""
    print("  [Binance] Fetching funding rates...")
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    data = api_get(url)
    tickers = fetch_binance_ticker_map()
    results = []
    symbol_to_coin = {v: k for k, v in BINANCE_MAP.items()}
    for item in data:
        sym = item["symbol"]
        if sym in symbol_to_coin:
            ticker = tickers.get(sym, {})
            results.append({
                "exchange": "binance",
                "coin": symbol_to_coin[sym],
                "symbol": sym,
                "funding_rate": float(item["lastFundingRate"]),
                "next_funding_time": int(item["nextFundingTime"]),
                "mark_price": float(item["markPrice"]),
                "index_price": float(item["indexPrice"]),
                "funding_interval_hours": 8,
                "volume_24h_base": safe_float(ticker.get("volume")),
                "volume_24h_usd": safe_float(ticker.get("quoteVolume")),
            })
    print(f"  [Binance] Got {len(results)} symbols")
    return results


def fetch_bybit_funding() -> list[dict]:
    """Fetch current funding rates from Bybit."""
    print("  [Bybit] Fetching funding rates...")
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    data = api_get(url)
    results = []
    symbol_to_coin = {v: k for k, v in BYBIT_MAP.items()}
    for item in data.get("result", {}).get("list", []):
        sym = item["symbol"]
        if sym in symbol_to_coin:
            fr = item.get("fundingRate")
            if fr:
                results.append({
                    "exchange": "bybit",
                    "coin": symbol_to_coin[sym],
                    "symbol": sym,
                    "funding_rate": float(fr),
                    "next_funding_time": int(item.get("nextFundingTime", 0)),
                    "mark_price": safe_float(item.get("markPrice")),
                    "index_price": safe_float(item.get("indexPrice")),
                    "funding_interval_hours": 8,
                    "volume_24h_base": safe_float(item.get("volume24h")),
                    "volume_24h_usd": safe_float(item.get("turnover24h")),
                })
    print(f"  [Bybit] Got {len(results)} symbols")
    return results


def fetch_okx_funding() -> list[dict]:
    """Fetch current funding rates from OKX."""
    print("  [OKX] Fetching funding rates...")
    results = []
    for coin in UNIVERSE:
        inst_id = OKX_MAP[coin]
        url = f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}"
        try:
            data = api_get(url)
            items = data.get("data", [])
            if items:
                item = items[0]
                ticker = api_get(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}")
                ticker_item = ticker.get("data", [{}])[0]
                results.append({
                    "exchange": "okx",
                    "coin": coin,
                    "symbol": inst_id,
                    "funding_rate": float(item["fundingRate"]),
                    "next_funding_time": int(item.get("nextFundingTime", 0)),
                    "mark_price": safe_float(ticker_item.get("last")),
                    "index_price": 0.0,
                    "funding_interval_hours": 8,
                    "volume_24h_base": safe_float(ticker_item.get("vol24h")),
                    "volume_24h_usd": safe_float(ticker_item.get("volCcy24h")),
                })
        except Exception:
            pass  # symbol may not exist on OKX
    print(f"  [OKX] Got {len(results)} symbols")
    return results


def fetch_hyperliquid_funding() -> list[dict]:
    """Fetch current funding rates from Hyperliquid."""
    print("  [Hyperliquid] Fetching funding rates...")
    data = api_post("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
    meta = data[0]["universe"]
    contexts = data[1]

    coin_set = set(UNIVERSE)
    results = []
    for asset_meta, ctx in zip(meta, contexts):
        coin = asset_meta["name"]
        if coin in coin_set:
            results.append({
                "exchange": "hyperliquid",
                "coin": coin,
                "symbol": coin,
                "funding_rate": float(ctx["funding"]),
                "next_funding_time": 0,
                "mark_price": safe_float(ctx.get("markPx")),
                "index_price": safe_float(ctx.get("oraclePx")),
                "funding_interval_hours": 1,  # Hyperliquid uses 1h funding
                "volume_24h_base": float("nan"),
                "volume_24h_usd": safe_float(ctx.get("dayNtlVlm"), float("nan")),
            })
    print(f"  [Hyperliquid] Got {len(results)} symbols")
    return results


def fetch_binance_historical_funding(symbol: str, limit: int = 100) -> list[dict]:
    """Fetch historical funding rate data from Binance for a given symbol."""
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit={limit}"
    data = api_get(url)
    return [{
        "timestamp": int(item["fundingTime"]),
        "funding_rate": float(item["fundingRate"]),
    } for item in data]


def save_current_rates(all_rates: list[dict]):
    """Save all current funding rates to CSV."""
    csv_path = DATA_DIR / "funding_funding_rates.csv"
    ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    fieldnames = ["fetch_timestamp", "fetch_datetime", "exchange", "coin", "symbol",
                  "funding_rate", "funding_rate_annualised", "next_funding_time",
                  "mark_price", "index_price", "funding_interval_hours",
                  "volume_24h_base", "volume_24h_usd"]

    rows = []
    for r in all_rates:
        # Annualise: rate_per_period * periods_per_year
        periods_per_year = 365 * 24 / r["funding_interval_hours"]
        r["funding_rate_annualised"] = r["funding_rate"] * periods_per_year
        r["fetch_timestamp"] = ts
        r["fetch_datetime"] = dt
        rows.append(r)

    pd.DataFrame(rows, columns=fieldnames).to_csv(csv_path, index=False)

    print(f"\n  Saved {len(rows)} rates to {csv_path}")
    return rows


def save_historical_funding(coin: str, exchange: str, history: list[dict]):
    """Save historical funding rates to CSV."""
    csv_path = DATA_DIR / "funding_funding_rates_history.csv"
    fieldnames = ["exchange", "coin", "timestamp", "datetime", "funding_rate"]

    rows = []
    for h in history:
        rows.append({
            "exchange": exchange,
            "coin": coin,
            "timestamp": h["timestamp"],
            "datetime": datetime.fromtimestamp(h["timestamp"] / 1000, tz=timezone.utc).isoformat(),
            "funding_rate": h["funding_rate"],
        })

    file_exists = csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

    return len(rows)


def main():
    print("=" * 60)
    print("Cross-Exchange Funding Rate Collector")
    print("=" * 60)
    print(f"Universe: {len(UNIVERSE)} coins")
    print(f"Exchanges: Binance, Bybit, OKX, Hyperliquid\n")

    history_path = DATA_DIR / "funding_funding_rates_history.csv"
    if history_path.exists():
        history_path.unlink()

    # Fetch current rates from all exchanges
    all_rates = []
    all_rates.extend(fetch_binance_funding())
    all_rates.extend(fetch_bybit_funding())
    all_rates.extend(fetch_okx_funding())
    all_rates.extend(fetch_hyperliquid_funding())

    save_current_rates(all_rates)

    # Fetch historical funding for selected coins from Binance (for backtest)
    print("\n  Fetching historical funding rates from Binance...")
    hist_coins = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
                  "DOT", "LINK", "MATIC", "UNI", "LTC", "ATOM", "ARB", "OP"]
    total_hist = 0
    for coin in hist_coins:
        try:
            history = fetch_binance_historical_funding(BINANCE_MAP[coin], limit=500)
            n = save_historical_funding(coin, "binance", history)
            total_hist += n
            time.sleep(0.2)  # rate limit
        except Exception as e:
            print(f"    {coin}: failed ({e})")
    print(f"  Saved {total_hist} historical records")

    print("\n" + "=" * 60)
    print("DATA COLLECTION COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
