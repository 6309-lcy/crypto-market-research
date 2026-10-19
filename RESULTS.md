# Research results

Funding is reproduced from the included August replay inputs and code. The other tables are extracted from their included result workbooks. Each study has its own sample and assumptions.

## Funding: September shared-data strategy

Source: [results.json](funding/september/results.json), using `crypto-perpetuals-2026-09-v1` unchanged. [Methodology](funding/september/README.md) and [Traditional Chinese explanation](funding/september/README.zh-TW.md).

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

## Funding: August actual-settlement replay

Source: [august_results.json](funding/august_results.json). The full [methodology](funding/README.md), [original-rule ledger](funding/august_original_trades.csv), and [August comparison ledger](funding/august_frozen_trades.csv) are included.

| Strategy | Trades | Funding | Mid-price PnL | Spread + slippage | Fees | Net |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| original | 0 | $0.00 | $0.00 | $-0.00 | $-0.00 | $0.00 |
| august_frozen | 109 | $123.21 | $370.75 | $-1,177.78 | $-2,108.06 | $-2,791.87 |

Initial capital: $100,000. Additional adverse slippage: 1 bp per fill, on top of observed bid/ask spreads and taker fees. Evaluation decisions: 13 August 00:00 to 20 August 05:00 UTC, 2026.

Slippage sensitivity for the August comparison: 0 bps → $-2,371.79, 1 bps → $-2,791.87, 3 bps → $-3,626.59.

## Funding: January–March actual-data coverage

[Methodology and reproducible results](funding/jan_mar/README.md) cover 24 January 08:00 through 31 March 16:00 UTC, 2026. Binance, Bybit and OKX have usable funding and price data for 15 common coins. The past-settled-rate signal reaches at most 10.9289 bps per eight hours, below the original 22?23 bps entry thresholds: zero trades and zero PnL in all three execution-cost scenarios. This result does not reproduce the earlier synthetic four-venue portfolio.

## Liquidity: Resilience Scores

Source: [liquidity_analysis_results.xlsx](liquidity/liquidity_analysis_results.xlsx), sheet `Resilience Scores`.

| exchange | resilience_score | avg_recovery_seconds |
| --- | --- | --- |
| binance | 74.91907463734131 | 14.33173476190476 |
| bybit | 41.1281460163018 | 13.47346982222222 |
| okx | 49.89217613771036 | 278.6143698705882 |

## Liquidity: Performance Summary

Source: [liquidity_backtest_results.xlsx](liquidity/liquidity_backtest_results.xlsx), sheet `Performance Summary`.

| Metric | Value |
| --- | --- |
| Total OB snapshots | 139 |
| Exchanges | binance, bybit, okx |
| Orderbook stress snapshots | 43 |
| Trade sizes tested | $10k, $50k, $100k, $500k |
| binance - calm spread (bps) | 0.01 |
| binance - stress spread (bps) | 0.01 |
| binance - spread widening | -0.0% |
| bybit - calm spread (bps) | 0.01 |
| bybit - stress spread (bps) | 0.01 |
| bybit - spread widening | -0.0% |
| okx - calm spread (bps) | 0.01 |
| okx - stress spread (bps) | 0.01 |
| okx - spread widening | 0.0% |

## Dex: Fragmentation Metrics

Source: [dex_analysis_results.xlsx](dex/dex_analysis_results.xlsx), sheet `Fragmentation Metrics`.

| pair | num_pools | total_tvl_usd | gini_coefficient | hhi | top_dex | top_dex_share_pct |
| --- | --- | --- | --- | --- | --- | --- |
| ETH_USDC | 23 | 206118266 | 0.8149989338813779 | 2727.75329795678 | uniswap_v3 | 94.17242429159577 |
| WBTC_USDC | 8 | 41691045 | 0.7483321028532626 | 4872.367781402831 | uniswap_v3 | 100.0 |

## Dex: Performance Metrics

Source: [dex_backtest_results.xlsx](dex/dex_backtest_results.xlsx), sheet `Performance Metrics`.

| Metric | Value |
| --- | --- |
| ETH_USDC $50k naive (bps) | 1.31 |
| ETH_USDC $50k optimal (bps) | 1.23 |
| ETH_USDC $50k savings (bps) | 0.08 |
| ETH_USDC $250k naive (bps) | 6.55 |
| ETH_USDC $250k optimal (bps) | 6.17 |
| ETH_USDC $250k savings (bps) | 0.38 |
| WBTC_USDC $50k naive (bps) | 5.98 |
| WBTC_USDC $50k optimal (bps) | 5.98 |
| WBTC_USDC $50k savings (bps) | 0.0 |
| WBTC_USDC $250k naive (bps) | 29.88 |
| WBTC_USDC $250k optimal (bps) | 29.88 |
| WBTC_USDC $250k savings (bps) | 0.0 |

## Ghost: Performance Summary

Source: [ghost_backtest_results.xlsx](ghost/ghost_backtest_results.xlsx), sheet `Performance Summary`.

| Metric | Value |
| --- | --- |
| Total events | 13759 |
| Likely spoofs | 12132 |
| Genuine cancels | 1627 |
| Spoof signal win rate | 53.1% |
| Avg PnL per spoof signal | 0.38 bps |
| Sharpe (per-trade) | 0.06 |
