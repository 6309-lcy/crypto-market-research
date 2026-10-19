# January-March 2026: actual data and original thresholds

[繁體中文說明](README.zh-TW.md)

This study tests the earlier strategy's economic rules on actual Binance, Bybit and OKX funding and price history from **24 January 08:00 through 31 March 16:00 UTC, 2026**. The earlier synthetic four-venue result of $5,195.67 has no reproducible real-data interpretation.

## Result

The three-venue replay takes **zero trades**. Across 2,985 coin/decision comparisons, the largest known eight-hour funding difference is **10.9289 bps**. The original entry condition requires more than **22?23 bps**: 20?21 bps round-trip taker fees, depending on the venue pair, plus a 2 bps buffer. Consequently the 1, 3 and 5 bps per-fill execution-penalty scenarios all preserve $100,000 starting capital.

![January-March actual data: equity, accounting and entry-gap comparison](performance.png)

This result applies to the three venues and lagged-settlement signal described below. It does not establish what a four-venue strategy using contemporaneous predicted funding would have earned.

## Actual inputs

- Original historical universe: ADA, ARB, ATOM, AVAX, BNB, BTC, DOGE, DOT, ETH, LINK, LTC, MATIC, OP, SOL, UNI, XRP.
- Fifteen coins have both venues' usable hourly trade and mark-price series. MATIC has no usable evaluation price series and is excluded; it is not substituted with POL.
- `funding.csv` retains actual funding events from 22 January for warm-up. Settlement timestamps preserve original Unix milliseconds. Binance returns actual settlement mark prices where available; Bybit/OKX valuation uses the matching historical mark-price candle open.
- `prices.csv` includes actual hourly traded-price and mark-price opens. It contains no invented bid/ask quotes.
- `provenance.json` records public endpoints, request parameters and response hashes, coverage, missing contracts and input-file hashes.

## Causal signal and comparison limits

Historical settled funding is available; a historical archive of the funding predictions seen before each settlement is not included. Each decision therefore uses the most recent actual settlement at least **five minutes earlier**, allowing for publication. This delay is an assumption because original HTTP receipt timestamps are unavailable. The five-minute delay excludes the funding event at the same decision boundary.

Funding periods are inferred only from the two most recent already-known settlement events. Raw rates are normalized to an eight-hour equivalent once for ranking. Future settlement rates and future intervals never enter earlier decisions. A full-window coverage audit separately verifies eight-hour settlements for the retained actual contracts; it is an outcome-completeness check, not a signal input.

The tradable market universe is fixed from prices and earlier funding present at the evaluation start. Missing later prices or outcomes fail validation rather than silently dropping a market.

At each 00:00, 08:00 and 16:00 decision, the strategy chooses the lower-rate venue for a long and the higher-rate venue for a short in each coin. It enters only above round-trip fees plus 2 bps. It exits when the known held-pair difference falls below 0.5 bps or at 48 hours, and liquidates at sample end. Both legs use equal base quantity. Capital is $100,000, target notional is at most $10,000 per leg, maximum concurrent pairs is five, and the allocation reserve is $1,000.

A decision executes at the next hourly traded-price open, imposing a one-hour delay. Per-fill adverse execution penalties of 1/3/5 bps model costs beyond the price proxy. Taker fees are Binance 5 bps, Bybit 5.5 bps and OKX 5 bps per fill. These are scenario assumptions, not verified account-specific fees. The original entry threshold is held fixed across execution scenarios.

Actual raw settlement cash flows are booked only while each leg is held. Prices, fees and estimated liquidation equity are accounted separately. Public candle opens do not prove executable historical liquidity. Historical spread, market impact, account margin, lot rounding, transfers and stablecoin basis remain unmodelled.

## Reproduce

From the repository root, Python 3.10+:

```powershell
python -m pip install -r requirements.txt
python funding/jan_mar/backtest.py
python -m unittest discover -s funding/jan_mar -p "test_backtest.py" -v
```

The included CSVs support an offline rerun. To refresh public inputs, subject to current API retention:

```powershell
python funding/jan_mar/fetch_data.py
python funding/jan_mar/add_okx.py
```

[Results and assumptions](results.json), [every ranked decision](decisions.csv), [hourly equity](equity.csv), and [trade ledger](trades.csv) are included. An empty trade ledger is the expected result of the original threshold on this coverage.

## Additional venue coverage

OKX actual settled funding comes from official monthly archives, read in memory and exported to CSV. Hourly trade and mark opens come from its historical candle APIs. Both input files retain Binance/Bybit records unchanged. Read [source availability and remaining Hyperliquid archive requirements](SOURCES.md).
