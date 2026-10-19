# Free matched execution data: BTC, September 1, 2026

[繁體中文說明](README.zh-TW.md)

This study acquires public historical order books and trades for **Bybit BTCUSDT and OKX BTC-USDT-SWAP**, using one identical UTC day: September 1 00:00 inclusive to September 2 00:00 exclusive. It supplements the frozen September funding dataset without changing that dataset.

Large original archives stay in a local cache outside the public repository. Download programs, source hashes, validation reports and minute-level coverage summaries are versioned here. ZIP and compressed source archives are not committed.

## Verified single-day coverage

| Venue | In-window book messages | In-window trades | Book minutes | Trade minutes | Largest book event/boundary gap |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | 863,930 | 2,017,359 | 1,440 | 1,440 | 299 ms |
| OKX | 5,858,458 | 2,660,697 | 1,440 | 1,440 | 537 ms |

Bybit reconstruction found no crossed, locked or empty books and no timestamp/identifier regressions. A duplicated closing snapshot identifier is outside the exclusive UTC window. OKX has 96 snapshots and its archive omits `seqId`, `prevSeqId` and checksum: timestamp coverage passes, but exact sequence continuity cannot be established. OKX trades have a prior timestamp-matched book for all 2,660,697 events; matching is a diagnostic, not a hypothetical maker fill.

Read [Bybit report](bybit_report.json), [OKX report](okx_report.json), [shared coverage and hashes](shared_report.json), and [minute diagnostics](bybit_minutes.csv)/[OKX minute diagnostics](okx_minutes.csv). Compressed source archives total approximately **661 MB** and remain local.

## Reproduce

From the repository root, Python 3.10+, with the normal research requirements installed:

```powershell
python execution/btc-2026-09-01/bybit_collect.py --cache ../market_data_cache/btc-2026-09-01/bybit
python execution/btc-2026-09-01/okx_collect.py --cache ../market_data_cache/btc-2026-09-01/okx
python execution/btc-2026-09-01/summarize.py
```

Read each program's `--help` for additional options. The cache contains the source archives for exact local replay; reports record SHA-256 hashes and public URLs. Cached content is checked rather than interpreted as newly fetched data. Full archive contents are parsed through streams, avoiding large uncompressed extraction copies.

## Source and timing conventions

Bybit provides a 200-level historical book archive and a separate daily trade archive. OKX provides a 400-level book archive and daily trades. **OKX trades use UTC+8 archive partitions**, so both September 1 and September 2 partitions are needed to cover the September 1 UTC day. Filtering is always based on actual event timestamps.

Book messages and trades are independently validated. Coverage summaries are diagnostics and do not replace raw book/trade messages for event replay. Native quantity units are retained: OKX contract quantities require historically valid contract-value metadata before conversion to BTC. The October metadata supplement alone does not prove the September contract value.

## Interpretation

A source archive being accessible is different from a verified complete replay. Reports check available sequence identifiers, resets, timestamps, duplicate and crossed books, and gaps. A monotonic identifier is not automatically a contiguous sequence: checks follow the semantics available in each message. Records before the UTC start can initialize books, but do not count toward the day's event coverage.

Even complete L2 and trades support an **estimated execution model**. They do not identify the queue position of a hypothetical order. Maker research still requires explicit assumptions about queue depletion, cancellations ahead, latency, partial fills, post-only rejection, fees, hedging and leg risk. A market-data timestamp is not the receipt time that a trader observed.

This download has no trading-performance claim. Start with matched data diagnostics; only then define an execution model and test it on an independently reserved period.
