# Supplementary specifications and data readiness

The frozen September `candles.csv`, `funding.csv` and `dataset.json` remain unchanged. These supplements do not upgrade the existing strategy estimates to exact account performance.

## Downloaded

`instrument_metadata.json` normalizes **60 current venue/coin specifications** and preserves fetch timestamps. The four accompanying source JSON files retain the public exchange responses. `manifest.json` records their SHA-256 hashes.

- Binance and Bybit: price increments, quantity increments, minimum order quantities and reported minimum notionals.
- OKX: contract value, contract-value currency, multiplier, contract quantity increment, price increment and minimum contract quantity. Volumes require these units to be interpreted.
- Hyperliquid: size decimals, maximum leverage and listing metadata. Tick rules and minimum notional are not invented where the metadata endpoint does not supply them.

These are **current snapshots**, fetched after September. Their exact validity during September is unverified. Account-specific fee tiers, historical changes, risk limits and collateral balances are not supplied by these public endpoints.

Reproduce from the repository root:

```powershell
python datasets/2026-09/fetch_supplements.py
```

## What is sufficient for each research question?

| Research | Existing evidence | Remaining requirement |
| --- | --- | --- |
| Lagged settled-rate persistence and low-turnover selection | Complete common September settled rates and hourly prices | Chronological fit/evaluation rules; explicit latency and valuation assumptions |
| Exact funding cash PnL | Actual rates, Binance settlement marks, Bybit/OKX hourly mark proxies | Hyperliquid historical oracle, exact settlement-time references for other venues, account reconciliation |
| Intraday executable basis/taker PnL | Hourly traded-price proxies | Synchronized bid/ask, sufficient depth, precise receipt times and historical instrument constraints |
| Maker fills and fee advantage | Current contract metadata only | Historical L2 updates, trades, exchange sequence/order identifiers where available, queue assumptions, latency, historical fee eligibility |
| Original pre-settlement predicted-funding signal | Actual final settlements | Timestamped historical funding predictions; final rates cannot replace these before publication |
| Precise net account returns | Scenario fees and pooled capital | Actual historical account fee tier, collateral, financing, liquidation and transfer rules |

A completed historical month cannot retroactively supply receipt timestamps or order-level identities that were never archived. L2 snapshots alone cannot establish a particular maker order's fill. Any replay must label unobserved queue position and latency assumptions even after acquiring books and trades.

## Hyperliquid archive access

The [official historical-data documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data) gives:

```text
s3://hyperliquid-archive/asset_ctxs/YYYYMMDD.csv.lz4
s3://hyperliquid-archive/market_data/YYYYMMDD/H/l2Book/COIN.lz4
```

Asset contexts are the candidate source for historical oracle/mark and contemporaneous funding observations; inspect actual fields and cadence before treating them as complete. The documentation explicitly says updates may be delayed and records may be missing. Requester-pays AWS access is required for documented downloads; costs and object inventory must be checked before a bulk download. No paid archive downloads or private-account requests were made for this supplement.

Trade/node archives are separately documented under `hl-mainnet-node-data`. Their schemas, access rules and period coverage need independent checks. An archive path being documented does not prove every September object is present.

## Additional execution-source findings

Read-only source inspection on 19 October 2026 verified accessible Bybit 200-level books and OKX price-level L2 archive samples. These are availability checks, not a full-month download or continuity certification.

- Bybit BTC September 1 book: `https://quote-saver.bycsi.com/orderbook/linear/BTCUSDT/2026-09-01_BTCUSDT_ob200.data.zip` (180,929,446 compressed bytes); boundary-date HEAD checks succeeded for all 15 candidate coins. Public trades are separately available.
- OKX BTC September 1 book: `https://static.okx.com/cdn/okx/match/orderbook/pro/L2/400lv/daily/20260901/BTC-USDT-SWAP-L2orderbook-400lv-2026-09-01.tar.gz` (378,586,403 compressed bytes). Official daily listings succeeded for the 15 coins. Trade archives use UTC+8 partitions whereas books use UTC; adjacent partitions and strict event-time filtering are required.
- Binance public `bookDepth` samples contain percentage-distance aggregates (`timestamp,percentage,depth,notional`), not price-level books. The BTC September bookTicker prefix was empty in the public bucket inspection. Public individual trades exist, but a usable public September price-level book source remains unestablished.
- Hyperliquid anonymous archive requests explicitly require requester-pays authentication. September current-public-API predicted funding is not historical September forecast data.

A practical next execution study is **Bybit–OKX, one coin over matched days**, downloading both full books and trades and validating sequence continuity before expanding. It still requires a declared maker queue/latency model. Full four-venue maker execution remains blocked by the unverified Binance book source and restricted Hyperliquid archives.

Official references: [Bybit historical data](https://www.bybit.com/derivatives/en/history-data), [OKX historical market-data API](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-historical-market-data), [Binance public-data documentation](https://github.com/binance/binance-public-data).

## Current readiness

The public common dataset supports a carefully labeled **hourly price-proxy, lagged-settlement funding study**. Exact funding-receipt replication, BBO execution claims and maker-fill claims remain unsupported until the identified inputs are acquired and validated. Detailed source access probes are recorded in `source_access.json` when available.
