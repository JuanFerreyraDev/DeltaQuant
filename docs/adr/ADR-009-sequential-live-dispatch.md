# ADR-009: Sequential Live Dispatch for Capital-Constrained Triangle Execution

**Status:** PROPOSED  
**Date:** 2026-09-08  
**Context:** Phase 5 Stage 1 — Live Binance Testnet Order Plumbing  
**Amends/Supersedes:** Phase 3 parallel-dispatch design for the **LIVE** execution path only (DRY_RUN simulation remains unchanged).

---

## 1. Context and Problem Statement

In Phase 3, the engine adopted a parallel dispatch model (`asyncio.gather`) for all 3 FOK legs. In `DRY_RUN` simulation, this model is safe and clean because simulated account balances do not constrain order placement: each leg's outcome is evaluated abstractly.

However, in **LIVE execution** with a real exchange (Binance Spot), triangular arbitrage is inherently capital-constrained and sequentially chained:
1. Leg 0 spends initial capital (e.g. USDT) to purchase intermediate asset $A$ (e.g. BTC).
2. Leg 1 spends asset $A$ to purchase intermediate asset $B$ (e.g. ETH).
3. Leg 2 spends asset $B$ to return to base capital (e.g. USDT).

If all 3 legs are fired concurrently via `asyncio.gather`:
- **Problem 1 (Theoretical Sizing):** Leg 1's quantity must be sized *before* Leg 0's actual fill is known. If Leg 0 suffers even slight price/fee differences or partial fills, Leg 1's pre-computed size does not match actual held inventory.
- **Problem 2 (Exchange Race Condition):** Binance spot accounts check balances synchronously at order evaluation time. If Leg 1's network packet arrives or is processed before Leg 0's fill has settled and credited asset $A$ to the account ledger, Binance rejects Leg 1 with `InsufficientFunds` (`Account has insufficient balance for requested action.`).

---

## 2. Empirical Testnet Evidence

On 2026-09-08, DeltaQuant ran 3 consecutive iterations of concurrent 2-leg FOK dispatch (`asyncio.gather`) on Binance testnet under strictly isolated balance conditions (starting free BTC = 0):
- **Leg 0:** BUY BTC with USDT on `BTCUSDT` (at ask).
- **Leg 1:** BUY ETH with theoretical BTC on `ETH/BTC` (at ask).

### Measured Results:
- **Iteration 1 (565ms total):** Both filled. Leg 0 transactTime `1788908569779`, Leg 1 transactTime `1788908570017` (238ms delta). Leg 1 arrived after Leg 0 had credited BTC.
- **Iteration 2 (869ms total):**
  - Leg 0: **FILLED** (transactTime `1788908647137`, id `13747104`).
  - Leg 1: **EXCEPTION: `ccxt.InsufficientFunds: binance Account has insufficient balance for requested action.`**
  - **Verdict:** Leg 1 was evaluated before Leg 0's fill credited the account, causing Leg 1 to fail immediately with an InsufficientFunds rejection despite Leg 0 filling 100%.
- **Iteration 3 (321ms total):** Both filled. Leg 0 transactTime `1788908656151`, Leg 1 transactTime `1788908656157` (6ms delta).

### Key Takeaway:
Parallel dispatch of sequentially-dependent spot legs is **intrinsically non-deterministic** on real exchanges. The success of Leg 1 depends on network latency jitter and exchange-side ledger settlement timing. A strategy cannot risk emergency market liquidation triggered purely by a self-inflicted race condition.

---

## 3. Decision

We diverge the **LIVE** execution path from the **DRY_RUN** simulation path:

1. **DRY_RUN mode:** Remains parallel (`asyncio.gather` / simulated leg failures) as designed in Phase 3.
2. **LIVE mode:** Adopts **Sequential Live Dispatch**:
   - **Step 1 (Leg 0):** Place Leg 0 FOK order using initial `position_usdt`. Await confirmed fill.
     - If Leg 0 expires or errors: Abort immediately. Return status `FAILED_LEG_0`. 0 unhedged inventory.
   - **Step 2 (Leg 1):** Compute Leg 1 order quantity using **actual confirmed `filled_qty` and `avg_price`** from Leg 0's `OrderResult` (minus fees). Place Leg 1 FOK order. Await confirmed fill.
     - If Leg 1 expires or errors: Trigger emergency market liquidation on Leg 0's acquired asset via `_reconcile_inventory(failed_leg_index=1)`.
   - **Step 3 (Leg 2):** Compute Leg 2 order quantity using **actual confirmed `filled_qty`** from Leg 1's `OrderResult`. Place Leg 2 FOK order. Await confirmed fill.
     - If Leg 2 expires or errors: Trigger emergency market liquidation on Leg 1's acquired asset via `_reconcile_inventory(failed_leg_index=2)`.
     - If Leg 2 fills: Return status `COMPLETED`. Triangular cycle closed.

