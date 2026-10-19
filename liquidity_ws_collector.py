"""
ws_collector.py — Long-running WebSocket-based L2 order book collector for Task 3.

Connects to Binance, OKX, and Bybit WebSocket feeds for BTC/USDT perpetuals and
appends depth snapshots to liquidity_ws_orderbook_snapshots.csv every SNAPSHOT_INTERVAL
seconds.  Runs indefinitely with automatic reconnection.

Usage (detached on Windows):
    start /b pythonw src\\ws_collector.py
    # or equivalently:
    start "" /b "C:\\...\\python.exe" src\\ws_collector.py
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

OUTPUT_CSV = DATA_DIR / "liquidity_ws_orderbook_snapshots.csv"
LOG_FILE = DATA_DIR / "liquidity_ws_collector.log"

NUM_LEVELS = 20
SNAPSHOT_INTERVAL = 2  # seconds between rows written per exchange
RECONNECT_DELAY = 5    # seconds before attempting reconnection

FIELDNAMES = ["timestamp", "exchange", "side", "level", "price", "quantity", "mid_price"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ws_collector.task3")


def make_session() -> aiohttp.ClientSession:
    """Return a ClientSession that uses the system DNS resolver (avoids aiodns issues)."""
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    return aiohttp.ClientSession(connector=connector)


# ── Local order book ─────────────────────────────────────────────────────────

class LocalBook:
    """Maintains a local L2 book from snapshot + delta messages."""

    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply_snapshot(self, bids, asks):
        self.bids = {float(p): float(q) for p, q in bids}
        self.asks = {float(p): float(q) for p, q in asks}

    def apply_delta(self, bids, asks):
        for p, q in bids:
            p, q = float(p), float(q)
            if q == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = q
        for p, q in asks:
            p, q = float(p), float(q)
            if q == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = q

    def get_snapshot(self, levels=NUM_LEVELS):
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[:levels]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:levels]
        return bids, asks

    def mid(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (max(self.bids) + min(self.asks)) / 2


# ── Exchange WebSocket coroutines ────────────────────────────────────────────

async def binance_ws(shared: dict):
    """Binance futures depth20 stream — sends full snapshots directly."""
    url = "wss://fstream.binance.com/ws/btcusdt@depth20@100ms"
    while True:
        try:
            async with make_session() as session:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    log.info("[binance] WebSocket connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            bids = [(float(p), float(q)) for p, q in data.get("bids", [])[:NUM_LEVELS]]
                            asks = [(float(p), float(q)) for p, q in data.get("asks", [])[:NUM_LEVELS]]
                            if bids and asks:
                                shared["binance"] = {
                                    "bids": bids,
                                    "asks": asks,
                                    "mid": (bids[0][0] + asks[0][0]) / 2,
                                }
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            log.warning(f"[binance] WS closed/error: {msg.type}")
                            break
        except Exception as exc:
            log.warning(f"[binance] Error: {exc}. Reconnecting in {RECONNECT_DELAY}s")
        await asyncio.sleep(RECONNECT_DELAY)


async def bybit_ws(shared: dict):
    """Bybit linear orderbook.20 stream — snapshot then deltas."""
    url = "wss://stream.bybit.com/v5/public/linear"
    book = LocalBook()
    while True:
        try:
            async with make_session() as session:
                async with session.ws_connect(url, heartbeat=20) as ws:
                    await ws.send_json({"op": "subscribe", "args": ["orderbook.20.BTCUSDT"]})
                    log.info("[bybit] WebSocket connected, subscription sent")
                    last_ping = time.time()
                    async for msg in ws:
                        # Application-level ping keepalive
                        if time.time() - last_ping > 20:
                            await ws.send_json({"op": "ping"})
                            last_ping = time.time()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("op") in ("ping", "pong"):
                                continue
                            topic = data.get("topic", "")
                            if "orderbook" not in topic:
                                continue
                            d = data.get("data", {})
                            msg_type = data.get("type", "")
                            if msg_type == "snapshot":
                                book.apply_snapshot(d.get("b", []), d.get("a", []))
                            elif msg_type == "delta":
                                book.apply_delta(d.get("b", []), d.get("a", []))
                            bids, asks = book.get_snapshot()
                            if bids and asks:
                                shared["bybit"] = {"bids": bids, "asks": asks, "mid": book.mid()}
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            log.warning(f"[bybit] WS closed/error: {msg.type}")
                            book = LocalBook()
                            break
        except Exception as exc:
            log.warning(f"[bybit] Error: {exc}. Reconnecting in {RECONNECT_DELAY}s")
            book = LocalBook()
        await asyncio.sleep(RECONNECT_DELAY)


async def okx_ws(shared: dict):
    """OKX BTC-USDT-SWAP books channel — snapshot then deltas, 20 levels."""
    url = "wss://ws.okx.com:8443/ws/v5/public"
    book = LocalBook()
    # OKX contract size: 1 contract = 0.001 BTC
    CONTRACT_TO_BTC = 0.001
    while True:
        try:
            async with make_session() as session:
                async with session.ws_connect(url, heartbeat=25) as ws:
                    await ws.send_json({
                        "op": "subscribe",
                        "args": [{"channel": "books", "instId": "BTC-USDT-SWAP"}],
                    })
                    log.info("[okx] WebSocket connected, subscription sent")
                    last_ping = time.time()
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            text = msg.data
                            if text == "pong":
                                continue
                            # Application-level ping
                            if time.time() - last_ping > 25:
                                await ws.send_str("ping")
                                last_ping = time.time()
                            data = json.loads(text)
                            if data.get("event") in ("subscribe", "error"):
                                continue
                            items = data.get("data", [])
                            action = data.get("action", "")
                            if not items or data.get("arg", {}).get("channel") != "books":
                                continue
                            item = items[0]
                            if action == "snapshot":
                                bids_raw = [(b[0], float(b[1]) * CONTRACT_TO_BTC)
                                            for b in item.get("bids", [])[:NUM_LEVELS]]
                                asks_raw = [(a[0], float(a[1]) * CONTRACT_TO_BTC)
                                            for a in item.get("asks", [])[:NUM_LEVELS]]
                                book.apply_snapshot(bids_raw, asks_raw)
                            elif action == "update":
                                bids_raw = [(b[0], float(b[1]) * CONTRACT_TO_BTC)
                                            for b in item.get("bids", [])]
                                asks_raw = [(a[0], float(a[1]) * CONTRACT_TO_BTC)
                                            for a in item.get("asks", [])]
                                book.apply_delta(bids_raw, asks_raw)
                            bids, asks = book.get_snapshot()
                            if bids and asks:
                                shared["okx"] = {"bids": bids, "asks": asks, "mid": book.mid()}
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            log.warning(f"[okx] WS closed/error: {msg.type}")
                            book = LocalBook()
                            break
        except Exception as exc:
            log.warning(f"[okx] Error: {exc}. Reconnecting in {RECONNECT_DELAY}s")
            book = LocalBook()
        await asyncio.sleep(RECONNECT_DELAY)


# ── Snapshot writer ──────────────────────────────────────────────────────────

async def snapshot_writer(shared: dict):
    """Periodically flush the latest shared state to the append-mode CSV."""
    write_header = not OUTPUT_CSV.exists()
    log.info(f"Output: {OUTPUT_CSV}  (append={'no' if write_header else 'yes'})")
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

            for exchange in ("binance", "bybit", "okx"):
                state = shared.get(exchange)
                if not state:
                    continue
                bids = state["bids"]
                asks = state["asks"]
                mid = state["mid"]
                for i, (price, qty) in enumerate(bids):
                    writer.writerow([ts, exchange, "bid", i + 1, price, qty, mid])
                for i, (price, qty) in enumerate(asks):
                    writer.writerow([ts, exchange, "ask", i + 1, price, qty, mid])
                total_rows += len(bids) + len(asks)

            snapshots += 1
            f.flush()

            if snapshots % 150 == 0:  # log every ~5 minutes
                elapsed_h = (time.time() - start_time) / 3600
                log.info(
                    f"Progress: {snapshots} snapshots | {elapsed_h:.2f}h elapsed | "
                    f"{total_rows:,} rows appended"
                )


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 60)
    log.info("TASK 3 — WebSocket L2 Order Book Collector")
    log.info("Exchanges: Binance (WS), Bybit (WS), OKX (WS)")
    log.info("Pair: BTC/USDT Perpetual | 20 levels | 2 s snapshots")
    log.info(f"Output: {OUTPUT_CSV}")
    log.info("=" * 60)

    shared: dict = {}
    await asyncio.gather(
        binance_ws(shared),
        bybit_ws(shared),
        okx_ws(shared),
        snapshot_writer(shared),
    )


if __name__ == "__main__":
    asyncio.run(main())
