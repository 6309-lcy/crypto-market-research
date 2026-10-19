# Research results

These tables are extracted from the included result workbooks. Each study has its own sample and assumptions.

## Funding: Performance Metrics

Source: [funding_backtest_results.xlsx](funding/funding_backtest_results.xlsx), sheet `Performance Metrics`.

| Metric | Value |
| --- | --- |
| Total Trades | 84 |
| Win Rate (%) | 70.2 |
| Total Gross PnL ($) | 6578.67 |
| Total Fees ($) | 1383.00 |
| Total Net PnL ($) | 5195.67 |
| Avg Net PnL/Trade ($) | 61.85 |
| Avg Net PnL/Trade (%) | 0.6185 |
| Max Drawdown ($) | 90.78 |
| Avg Holding Periods | 4.2 |
| Unique Coins Traded | 9 |
| Position Size (USD/leg) | $10,000 |
| Historical Data Basis | Binance actual history; other exchanges simulated from snapshot offsets |
| Random Seed | 42 |

## Funding: Backtest Notes

Source: [funding_backtest_results.xlsx](funding/funding_backtest_results.xlsx), sheet `Backtest Notes`.

| item | detail |
| --- | --- |
| Methodology note | Only Binance funding history is real. Bybit, OKX, and Hyperliquid histories are simulated from current cross-exchange offsets plus fixed-seed noise. |
| Interpretation note | Use the backtest to compare strategy behaviour under consistent assumptions, not as a prediction of live future returns. |

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