---

## 4. Consequences and Trade-offs

### Advantages:
- **Zero Ledger Race Conditions:** Eliminates `InsufficientFunds` rejections caused by in-flight settlement races.
- **Exact Quantity Sizing:** Leg 1 and Leg 2 are sized based on real coins in hand, not theoretical assumptions.
- **Clean Failure Boundaries:** If Leg 0 fails, no downstream orders ever touch the exchange. If Leg 1 fails, only Leg 0's inventory exists to unwind.
- **Eliminates Combinatorial State Explosion:** States like "Leg 0 failed + Leg 2 filled" become strictly impossible by construction, obviating complex parallel reconciliation matrices.

### Trade-offs:
- **Execution Latency & Exposure Window:** Total execution time equals $RTT_0 + RTT_1 + RTT_2$ (~300–600ms total on Binance REST) rather than $\max(RTT_0, RTT_1, RTT_2)$.
- **Book Staleness Risk:** By the time Leg 2 is placed, its target order book price may have moved slightly. However, because FOK orders are used, if the book moved unfavorably, the order simply expires and triggers the existing reconciliation mechanism, rather than filling at an unhedged or loss-making price.

---

## 5. Latency Trade-Off and Price Exposure Window

Sequential dispatch deliberately trades execution speed for ledger determinism and safety. However, it reintroduces the inter-leg price-movement exposure window that Phase 3's parallel design was conceived to compress:

- **Empirically Observed Latencies (from Testnet Investigation):**
  - Observed round-trip time per REST request ranged from **257 ms to 869 ms**.
  - Across 3 sequential legs, total wall-clock execution duration spans:
    $$\text{Best case: } 3 \times 250\text{ ms} \approx 750\text{ ms}$$
    $$\text{Worst observed case: } 3 \times 870\text{ ms} \approx 2.6\text{ seconds}$$
- **Price Exposure Window:**
  - In liquid crypto spot markets, top-of-book quotes move on 10ms–100ms timescales. Over a 750ms–2.6s window, the book ticker snapshot sampled at $T_0$ will frequently have shifted by the time Leg 1 or Leg 2 arrives at the exchange.
  - Because all legs use **FOK (Fill-or-Kill)**, an adverse shift in the book does *not* result in negative slippage fills; rather, the FOK order immediately expires unfilled.
  - The consequence is an increased frequency of partial executions triggering reconciliation: Leg 0 fills, but Leg 1 or Leg 2 expires due to price movement during the sequential delay.
- **Interaction with `SAFETY_MARGIN`:**
  - DeltaQuant currently configures `SAFETY_MARGIN = 0.0015` (15 bps) to absorb fees, minor slippage, and book drift.
  - Whether 15 bps is sufficient to keep sequential triangular paths profitable against a 750ms–2.6s REST round-trip exposure window remains an open empirical calibration question (deferred to Phase 5 calibration / future WebSocket order entry).
  - This trade-off is recorded explicitly here: **safety and exact inventory accounting are prioritized over fill rate**. Under no circumstances will the system sacrifice ledger solvency to save a few hundred milliseconds.

---

## 6. Hard Requirement: Real Filled Inventory for Downstream Legs and Reconciliation

In sequential live dispatch, **all downstream order sizing and all reconciliation calculations must use the REAL confirmed `filled_qty` and `avg_price` from actual exchange execution, NEVER theoretical plan values**:

1. **Leg 1 Sizing:** Must be derived from Leg 0's actual confirmed `OrderResult.filled_qty` (minus any base-asset commission deducted).
2. **Leg 2 Sizing:** Must be derived from Leg 1's actual confirmed `OrderResult.filled_qty` (minus any base-asset commission deducted).
3. **Reconciliation Sizing (`_reconcile_inventory`):**
   - If Leg 1 fails: The market liquidation order on Leg 0's pair must liquidate exactly the quantity of asset $A$ confirmed filled by Leg 0 (`order_results[0].filled_qty`), at its actual cost (`order_results[0].avg_price`).
   - If Leg 2 fails: The market liquidation order on Leg 1's pair must liquidate exactly the quantity of asset $B$ confirmed filled by Leg 1 (`order_results[1].filled_qty`), at its actual cost (`order_results[1].avg_price`).
   - Under no circumstances may `_reconcile_inventory` or downstream leg planning fall back to theoretical quantities from `_build_live_order_plan` or initial `position_usdt`.

**Historical Precedent:**  
This requirement is informed directly by the Phase 3 canonical-vs-execution-order bug (`pair_symbols` vs `Triangle.pair_ab`), where substituting an assumed/theoretical representation for the actual runtime sequence caused silent inversion of legs. Substituting theoretical quantities for real exchange-confirmed fills introduces the exact same class of fatal state drift. Real execution state is the sole source of truth.
