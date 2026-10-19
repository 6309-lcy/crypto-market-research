# January-March source availability

Public-data probes were made on 19 October 2026. Historical funding outcomes and historical pre-settlement predictions are separate datasets.

| Venue | Actual settled funding | Historical prices | Historical execution coverage |
| --- | --- | --- | --- |
| Binance | Public funding history endpoint, included CSV | Hourly trade and mark opens, included CSV | Candle-price proxy with adverse cost scenarios |
| Bybit | Public funding history endpoint, included CSV | Hourly trade and mark opens, included CSV | Candle-price proxy with adverse cost scenarios |
| OKX | Official monthly funding archives; older than the REST funding retention | Trade and mark candle endpoints retain January | Official L2 listings exist; full-period book reconstruction not performed |
| Hyperliquid | January and March fundingHistory responses verified; pagination supported | January hourly candles returned empty; 4h/daily candles returned | Hourly trade/mark/oracle and BBO archives require further archive access and validation |

## OKX

The ordinary `GET /api/v5/public/funding-rate-history` endpoint retains approximately three months. A January BTC query returned an empty success response. The official archive listing at `GET /api/v5/public/market-data-history` provides older monthly funding files. Verified BTC examples:

- `https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202601/BTC-USDT-SWAP-fundingrates-2026-01.zip?v=1001`
- February uses `202602` and `2026-02`; March uses `202603` and `2026-03`.

The CSV fields are `instrument_name,funding_rate,funding_time`. Download scripts read the ZIP bytes in memory and export CSV data; ZIP files are excluded from this repository. Archive grouping can use UTC+8, so actual event timestamps determine the UTC evaluation window.

January hourly prices were verified using `/api/v5/market/history-candles` and `/api/v5/market/history-mark-price-candles`. The corresponding index-price endpoint also returned January data, but an index is not an executable quote. Historical L2 file listings were verified for January 24 and March 31; individual BTC files were approximately 180–220 MB and were not downloaded in this audit.

Official references: [API documentation](https://www.okx.com/docs-v5/en/) and [historical data](https://www.okx.com/historical-data).

## Hyperliquid

`POST https://api.hyperliquid.xyz/info` with `type=fundingHistory` returned January 24 and March 31 BTC funding records. Requests spanning the period returned 500 events; pagination by the last returned timestamp plus one millisecond returned subsequent events.

`type=candleSnapshot` retains only the latest 5,000 bars for the requested interval. January BTC 1h requests returned no bars on the audit date; January 4h and daily requests returned data. Coarser candles cannot reconstruct the hourly execution, historical mark or oracle values required here.

The [official archive documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data) describes:

```text
s3://hyperliquid-archive/market_data/YYYYMMDD/H/l2Book/COIN.lz4
s3://hyperliquid-archive/asset_ctxs/YYYYMMDD.csv.lz4
```

Anonymous January object probes returned HTTP 403. This establishes an access limitation, not proof that the objects are absent. Documented downloads use AWS requester-pays access, and archive cadence/completeness must be verified. No AWS credentials or paid downloads were used in this study. The [Info API documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) explains the public endpoints.

## Signal interpretation

The earlier local file had a single four-venue snapshot from March 31, 16:33 UTC. It cannot establish what funding predictions were visible in January or February. This replay instead uses actual settlements already published before each decision, with an explicit five-minute publication allowance. Adding settlement records alone cannot reproduce missing historical predicted-rate signals.
