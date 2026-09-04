# ADR-010: Testnet Order Placement and Emergency Market Liquidation

## Status
Accepted

## Context

Phase 5 Stage 1 introduces the first real order placement code path in DeltaQuant. The executor needs two distinct exchange actions:

1. A Fill-or-Kill limit order for each arbitrage leg.
2. A market order for emergency liquidation when one leg fills and a later leg expires.

These are not interchangeable. FOK is suitable for the planned triangle legs because it either fills immediately at the limit price or exits with zero fill. Market orders are suitable only for reconciliation because they trade away price certainty in exchange for immediate execution.

The project is intentionally restricted to Binance TESTNET in this stage. Production live orders are not in scope and must remain impossible by construction.

## Decision

Add a separate `place_market_order(symbol, side, quantity) -> OrderResult` method to `ExchangeAdapter` rather than overloading `place_fok_order` with an order-type flag.

Live order placement is only allowed when all of the following are true:

- `DRY_RUN=False`
- `BINANCE_TESTNET=True`
- dedicated Binance TESTNET API credentials are present

Any other combination fails closed with a `PermissionError` or `ValidationError` before a real order is sent.

`BinanceAdapter` must:

- use Binance TESTNET endpoints when `BINANCE_TESTNET=True`
- log an unmistakable startup line indicating TESTNET vs PRODUCTION mode and whether live order placement is enabled or blocked
- validate order quantity and price locally against the market filters returned by `get_markets()` before submitting
- reject invalid sides, quantities, prices, or notional values locally rather than letting Binance reject them after a round-trip
- map the exchange response into the existing `OrderResult` dataclass without changing `is_filled` semantics

For reconciliation, the executor must use `place_market_order` after a failed FOK leg instead of attempting a second FOK. A second FOK would repeat the same failure mode that already occurred; a market order is the correct tool for unwinding inventory quickly.

## Consequences

Positive:
- The interface stays explicit: FOK and market orders are distinct operations.
- Production order placement remains impossible in this stage.
- Emergency liquidation uses the correct exchange primitive.
- Filter validation happens before any order request is sent.

Negative:
- The executor now needs the current path and price snapshot to build live order requests.
- The implementation is stricter and will reject malformed order construction earlier.
- In live reconciliation, the BUY-side liquidation quantity is derived from the cached `ask` snapshot available at failure handling time. If the book moved between initial dispatch and reconciliation, this can leave a small residual inventory amount even though the market order itself fills.

Known limitation and planned mitigation:
- This stage accepts the residual-risk trade-off and relies on the existing runbook check for leftover inventory.
- A future hardening step may fetch a fresh ticker immediately before BUY-side liquidation quantity calculation to reduce mismatch risk.

## Testing

Required tests for this ADR:
- live order placement is rejected when `BINANCE_TESTNET=False`
- `place_fok_order` normalizes or rejects quantities/prices according to the market filters
- `place_market_order` validates against a fresh ticker and market filters before sending
- live reconciliation calls `place_market_order` after a FOK failure
- no code path can send a production order while `DRY_RUN=True`

## References

- `exchanges/base.py`
- `exchanges/binance_adapter.py`
- `core/executor.py`
- `docs/adr/ADR-002-symbol-format-contract.md`
- `docs/adr/ADR-006-risk-limits-and-circuit-breaker.md`
