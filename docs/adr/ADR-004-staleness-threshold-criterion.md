# ADR-004: Tick Staleness Threshold Criterion

## Status
Accepted

## Context

In real-time triangular arbitrage, price updates for all three legs of a triangle arrive asynchronously via WebSocket streams (`subscribe_book_ticker`). When `core/evaluator.py` receives a price update for leg $A \leftrightarrow B$, it evaluates the net return of the triangle $A \to B \to C \to A$ using the most recent cached `BookTicker` snapshots available for $B \leftrightarrow C$ and $C \leftrightarrow A$.

If any of those cached snapshots is out of date (due to network jitter, WebSocket buffer delay, low liquidity on a cross pair, or event loop lag), the mathematical return calculated by the evaluator represents a past market state rather than the current execution environment on the matching engine.

Executing FOK orders against stale prices leads to predictable failures:
- **Low fill rate**: FOK limit orders submitted at outdated prices fail to execute immediately and expire with zero fill.
- **Slippage and leg desynchronisation**: In rare cases where a stale leg fills but subsequent legs fail, the bot is left holding unhedged inventory.
- **Wasted rate-limit weight**: Submitting doomed orders consumes API rate-limit weight without opportunity for profit.

The evaluator requires a numerical threshold `MAX_TICK_AGE_MS` to classify tickers as fresh or stale.

## Decision

Set `MAX_TICK_AGE_MS = 200` (200 milliseconds) as the maximum allowable tick age for real-time triangle evaluation in DeltaQuant Phase 2.

The timestamp evaluated is `BookTicker.timestamp_ms`, which records the local Unix timestamp in milliseconds when the WS message was received by `BinanceAdapter.subscribe_book_ticker`.

A triangle evaluation is flagged `is_stale = True` (and discarded with `is_profitable = False`) if:

$$\text{current\_time\_ms} - \text{ticker.timestamp\_ms} > 200\text{ ms}$$

or if any tick has a future timestamp ($\text{age} < 0$, indicating clock skew).

### Numerical Justification

The choice of `MAX_TICK_AGE_MS = 200` ms is an **initial heuristic estimate** designed to establish a safe baseline for Phase 2 before empirical latency data is gathered during Phase 3 dry-run monitoring.

It is derived as an upper bound from the following operational parameters:

1. **Order Book Stability Window**: Empirical crypto order book microstructure studies indicate top-of-book quotes for major assets (BTC, ETH, USDT) degrade significantly beyond 250 ms–300 ms.
2. **Estimated REST Execution Latency**: Placing 3 parallel FOK orders via REST `asyncio.gather` on a cloud VPS in the exchange's region (AWS Tokyo / eu-central-1) is expected to take ~50 ms–100 ms (network RTT + exchange matching latency).
3. **Derived Upper Bound**: Subtracting expected execution latency (~50 ms–100 ms) from the stability window (~250 ms–300 ms) leaves a maximum tolerable tick age of ~150 ms–200 ms at evaluation time.

> [!NOTE]
> `MAX_TICK_AGE_MS = 200` is explicitly classified as an **initial heuristic starting point**, not a mathematically proven constant. It will be recalibrated in `docs/calibrations.md` using empirical P95/P99 latency distributions collected during Phase 3 passive dry-run execution.

## Implementation

- Defined in `config/settings.py` as `MAX_TICK_AGE_MS: int = Field(default=200, ge=10, le=5000)`.
- Enforced in `core/evaluator.py`: `evaluate_path` checks every leg's timestamp against `current_time_ms - max_tick_age_ms`.
- Verified in `tests/test_evaluator.py` under `TestStalenessCheck`.

## Consequences

**Positive:**
- Eliminates execution attempts against stale order books, preserving API rate-limit weight and preventing FOK expirations.
- Protects capital from phantom spreads caused by asynchronous WebSocket delivery.
- Simple, deterministic RAM-local check with zero performance overhead in the evaluation hot path.

**Negative / Trade-offs:**
- Low-liquidity cross pairs (e.g. `ETH/BTC` during quiet hours) may experience update intervals $> 200\text{ms}$, causing valid opportunities to be discarded due to staleness.
- Requires system clock synchronization (NTP) on the host VPS to prevent false staleness triggers due to clock drift.

## Alternatives Considered

**1. No staleness check — execute whenever net_return > margin (rejected)**
Discards freshness information. Led to the Phase 1 defects where non-viable spreads were evaluated as profitable against dead book snapshots.

**2. Tight threshold (e.g. 50 ms) (rejected for Phase 2)**
Too aggressive for cloud VPS deployments using public WS streams via `ccxt.pro`. Would filter out >95% of valid opportunities due to standard Internet latency jitter.

**3. Dynamic staleness threshold per symbol (deferred)**
Setting tighter thresholds for USDT high-volume pairs and looser thresholds for cross pairs. Deferred until Phase 3 dry-run empirical data provides per-symbol latency distributions.

## References
- **Technical Plan §3, §7**: Evaluation flow and staleness detection requirements.
- **ADR-001**: ExchangeAdapter interface contract.
- **`config/settings.py`**: `MAX_TICK_AGE_MS` setting definition.
