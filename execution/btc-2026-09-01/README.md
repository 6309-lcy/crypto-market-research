# Free matched execution data: BTC, September 1, 2026

[繁體中文說明](README.zh-TW.md)

This study acquires public historical order books and trades for **Bybit BTCUSDT and OKX BTC-USDT-SWAP**, using one identical UTC day: September 1 00:00 inclusive to September 2 00:00 exclusive. It supplements the frozen September funding dataset without changing that dataset.

Large original archives stay in a local cache outside the public repository. Download programs, source hashes, validation reports and minute-level coverage summaries are versioned here. ZIP and compressed source archives are not committed.

## Execution experiment results

The full raw-data replay completed. **No cost-filtered taker or maker entries** occurred. Among **172,773** valid directional one-second observations, the best crossed spread was **7.7157 bps gross**, **−2.7841 bps after entry fees**, and **−13.2841 bps** after assumed round-trip fees at zero exit basis. Twenty-five direction observations lacked enough visible top-10 depth and were excluded.

Scheduled alternating-direction maker probes are a separate cost experiment, not an alpha strategy. Each row below is a separate hypothetical account; results must not be summed.

| TTL | Hedge latency | Attempts | Closed pairs | Partial unwinds | Net, 2 bps maker | Net, 0 bps maker |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 s | 100 ms | 1,403 | 13 | 11 | $-20.1453 | $-17.6553 |
| 1 s | 500 ms | 1,403 | 13 | 11 | $-20.7760 | $-18.2860 |
| 5 s | 100 ms | 1,344 | 75 | 18 | $-106.7919 | $-94.2067 |
| 5 s | 500 ms | 1,344 | 75 | 18 | $-108.3778 | $-95.7925 |

All scenarios have **zero unresolved exposure**. Base scheduled probe (2 bps maker fee, 5 s TTL, 100 ms hedge latency): gross −$1.1809, fees $105.6110, net **−$106.7919**. It tries 0.01 BTC orders, keeps at most one experiment active, and liquidates each complete paired exposure 60 seconds after hedging.

[Full assumptions](MODEL.md), [Traditional Chinese model](MODEL.zh-TW.md), [results](results.json), [execution ledger](executions.csv), and [directional opportunity scan](opportunities.csv) are included. The approximately 25 MB full opportunity CSV is best downloaded rather than viewed in GitHub's file renderer.

![BTC Bybit/OKX execution-cost experiment](performance.png)

```powershell
python execution/btc-2026-09-01/replay.py --cache ../market_data_cache/btc-2026-09-01
python -m unittest discover -s execution/btc-2026-09-01 -p "test_replay.py" -v
```

**27 independent regression tests** pass, alongside seven internal synthetic check groups. The 100 ms top-10 grid, fee and latency scenarios, queue model, unverified historical OKX 0.01 BTC contract value, and exclusion of all hour-crossing intervals are explicit limits. This single-day development experiment establishes no live or out-of-sample profitability.

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

The replay is a hypothetical execution experiment and makes no realized trading-performance claim. Start with matched data diagnostics; only then define an execution model and test it on an independently reserved period.
