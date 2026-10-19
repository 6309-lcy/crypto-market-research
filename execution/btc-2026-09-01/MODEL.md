# Single-day execution study

This pilot uses the complete local raw Bybit BTCUSDT and OKX BTC-USDT-SWAP books and trades for September 1, 2026 UTC. The one-day sample is a model-development and data-quality exercise. It does not establish persistent profitability or out-of-sample performance.

[繁體中文模型說明](MODEL.zh-TW.md)

## Implemented model

`replay.py` streams and hashes all five local raw archives. It reconstructs price-level books, retaining the top ten levels on a **100 ms strict-past grid**. Trade queue depletion uses original Bybit trades at microsecond precision; RPI trades are excluded. Quotes older than 1,000 ms are unavailable.

- Size: **0.01 BTC**, with the explicitly unverified OKX assumption **0.01 BTC/contract**.
- Taker scan: every second, both directions; threshold requires visible entry spread to exceed all four fees under a zero-exit-basis assumption. Entry occurs 100 ms later, and both legs liquidate 60 seconds later using displayed-depth VWAP.
- Maker: Bybit resting order, 100 ms placement delay; 1 or 5 second TTL; OKX hedge 100 or 500 ms after a full fill. Queue ahead is the larger of displayed quantity at decision and activation. No credit is given for cancellations. Only opposite-side non-RPI trades at the exact price, strictly inside the active lifetime, deplete queue. Orders whose price is absent from the activation top ten are rejected rather than guessed.
- Partial fills below one OKX contract are unwound on Bybit after expiry plus hedge latency. Failed hedges attempt a Bybit unwind. Any unresolved exposure stops subsequent orders for that scenario and is separately counted; settled PnL alone is incomplete if any unresolved exposure remains.
- Fees: Bybit taker 5.5 bps, OKX taker 5 bps; Bybit maker 2 bps base scenario and 0 bps sensitivity. These are assumptions, not verified account rates. No extra slippage beyond visible-depth VWAP is included.
- Funding: exclude orders whose worst-case lifetime crosses **any UTC hour**, conservatively avoiding funding payments. The last hour/day boundary guard includes order life, hedge delay and 60-second hold.
- Separate scenarios: cost-filtered maker signals versus alternating-direction scheduled probes every minute. Sensitivity accounts are counterfactual and their profits/losses must never be summed.

The 100 ms grid and public timestamps do not establish sub-millisecond receipt ordering. Post-only checks are grid approximations; intervening touch changes may be missed. Queue fills, hedge execution and liquidation are simulations rather than exchange-confirmed fills. Simultaneous paired exits also omit liquidation leg risk.

## Questions

1. Do synchronized visible quotes produce a cross-venue spread large enough to cover the stated costs?
2. How often does a deliberately scheduled maker probe execute under a conservative queue model?
3. After a modeled maker fill, what does hedging and liquidating the paired exposure cost?

A quote crossing both venues produces a long position on one venue and a short on the other. It does not close the trade. A valid round-trip result includes liquidation of both positions, all fills' fees, and price movement between hedge and liquidation. A one-leg maker fill must not be counted as hedged profit.

## Actual data versus assumptions

Actual inputs are price-level book messages and public trades from the recorded source archives. Original event timestamps determine causal ordering. Reports preserve source SHA-256 hashes so replay inputs are identifiable.

Hypothetical order arrival, queue position, fill allocation, cancellation arrival and latency are model assumptions. We cannot recover the exact receipt order at a trader's machine or the queue position of an order that never existed. Public trades and L2 can support a conservative fill model, but they do not prove the model's fills.

Bybit sizes are in BTC. OKX native sizes are contracts. Any BTC conversion using 0.01 BTC per contract is a declared scenario input: the historical contract value has not been independently verified. Public current instrument specifications cannot silently establish the historical value.

Fee scenarios represent ordinary taker/maker assumptions rather than an account's proven historical tier. A zero maker-fee scenario is a sensitivity only. Slippage, insufficient displayed depth, stale quotes, post-only rejection and failed hedge handling must be reported rather than treated as free executions.

## Interpretation

Cost-filtered opportunities and scheduled execution probes serve different purposes. A scheduled probe can quantify fill behavior and adverse selection even when no trading signal covers costs. Its PnL is a diagnostic experiment and must not be presented as alpha.

A positive visible spread is not a guaranteed return. The engine must apply decision and execution delays, read only already-observed data, account for quantity and fees, and value any remaining exposure. Intervals crossing a funding settlement require actual funding cash or exclusion with a clearly stated rule.

Inspect the generated result file and ledgers for the precise configured timeouts, latency, price sampling and accounting conventions. Alternative assumptions are scenarios; selecting one after observing its best outcome is not independent validation.
