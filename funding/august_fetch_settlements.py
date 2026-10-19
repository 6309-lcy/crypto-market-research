"""Download public, settled funding events; no credentials or third-party packages.

Run with Python 3.10+: python -B august_fetch_settlements.py
The CSV includes inference history from --history-start, with the requested
analysis window separately recorded in the JSON audit. Included observations
supply contract identities by default; --archive overrides this with raw JSONL.
All observation roles are included, regardless of execution book availability.
Indicative rates and intervals from identity sources are never used.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

VENUES = ("binance", "bybit", "okx", "hyperliquid")
ENDPOINTS = {
    "binance": "https://fapi.binance.com/fapi/v1/fundingRate",
    "bybit": "https://api.bybit.com/v5/market/funding/history",
    "okx": "https://www.okx.com/api/v5/public/funding-rate-history",
    "hyperliquid": "https://api.hyperliquid.xyz/info",
}
FIELDS = ("exchange", "base", "symbol", "settlement_time", "funding_rate",
          "mark_price", "interval_hours", "source_endpoint")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("CLI dates must include UTC timezone")
    return int(parsed.timestamp() * 1000)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite API numeric field")
    return str(value)


def api_symbol(exchange, symbol):
    if exchange == "hyperliquid":
        return symbol
    if "/" in symbol:
        pair, collateral = symbol.split(":")
        base, quote = pair.split("/")
        if quote != "USDT" or collateral != "USDT":
            raise ValueError("unsupported snapshot quote or collateral")
        return f"{base}-{quote}-SWAP" if exchange == "okx" else base + quote
    return symbol


def discover(archive):
    contracts = defaultdict(set)
    file_audit = []
    rows = 0
    for path in sorted(archive.rglob("*.jsonl")):
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as handle:
            for line in handle:
                digest.update(line)
                if not line.strip():
                    continue
                item = json.loads(line)
                exchange = item["exchange"]
                if exchange not in VENUES:
                    continue
                base, symbol = str(item["base"]), str(item["symbol"])
                contracts[(exchange, base)].add(symbol)
                count += 1
        rows += count
        file_audit.append({"file": path.relative_to(archive).as_posix(),
                           "sha256": digest.hexdigest(), "rows": count})
    if not contracts:
        raise ValueError("archive contains no contracts")
    return contracts, {"file_count": len(file_audit), "rows_scanned": rows,
                       "files": file_audit}


def discover_observations(observations):
    data = observations.read_bytes()
    contracts = defaultdict(set)
    rows = 0
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    if not {"exchange", "base", "symbol"}.issubset(reader.fieldnames or ()):
        raise ValueError("observations requires exchange, base and symbol columns")
    for item in reader:
        exchange = item["exchange"]
        if exchange not in VENUES:
            continue
        base, symbol = item["base"], item["symbol"]
        if not base or not symbol:
            raise ValueError("observations contains an empty contract identity")
        contracts[(exchange, base)].add(symbol)
        rows += 1
    if not contracts:
        raise ValueError("observations contains no contracts")
    return contracts, {"file": observations.name, "sha256": sha(data),
                       "rows_scanned": rows, "roles": "all"}


class PublicClient:
    def __init__(self, delay, attempts):
        self.delay = delay
        self.attempts = attempts
        self.lock = threading.Lock()
        self.next_request = 0.0

    def request(self, exchange, params, audit):
        endpoint = ENDPOINTS[exchange]
        post = exchange == "hyperliquid"
        payload = json.dumps(params).encode() if post else None
        url = endpoint if post else endpoint + "?" + urllib.parse.urlencode(params)
        entry = {"endpoint": endpoint, "method": "POST" if post else "GET",
                 "parameters": params.copy(), "attempts": []}
        audit.append(entry)
        for attempt in range(self.attempts):
            with self.lock:
                pause = max(0, self.next_request - time.monotonic())
                if pause:
                    time.sleep(pause)
                self.next_request = time.monotonic() + self.delay
            attempt_audit = {"fetch_utc": utc_now()}
            entry["attempts"].append(attempt_audit)
            try:
                request = urllib.request.Request(
                    url, data=payload, headers={"User-Agent": "public-funding-research/1.0",
                    "Content-Type": "application/json"}, method=entry["method"])
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                    attempt_audit.update(http_status=response.status,
                                         response_sha256=sha(body))
                data = json.loads(body)
                if exchange == "bybit":
                    if data.get("retCode") != 0:
                        attempt_audit["api_code"] = data.get("retCode")
                        raise ValueError("API rejected request")
                    data = data["result"]["list"]
                elif exchange == "okx":
                    if str(data.get("code")) != "0":
                        attempt_audit["api_code"] = data.get("code")
                        raise ValueError("API rejected request")
                    data = data["data"]
                if not isinstance(data, list):
                    raise ValueError("unexpected API response type")
                entry["response_rows"] = len(data)
                return data
            except urllib.error.HTTPError as exc:
                attempt_audit.update(http_status=exc.code, error="HTTPError")
                retry = exc.code in (408, 418, 429, 500, 502, 503, 504)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                attempt_audit["error"] = type(exc).__name__
                retry = True
            except (ValueError, KeyError, TypeError) as exc:
                attempt_audit["error"] = type(exc).__name__
                retry = "api_code" in attempt_audit and str(attempt_audit["api_code"]) in (
                    "10006", "10016", "50011", "50040")
            if not retry or attempt + 1 == self.attempts:
                raise RuntimeError("public_request_failed; see request audit")
            time.sleep(min(30, 2 ** (attempt + 1) * 2))
        raise RuntimeError("request attempts exhausted")


def infer_interval(events):
    times = sorted({row["_time"] for row in events})
    deltas = [b - a for a, b in zip(times, times[1:])]
    counts = Counter(round(delta / 1000) for delta in deltas)
    evidence = {"method": "modal observed consecutive spacing, >=3 supporting gaps, "
                ">=80% direct support, all gaps integer multiples within 2000ms",
                "events": len(times), "consecutive_gaps": len(deltas),
                "spacing_seconds_counts": {str(k): v for k, v in sorted(counts.items())},
                "tolerance_ms": 2000, "interval_hours": None, "missing_event_estimate": None}
    if not deltas:
        return None, evidence
    modal, support = counts.most_common(1)[0]
    close = [d for d in deltas if abs(d - modal * 1000) <= 2000]
    candidate = statistics.median(close)
    evidence.update(candidate_spacing_ms=candidate, direct_support=len(close),
                    direct_support_fraction=len(close) / len(deltas))
    multiples = [round(delta / candidate) for delta in deltas] if candidate > 0 else []
    robust = (len(close) >= 3 and len(close) / len(deltas) >= 0.8 and
              all(n >= 1 and abs(d - n * candidate) <= 2000
                  for d, n in zip(deltas, multiples)))
    if not robust:
        evidence["reason"] = "insufficient or inconsistent observed spacing"
        return None, evidence
    interval = candidate / 3600000
    evidence.update(interval_hours=interval,
                    missing_event_estimate=sum(n - 1 for n in multiples),
                    anomalous_gaps=[{"previous": iso(a), "next": iso(b),
                                     "spacing_ms": b - a}
                                    for a, b in zip(times, times[1:])
                                    if abs(b - a - candidate) > 2000])
    return interval, evidence


def fetch_contract(client, exchange, base, symbol, start, end, analysis_start):
    remote = api_symbol(exchange, symbol)
    record = {"exchange": exchange, "base": base, "symbol": symbol,
              "api_symbol": remote, "source_endpoint": ENDPOINTS[exchange],
              "fetch_started_utc": utc_now(), "requests": [], "errors": []}
    events = {}
    duplicates = conflicts = rejected = outside = 0
    cursor = start if exchange in ("binance", "hyperliquid") else end + 1
    try:
        for page in range(1000):
            if exchange == "binance":
                params = {"symbol": remote, "startTime": cursor, "endTime": end, "limit": 1000}
                time_key, rate_key, symbol_key = "fundingTime", "fundingRate", "symbol"
            elif exchange == "bybit":
                params = {"category": "linear", "symbol": remote, "startTime": start,
                          "endTime": cursor - 1, "limit": 200}
                time_key, rate_key, symbol_key = "fundingRateTimestamp", "fundingRate", "symbol"
            elif exchange == "okx":
                params = {"instId": remote, "after": cursor, "before": start - 1, "limit": 100}
                time_key, rate_key, symbol_key = "fundingTime", "realizedRate", "instId"
            else:
                params = {"type": "fundingHistory", "coin": remote,
                          "startTime": cursor, "endTime": end}
                time_key, rate_key, symbol_key = "time", "fundingRate", "coin"
            data = client.request(exchange, params, record["requests"])
            if not data:
                record["pagination_termination"] = "empty page"
                break
            page_times = []
            for item in data:
                try:
                    event_time = int(item[time_key])
                    page_times.append(event_time)
                    if item.get(symbol_key) != remote:
                        raise ValueError("response contract mismatch")
                    if not start <= event_time <= end:
                        outside += 1
                        continue
                    if exchange == "okx" and item.get("method") not in (None, "", "current_period", "next_period"):
                        raise ValueError("unknown OKX funding method")
                    rate = number(item[rate_key])
                    mark = item.get("markPrice")
                    mark = number(mark) if mark not in (None, "") else ""
                    row = {"exchange": exchange, "base": base, "symbol": symbol,
                           "settlement_time": iso(event_time), "funding_rate": rate,
                           "mark_price": mark, "interval_hours": "",
                           "source_endpoint": ENDPOINTS[exchange], "_time": event_time}
                    previous = events.get(event_time)
                    if previous:
                        duplicates += 1
                        if (previous["funding_rate"], previous["mark_price"]) != (rate, mark):
                            conflicts += 1
                    else:
                        events[event_time] = row
                except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
                    rejected += 1
            if not page_times:
                raise RuntimeError("page contained no usable timestamps")
            if exchange in ("binance", "hyperliquid"):
                following = max(page_times) + 1
                if following <= cursor:
                    raise RuntimeError("forward pagination did not advance")
                cursor = following
                reached = cursor > end
            else:
                following = min(page_times)
                if following >= cursor:
                    raise RuntimeError("backward pagination did not advance")
                cursor = following
                reached = cursor <= start
            if reached:
                record["pagination_termination"] = "requested boundary reached"
                break
        else:
            raise RuntimeError("pagination page cap reached")
    except RuntimeError as exc:
        record["errors"].append(str(exc))
    result = sorted(events.values(), key=lambda row: row["_time"])
    interval, evidence = infer_interval(result)
    for row in result:
        row["interval_hours"] = interval if interval is not None else ""
    analysis = [row for row in result if row["_time"] >= analysis_start]
    coverage = {"rows": len(result), "analysis_rows": len(analysis),
                "history_rows": len(result) - len(analysis),
                "first_settlement": result[0]["settlement_time"] if result else None,
                "last_settlement": result[-1]["settlement_time"] if result else None,
                "outside_window_rows_discarded": outside, "duplicate_events": duplicates,
                "conflicting_duplicate_events": conflicts, "rejected_rows": rejected}
    boundary_gaps = []
    if result and interval is not None:
        spacing = interval * 3600000
        if result[0]["_time"] - start > spacing + 2000:
            boundary_gaps.append("leading")
        if end - result[-1]["_time"] > spacing + 2000:
            boundary_gaps.append("trailing")
    record.update(coverage=coverage, interval_evidence=evidence, boundary_gaps=boundary_gaps,
                  fetch_finished_utc=utc_now())
    if not result:
        record["status"] = "fetch_failed" if record["errors"] else "no_settlements_returned"
    elif (record["errors"] or conflicts or rejected or interval is None or boundary_gaps
          or evidence["missing_event_estimate"]):
        record["status"] = "partial_or_unverified"
    else:
        record["status"] = "observed_spacing_complete"
    return result, record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-08-11T10:00:00Z")
    parser.add_argument("--end", default="2026-08-20T06:00:00Z")
    parser.add_argument("--history-start", default="2026-08-11T00:00:00Z")
    parser.add_argument("--observations", type=Path, default=Path(__file__).resolve().parent /
                        "august_observations.csv",
                        help="contract identities from all roles; default: adjacent august_observations.csv")
    parser.add_argument("--archive", type=Path, default=None,
                        help="optional raw JSONL archive; overrides --observations")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--request-delay", type=float, default=0.4)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3, 4, 5), default=3)
    args = parser.parse_args()
    start, end, history_start = map(timestamp, (args.start, args.end, args.history_start))
    if not history_start <= start <= end or not math.isfinite(args.request_delay) or args.request_delay < 0.25:
        parser.error("require history-start <= start <= end and request-delay >= 0.25 seconds")
    started = utc_now()
    archive_audit = None
    if args.archive is not None:
        contracts, archive_audit = discover(args.archive)
        identity_source = {"type": "archive", "audit": "archive"}
    else:
        contracts, observations_audit = discover_observations(args.observations)
        identity_source = {"type": "observations", **observations_audit}
    bases = sorted({base for exchange, base in contracts})
    records = []
    jobs = []
    for exchange in VENUES:
        for base in bases:
            symbols = contracts.get((exchange, base), set())
            if not symbols:
                records.append({"exchange": exchange, "base": base,
                                "status": "absent_from_snapshot", "symbols": []})
            for symbol in sorted(symbols):
                jobs.append((exchange, base, symbol))
    print(f"Snapshot: {len(bases)} distinct bases, {len(jobs)} venue contracts; "
          f"{len(records)} venue/base pairs absent", flush=True)
    client = PublicClient(args.request_delay, args.attempts)
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_contract, client, *job, history_start, end, start): job
                   for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                result, record = future.result()
            except Exception as exc:
                result = []
                record = {"exchange": job[0], "base": job[1], "symbol": job[2],
                          "status": "fetch_failed", "errors": [type(exc).__name__]}
            rows.extend(result)
            records.append(record)
            print(f"{job[0]} {job[1]}: {len(result)} events, {record['status']}", flush=True)
    rows.sort(key=lambda row: (row["exchange"], row["base"], row["symbol"], row["_time"]))
    keys = [(r["exchange"], r["base"], r["symbol"], r["_time"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise RuntimeError("output contains duplicate event keys")
    if any(not history_start <= row["_time"] <= end for row in rows):
        raise RuntimeError("output contains events outside retained window")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = buffer.getvalue().encode("utf-8")
    records.sort(key=lambda row: (row["exchange"], row["base"], row.get("symbol", "")))
    summary = {}
    for exchange in VENUES:
        subset = [r for r in rows if r["exchange"] == exchange]
        venue_records = [r for r in records if r["exchange"] == exchange]
        summary[exchange] = {
            "rows": len(subset), "analysis_rows": sum(r["_time"] >= start for r in subset),
            "contracts_with_events": len({r["symbol"] for r in subset}),
            "status_counts": dict(Counter(r["status"] for r in venue_records))}
    metadata = {
        "schema_version": 1, "fetch_started_utc": started, "fetch_finished_utc": utc_now(),
        "window": {"analysis_start_inclusive": iso(start), "end_inclusive": iso(end),
                   "retained_history_start_inclusive": iso(history_start)},
        "data_policy": "Only actual public funding history events. Snapshot rates, prices and "
                       "intervals are not copied. OKX uses realizedRate. Missing prices and "
                       "unverified intervals are empty. Timestamps retain original milliseconds.",
        "identity_policy": "Snapshot base and symbol preserved; multiplier prefixes are distinct. "
                           "Missing venue/base pairs are not mapped to differently named bases.",
        "interval_policy": "Inferred from retained actual settlement spacings only; completeness "
                           "means consistent observed spacing, not independent proof of schedule.",
        "request_policy": {"max_concurrency": args.workers, "min_request_spacing_seconds": args.request_delay,
                           "max_attempts": args.attempts, "timeout_seconds": 30,
                           "max_pages_per_contract": 1000},
        "endpoints": ENDPOINTS, "base_universe": bases, "archive": archive_audit,
        "identity_source": identity_source,
        "summary": summary, "contracts": records,
        "output": {"file": "august_settlements.csv", "sha256": sha(csv_bytes),
                   "rows": len(rows), "duplicate_event_keys": 0, "columns": list(FIELDS)},
        "script": {"file": Path(__file__).name, "sha256": sha(Path(__file__).read_bytes())},
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "august_settlements.csv").write_bytes(csv_bytes)
    (args.output_dir / "august_settlement_fetch.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    print(f"CSV SHA256 {sha(csv_bytes)}", flush=True)


if __name__ == "__main__":
    main()
