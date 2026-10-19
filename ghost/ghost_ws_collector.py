"""
ws_collector.py — Long-running WebSocket-based L2 order book collector for Task 5.

Connects to Binance and Hyperliquid WebSocket feeds for BTC/USDT perpetuals and
appends depth snapshots to ghost_ws_orderbook_snapshots.csv every SNAPSHOT_INTERVAL
seconds.  Runs indefinitely; suitable for multi-day spoofing analysis data collection.

Usage (detached on Windows):
    start /b pythonw src\\ws_collector.py
    # log output goes to data\\ws_collector.log
"""

import asyncio
import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(exist_ok=True)

OUTPUT_CSV = DATA_DIR / "ghost_ws_orderbook_snapshots.csv"
LOG_FILE = DATA_DIR / "ghost_ws_collector.log"

NUM_LEVELS = 10
SNAPSHOT_INTERVAL = 1  # 1 s for fine-grained ghost detection
RECONNECT_DELAY = 5

FIELDNAMES = [
    "timestamp", "exchange", "side", "level",
    "price", "quantity", "notional_usd", "mid_price",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ws_collector.task5")


def make_session() -> aiohttp.ClientSession:
    """Return a ClientSession that uses the system DNS resolver (avoids aiodns issues)."""
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    return aiohttp.ClientSession(connector=connector)


# ── Exchange WebSocket coroutines ────────────────────────────────────────────

async def binance_ws(shared: dict):
    """Binance futures depth10 stream — full snapshots."""
    url = "wss://fstream.binance.com/ws/btcusdt@depth10@100ms"
    while True:
        try:
            async with make_session() as session:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    log.info("[binance] WebSocket connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            bids = [(float(p), float(q))
                                    for p, q in data.get("bids", [])[:NUM_LEVELS]]
                            asks = [(float(p), float(q))
                                    for p, q in data.get("asks", [])[:NUM_LEVELS]]
                            if bids and asks:
                                mid = (bids[0][0] + asks[0][0]) / 2
                                shared["binance"] = {"bids": bids, "asks": asks, "mid": mid}
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            log.warning(f"[binance] WS closed/error: {msg.type}")
                            break
        except Exception as exc:
            log.warning(f"[binance] Error: {exc}. Reconnecting in {RECONNECT_DELAY}s")
        await asyncio.sleep(RECONNECT_DELAY)


async def hyperliquid_ws(shared: dict):
    """Hyperliquid L2 book WebSocket stream."""
    url = "wss://api.hyperliquid.xyz/ws"
    subscribe_msg = {
        "method": "subscribe",
        "subscription": {"type": "l2Book", "coin": "BTC"},
    }
    while True:
        try:
            async with make_session() as session:
                async with session.ws_connect(url, heartbeat=20) as ws:
                    await ws.send_json(subscribe_msg)
                    log.info("[hyperliquid] WebSocket connected, subscription sent")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("channel") != "l2Book":
                                continue
                            book_data = data.get("data", {})
                            levels = book_data.get("levels", [[], []])
                            bids = [(float(l["px"]), float(l["sz"]))
                                    for l in levels[0][:NUM_LEVELS]]
                            asks = [(float(l["px"]), float(l["sz"]))
                                    for l in levels[1][:NUM_LEVELS]]
                            if bids and asks:
                                mid = (bids[0][0] + asks[0][0]) / 2
                                shared["hyperliquid"] = {"bids": bids, "asks": asks, "mid": mid}
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            log.warning(f"[hyperliquid] WS closed/error: {msg.type}")
                            break
        except Exception as exc:
            log.warning(f"[hyperliquid] Error: {exc}. Reconnecting in {RECONNECT_DELAY}s")
        await asyncio.sleep(RECONNECT_DELAY)


# ── Snapshot writer ──────────────────────────────────────────────────────────

async def snapshot_writer(shared: dict):
    """Flush the latest shared state to the append-mode CSV every second."""
    write_header = not OUTPUT_CSV.exists()
    log.info(f"Output: {OUTPUT_CSV}  (header_written={not write_header})")
    total_rows = 0
    snapshots = 0
    start_time = time.time()

    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(FIELDNAMES)

        while True:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            ts = datetime.now(timezone.utc).isoformat()

            for exchange in ("binance", "hyperliquid"):
                state = shared.get(exchange)
                if not state:
                    continue
                bids = state["bids"]
                asks = state["asks"]
                mid = state["mid"]
                for i, (price, qty) in enumerate(bids):
                    writer.writerow([ts, exchange, "bid", i + 1,
                                     price, qty, qty * price, mid])
                for i, (price, qty) in enumerate(asks):
                    writer.writerow([ts, exchange, "ask", i + 1,
                                     price, qty, qty * price, mid])
                total_rows += len(bids) + len(asks)

            snapshots += 1
            f.flush()

            if snapshots % 300 == 0:  # log every 5 minutes
                elapsed_h = (time.time() - start_time) / 3600
                elapsed_d = elapsed_h / 24
                log.info(
                    f"Progress: {snapshots} snapshots | {elapsed_d:.3f} days elapsed | "
                    f"{total_rows:,} rows appended | target: 7-10 days"
                )


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 60)
    log.info("TASK 5 — WebSocket L2 Order Book Collector (Multi-Day)")
    log.info("Exchanges: Binance (WS), Hyperliquid (WS)")
    log.info("Pair: BTC/USDT Perpetual | 10 levels | 1 s snapshots")
    log.info(f"Output: {OUTPUT_CSV}  [APPEND MODE — accumulates for 7-10 days]")
    log.info("=" * 60)

    shared: dict = {}
    await asyncio.gather(
        binance_ws(shared),
        hyperliquid_ws(shared),
        snapshot_writer(shared),
    )


if __name__ == "__main__":
    asyncio.run(main())
