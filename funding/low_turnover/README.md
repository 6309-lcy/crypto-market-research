# Cost-covering, low-turnover funding strategy

This study uses the unchanged `crypto-perpetuals-2026-09-v1` [shared September dataset](../../datasets/2026-09/README.md). It tests predefined cost-covering rules rather than selecting the best September threshold.

[繁體中文說明](README.zh-TW.md)

## Reproduced result

The three fixed cost scenarios take **zero trades**, preserve **$100,000** initial capital and incur zero fees. The strongest consistent evaluation signal is long UNI on Bybit / short UNI on Hyperliquid at September 24 09:00 UTC, with nine positive historical buckets. Its forecast still falls below the cost threshold.

| Per-fill execution penalty | Trades | Estimated net | Best consistent 72h forecast | Its required costs + buffer | Shortfall |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 bps | 0 | $0.00 | 19.8168 bps | 29 bps | 9.1832 bps |
| 3 bps | 0 | $0.00 | 19.8168 bps | 37 bps | 17.1832 bps |
| 5 bps | 0 | $0.00 | 19.8168 bps | 45 bps | 25.1832 bps |

The study therefore avoids the earlier low-threshold churn but has not found a profitable entry under these rules. The prior 387-trade result covers September 1–30 and uses different entry rules; it is historical context rather than a directly comparable same-window control. This evaluation starts September 15 and is not a newly unseen holdout.

![Cost-covering low-turnover results and forecast shortfalls](performance.png)

[Full results](results.json), [forecast/rejection ledger](decisions.csv), [hourly equity](equity.csv), [trade ledger](trades.csv) and [payments](payments.csv). The complete all-direction decision CSV is approximately 91 MB and should be downloaded for analysis. Empty trade/payment ledgers are expected when no entry qualifies.

**24 independent tests pass**, including future-mutation invariance, raw hourly-rate aggregation, exact settlement ownership, full cost accounting, holding/cooldown rules and exit-cost equality. The exact rule exits only below future cost coverage; equality alone does not trigger that exit.

## Fixed protocol

- Development and historical context: September 1–14. Evaluation: September 15 00:00 through September 30 23:00 UTC, flat at the start and liquidated at the final available open.
- September was already examined in previous research. This is chronological development evaluation, not a newly unseen holdout.
- Use nine complete, published eight-hour UTC buckets (72 hours). Sum native settled rates in each bucket: eight hourly Hyperliquid events versus one eight-hour CEX event. Original timestamps determine publication; bucket-end plus five minutes is a conservative availability requirement.
- For each venue pair and direction, require at least eight positive bucket differences out of nine. Forecast the next 72 hours using nine times the lower quartile of the historical eight-hour spread.
- Enter only if that conservative forecast covers full round-trip fees, four adverse per-fill execution penalties and a 5 bps buffer. A full 72-hour entry horizon must fit before final liquidation.
- Hold at least 24 hours. Exit afterward if the next 24-hour funding estimate no longer covers future exit fees, two execution penalties and a 2 bps buffer, or after three consecutive hourly reversed forecasts. Hard holding cap: seven days. No exit solely because another pair ranks higher.
- A closed coin waits 24 hours before re-entry. Decisions fill at the next hourly traded-price open; no same-decision churn.

These are modeling choices fixed before the run, not guarantees that past funding persists. Thresholds can legitimately result in zero trades.

## Capital, costs and valuation

Initial capital $100,000, target $10,000 per leg, at most five paired positions, equal base quantities and a pooled $1,000 allocation reserve. Per-fill taker assumptions: Binance 5 bps, Bybit 5.5 bps, OKX 5 bps, Hyperliquid 4.5 bps. Run additional adverse execution penalties of 1, 3 and 5 bps per fill.

Actual raw settled rates determine funding outcomes. Binance uses the funding endpoint's settlement mark; Bybit/OKX use historical hourly mark opens. Hyperliquid lacks historical oracle data in the common sample, so its trade open remains an explicit funding-notional proxy. Estimated cash and net PnL do not establish exact account receipts. USD stablecoins are assumed at parity.

Candle opens approximate execution. Historical BBO, dynamic impact, per-venue collateral, liquidation, financing, transfers and contract lot constraints are unmodeled. Pooled gross allocation is a capital constraint, not a complete exchange-margin simulator.

## Reproduce

From the repository root:

```powershell
python funding/low_turnover/backtest.py
python -m unittest discover -s funding/low_turnover -p "test_backtest.py" -v
```

The run reads the frozen dataset offline and records input/code hashes. Detailed ledgers, forecasts, cost-sensitivity results and the chart are generated beside the script. Interpret zero-trade results together with rejected forecasts and cost margins, rather than assuming a low-turnover variant must earn a profit.
