# Crypto Market Research

Python research on perpetual-futures funding, centralized-exchange order books, decentralized-exchange liquidity, and disappearing liquidity levels.

Research is organized into four topic directories. Each directory contains its Python source, datasets, charts, and result workbooks.

| Directory | Study | Included results |
| --- | --- | --- |
| [`funding/`](funding/) | Funding-rate differences across Binance, Bybit, OKX, and Hyperliquid | September four-venue shared-data strategy, causal signals, and costed trade ledgers |
| [`liquidity/`](liquidity/) | BTC order-book depth and stress across Binance, Bybit, and OKX | Slippage, recovery estimates, and resilience scores |
| [`dex/`](dex/) | ETH/USDC and WBTC/USDC liquidity fragmentation | Pool concentration and routing-cost comparisons |
| [`ghost/`](ghost/) | Large BTC liquidity levels and cancellation behaviour | Event labels, classification, and price-response analysis |

## Repository structure

```text
crypto-market-research/
|-- README.md
|-- RESULTS.md
|-- requirements.txt
|-- datasets/      # Frozen September four-venue shared data
|-- funding/       # September strategies and earlier funding studies
|-- liquidity/     # Order-book depth, stress, and resilience
|-- dex/           # Pool fragmentation and execution routing
`-- ghost/         # Disappearing liquidity and price response
```

Each topic keeps its inputs and outputs together. File prefixes identify the study when downloading individual files.

## Shared data for subsequent research

Use [September 2026: one common four-venue dataset](datasets/2026-09/README.md) for new cross-venue comparisons. The UTC window is 1 September inclusive through 1 October exclusive. Its versioned manifest fixes the shared universe and data hashes; per-market coverage checks identify accepted inputs. Historical January–March and August studies remain separate samples.

The [September strategy replay](funding/september/README.md) now uses this exact frozen dataset. The original rule makes **0 trades**; the separately predefined lower-threshold comparison makes **387 trades**, with estimated net PnL **−$10,701.46** at 3 bps adverse execution penalty per fill plus taker fees. Hyperliquid funding notional is an explicit oracle-price estimate.

## Free execution-data pilot

[Bybit/OKX BTC, September 1 UTC](execution/btc-2026-09-01/README.md) adds freely downloaded price-level books and trades. Both markets cover all 1,440 minutes. Raw archives stay local; reproducible collectors, SHA-256 source hashes and validation reports are public. The [execution replay](execution/btc-2026-09-01/MODEL.md) finds no cost-filtered taker/maker entries. A separately scheduled 0.01 BTC maker probe loses an estimated $106.79 in its 2 bps maker-fee, 5 s TTL, 100 ms hedge scenario. The OKX archive lacks sequence/checksum fields, and hypothetical maker queue position remains a modeling assumption.

## Results

[RESULTS.md](RESULTS.md) links the September shared-data strategy, earlier funding studies, and the other studies' result workbooks. PNG charts summarize the studies; CSV ledgers and workbooks retain detailed calculations and event records.

- September funding: the original rule takes **0 trades**. The fixed lower-threshold comparison takes **387 trades**, with estimated net PnL **−$10,701.46** at 3 bps execution penalty per fill plus fees. See [the shared-data strategy](funding/september/README.md).
- Earlier August funding: the original high-threshold rule takes **0 trades** on August data. The separate lower-threshold August comparison takes **109 trades** and returns **−$2,791.87** with 1 bp additional slippage per fill. See [methodology and reproduction](funding/README.md).
- January–March funding: a separate [actual-data replay](funding/jan_mar/README.md) covers Binance/Bybit/OKX and 15 common coins. The original 22?23 bps entry thresholds produces **0 trades**; the strongest known lagged-settlement gap is **10.9289 bps**. Historical predicted-rate observations and Hyperliquid hourly price/oracle coverage remain unavailable in this replay.
- Liquidity analysis: included workbook resilience scores are Binance 74.92, OKX 49.89, and Bybit 41.13. Scores depend on this sample and the implemented normalization.
- DEX analysis: Uniswap v3 accounts for 94.17% of ETH/USDC pool TVL and 100% of WBTC/USDC pool TVL in the included pool snapshot. The included model estimates 0.08 bps ETH routing savings at $50,000.
- Ghost-level backtest: 13,759 classified events, 53.1% signal win rate, and mean signal PnL of 0.38 bps before any unmodelled execution costs. Labels infer behaviour from public snapshots; participant identities and intent are unobserved.

## Plots

### September funding: shared four-venue data

![September shared-data funding strategies: estimated equity and cost attribution](funding/september/performance.png)

The original rule stays flat. The pre-existing lower-threshold comparison earns an estimated $333.16 in funding but loses $10,701.46 after price PnL, execution penalties and fees. Read the [September methodology](funding/september/README.md) for causal signals and the missing-oracle valuation assumption. Earlier August and January–March studies remain linked separately.

### Order-book depth and slippage

![BTC order-book depth and estimated slippage across Binance, Bybit, and OKX](liquidity/liquidity_depth_slippage_comparison.png)

Compare visible BTC liquidity and estimated execution costs across the three exchanges. Results reflect the collected snapshots and the trade-size assumptions in `liquidity/liquidity_analysis.py`.

### DEX routing comparison

![Estimated execution costs for single-venue and optimized DEX routing](dex/dex_routing_comparison.png)

Compare modeled routing costs for ETH/USDC and WBTC/USDC. Estimates depend on the included pool snapshot and the liquidity assumptions in `dex/dex_analysis.py`.

### Price response around ghost events

![Observed price response following classified ghost-liquidity events](ghost/ghost_price_after_events.png)

Price-response analysis around disappearing large liquidity levels. Snapshot-based labels describe observed order-book behaviour; account identities and intent are unobserved.

## Run locally

Use Python 3.10 or newer. For an existing Conda environment:

```powershell
conda activate aqa2
python -m pip install -r requirements.txt
python funding/september/backtest.py
python -m unittest discover -s funding/september -p "test_backtest.py" -v
python liquidity/liquidity_analysis.py
python liquidity/liquidity_backtest.py
python dex/dex_analysis.py
python dex/dex_backtest.py
python ghost/ghost_analysis.py
python ghost/ghost_backtest.py
```

Each script reads and writes files beside itself. The September funding strategy reads the frozen shared dataset offline; detailed timing, oracle-proxy assumptions and reproduction instructions are in [funding/september/README.md](funding/september/README.md). Running an analysis or backtest updates its topic's workbooks and charts. Run analyses before their associated backtests. `*_fetch_*.py` scripts fetch fresh public market data, and `*_ws_collector.py` scripts collect new live snapshots; these require network access and may be subject to exchange restrictions.

## Data and interpretation

The funding study has been rebuilt on August data; the other three studies retain their original answer-pack samples. Each topic retains its own observations, candle context, and historical window. The order-book CSVs include the supplied REST and streaming samples. Candle histories provide price context at a different sampling frequency. File timestamps, workbook rows, and collection code should be used to assess the exact sample.

Funding uses archived 11–20 August observations and actual settlements from all four venues. Signals use only prior observations and per-contract intervals. Hyperliquid funding notional uses archived oracle prices. CEX notional falls back to fresh archived mark/mid proxies where exact historical marks are unavailable. Execution uses observed BBO plus explicit slippage assumptions; full depth-dependent impact and venue margin mechanics remain unmodelled. DEX routing uses the assumptions implemented in the supplied pool model, including estimated liquidity. Public order-book snapshots provide limited evidence about fills, cancellations, queue position, and intra-snapshot events. Historical and simulated results describe these samples and assumptions.

Binary Word/PDF reports and development logs are omitted from this code-and-results export. The results summary links the August funding replay and the other studies' workbooks.
