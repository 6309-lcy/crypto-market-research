# September 2026: four-venue funding strategy

This replay uses exactly the frozen [September shared dataset](../../datasets/2026-09/README.md), `crypto-perpetuals-2026-09-v1`: 15 common coins, Binance/Bybit/OKX/Hyperliquid, 43,200 hourly trade candles and 14,850 actual funding events. Original input files are read in place and their SHA-256 hashes verified before running.

[繁體中文說明](README.zh-TW.md)

## Reproduced results

| Policy | Trades | Funding estimate | Trade-price PnL | Execution penalty | Fees | Estimated net |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| original | 0 | $0.00 | $0.00 | $-0.00 | $-0.00 | $0.00 |
| august_frozen | 387 | $333.16 | $421.47 | $-4,351.18 | $-7,104.90 | $-10,701.46 |

Base scenario: 3 bps adverse execution penalty per fill plus taker fees, $100,000 initial capital. Hyperliquid funding notional uses the explicitly estimated trade-open oracle proxy.

| Per-fill execution penalty | Original net | Lower-threshold comparison estimated net |
| --- | ---: | ---: |
| 1 bps | $0.00 | $-7,912.53 |
| 3 bps | $0.00 | $-10,701.46 |
| 5 bps | $0.00 | $-13,407.25 |

The original rule sees at most **8.994256 bps** at its eight-hour decisions, below every original entry threshold. The lower-threshold comparison makes **387 trades**, with estimated return **-10.70%** and maximum hourly liquidation-value drawdown **-10.71%**.

![September performance and cost attribution](performance.png)

## Predefined rules

| Policy | Decisions and entry | Exit |
| --- | --- | --- |
| `original` | Every 8h; long the lower-rate venue and short the higher-rate venue for each coin. Enter only when the known 8h-equivalent gap exceeds both legs' round-trip taker fees plus 2 bps. | Known held-pair gap below 0.5 bps, maximum 48h, or final sample liquidation. |
| `august_frozen` | Existing lower-threshold comparison from earlier research, fixed before this replay: hourly ranking, top five coins, enter above 1 bp per 8h. | After at least 8h: gap below 1/3 bp per 8h, outside top five, preferred pair changes, or maximum 48h. Final sample liquidation always applies. |

The policies are separately reported. There is no September threshold search. Pair fee assumptions per fill: Binance 5 bps, Bybit 5.5 bps, OKX 5 bps, Hyperliquid 4.5 bps. Thus original entry thresholds are 21–23 bps, depending on the pair. Adverse execution penalties of 1, 3 and 5 bps per fill are tested on top of taker fees. Account-specific fee tiers are not known.

Initial capital is $100,000, target notional is at most $10,000 per leg, maximum concurrent pairs is five, and the pooled allocation reserve is $1,000. Both legs use exactly the same base quantity, so differing venue prices produce slightly different notionals. Entry sizing accounts for existing positions' liquidation value, gross exposure, and the new pair's estimated immediate liquidation costs. This reserve constrains allocation at entry; future market moves can consume it. Venue-specific collateral, transfer and liquidation rules are not modeled.

## Timing and causal signals

The sample starts September 1 at 00:00 UTC. No prior-month observations are injected. Every venue/base must accumulate two already-published settlements before it has an inferred interval. CEX intervals in this sample are eight hours; Hyperliquid intervals are one hour. Inference uses only past timestamps rather than full-month future schedules.

A funding event is available for a signal only after its original timestamp plus a five-minute publication allowance. This is an explicit latency assumption because historical response receipt times are absent. Signals use `raw_rate * 8 / inferred_interval` exactly once. Actual settlement cash uses the original native rate, never this normalized signal rate. The complete common universe is defined by the frozen sample ex post, not by a reconstructed point-in-time listing universe.

Orders execute at the next hourly trade-candle open, one hour after the decision, plus adverse execution penalty. The same-hour high/low/close/volume are never used. Trade opens are execution proxies, not observed BBO or verified fills.

The data window is September 1 inclusive to October 1 exclusive. Final liquidation uses the final available hourly open, **September 30 at 23:00 UTC**. Exposure therefore excludes the remaining last hour. Original millisecond settlement timestamps determine ownership: events are paid only strictly after entry and on or before exit. Events milliseconds after final liquidation are excluded.

## Funding valuation uncertainty

- Binance: historical settlement mark supplied by the funding endpoint where present.
- Bybit and OKX: historical mark-price candle open at the settlement hour, explicitly a mark proxy near the event time.
- Hyperliquid: the archive lacks the required historical oracle. This replay uses the same-hour trade open as an **estimated funding-notional proxy**, preserving the actual settled funding rate. It does not assert that trade price equals oracle price.

A funding payment is `-side * base_quantity * funding_reference_price * raw_rate`, with side +1 for long and −1 for short. Every payment records its valuation source. The report separately shows funding, trade-reference price PnL, execution penalty, fees and estimated net PnL.

The report also shifts the Hyperliquid funding-notional proxy by ±1% on the fixed 3 bps trade ledger. This is an accounting sensitivity, not a claim about observed oracle bounds and not a rerun of sizing or a dynamic basis stress. Exact Hyperliquid cash receipts require actual oracle history. Both strategies' net results should be described as estimates under the stated price and execution assumptions.

## Reproduce

From the repository root, Python 3.10+:

```powershell
python -m pip install -r requirements.txt
python funding/september/backtest.py
python -m unittest discover -s funding/september -p "test_backtest.py" -v
```

The run is offline and reads only the frozen shared dataset. It produces [results.json](results.json), [performance.png](performance.png), and separate policy ledgers:

- [Original trades](original_trades.csv), [decisions](original_decisions.csv), [hourly equity](original_equity.csv), [funding payments](original_payments.csv).
- [Comparison trades](august_frozen_trades.csv), [decisions](august_frozen_decisions.csv), [hourly equity](august_frozen_equity.csv), [funding payments](august_frozen_payments.csv).

Stablecoins are valued at parity. Full depth-dependent impact, order minimums, funding receipt latency, insolvency, per-venue margin and capital transfers remain outside this replay. The one-month sample supports no annualized performance claim.
