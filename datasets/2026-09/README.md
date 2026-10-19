# Shared market dataset: September 2026

A single UTC calendar month of public perpetual-futures data across Binance, Bybit, OKX and Hyperliquid. This dataset is the shared baseline for subsequent cross-venue research.

[繁體中文說明](README.zh-TW.md)

## Validated download

All **15 coins × 4 venues = 60 markets** passed independent validation: **43,200 hourly candles**, **14,850 actual funding events**, and **zero missing candle hours or settlement events**. Each CEX market has 90 eight-hour settlements; each Hyperliquid market has 720 hourly settlements. All 32,400 CEX mark opens are present; Hyperliquid mark/oracle fields remain unavailable.

## Fixed window and universe

- Start inclusive: **2026-09-01 00:00:00 UTC**.
- End exclusive: **2026-10-01 00:00:00 UTC**.
- Frequency: **one-hour trade candles**, exactly 720 expected opens per market.
- Candidate coins: ADA, ARB, ATOM, AVAX, BNB, BTC, DOGE, DOT, ETH, LINK, LTC, OP, SOL, UNI and XRP.
- Venues: Binance USDT-M, Bybit linear USDT, OKX USDT swaps, Hyperliquid USD-denominated perpetuals.

The accepted common universe is frozen in [dataset.json](dataset.json), with per-market checks in [coverage.csv](coverage.csv). Only coins complete at **all four venues** qualify. The downloader retains any incomplete candidate observations for transparency; the accepted list determines which rows may be compared. No symbol substitutions, interpolated candles or synthetic funding histories are used.

## Files

| File | Meaning |
| --- | --- |
| [candles.csv](candles.csv) | Real hourly trade OHLCV, native symbol and optional historical CEX mark opens |
| [funding.csv](funding.csv) | Actual settled funding rates at each venue's native settlement timestamps |
| [coverage.csv](coverage.csv) | Exact candle counts, funding intervals, missing hours/events and accepted markets |
| [dataset.json](dataset.json) | Fixed time window, common universe and SHA-256 input hashes |
| [provenance.json](provenance.json) | Fetch dates, endpoints, request parameters, response hashes and source coverage |
| [fetch_data.py](fetch_data.py) | Public, credential-free downloader |
| [validate_data.py](validate_data.py) | Independent completeness and financial-data sanity checks |

Candles are labeled by their **open time**. Their high, low, close and volume become known only after the hour ends. A strategy deciding at 12:00 may use the completed 11:00 candle; using the 12:00 candle's close would leak future information. A candle open is a price proxy and does not establish executable bid/ask prices.

Funding rates are decimal fractions for one native settlement period: `0.0001` means 0.01%, or 1 bp. Hyperliquid settles hourly; CEX periods are checked from actual event spacing per contract. Normalize only for comparison, and retain raw rates for settlement cash-flow calculations. Funding records are outcomes, not proof of which predicted rates were visible before settlement.

Volume retains each endpoint's native units. OKX contract-volume fields and base-denominated volume from another venue cannot be summed without instrument-size conversion. Stablecoin collateral and quote differences are preserved in the native symbols; this data export does not establish USD/USDT/USDC parity.

Historical CEX mark opens are retained where supplied. Hyperliquid trade prices do not stand in for its oracle or mark prices. Missing mark/oracle values remain missing. Hyperliquid funding valuation requires its oracle-price basis, so a funding-cash PnL backtest needs additional verified oracle observations or an explicit valuation estimate.

## Reproduce and validate

From the repository root, Python 3.10+:

```powershell
python -m pip install -r requirements.txt
python datasets/2026-09/fetch_data.py
python datasets/2026-09/validate_data.py
```

The included data is the versioned baseline. Refetching can change availability or source revisions; compare hashes before treating a new download as the same dataset. Offline validation requires only the included CSVs.

For subsequent research, record `dataset_id`, input hashes, the common universe, decision timestamps, the train/evaluation split and cost assumptions with every result. This directory establishes a common sample; it contains no strategy-performance claim.
