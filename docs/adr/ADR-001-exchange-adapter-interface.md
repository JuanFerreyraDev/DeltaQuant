# ADR-001: ExchangeAdapter Interface Design

## Status
Accepted

## Context

DeltaQuant begins with a single exchange (Binance) holding all real capital.
At this stage, a simple direct dependency on `ccxt.binance` throughout the
engine layer (`graph.py`, `evaluator.py`, `executor.py`) would be the path of
least resistance: fewer files, no abstraction overhead, faster to write.

However, two constraints documented in the technical plan (§1 "Principios de
diseño" and §8 Fase 6) make this a poor long-term choice:

1. **Planned multi-exchange expansion.** The roadmap explicitly targets a second
   CEX (e.g. Bybit) once Binance has been validated with real profitability data.
   Wiring exchange-specific logic directly into the engine means that expansion
   requires modifying files that already handle live capital — a risky change
   surface at a point in the project where the engine code will have been in
   production for months.

2. **The ccxt.pro escape hatch (§3.1).** If latency profiling in production
   reveals that ccxt's abstraction overhead is a bottleneck on the WebSocket
   parsing path, the plan calls for replacing the Binance WS client with a
   direct `websockets` + manual-parse integration.  Without an adapter layer,
   this substitution ripples into every file that touches market data.

A third, less obvious constraint: the technical plan mandates that Redis stays
out of the evaluation hot path (§1 "Redis fuera del hot path").  Having a clean
boundary between the engine layer and the exchange/infrastructure layer makes
it structurally easier to enforce this constraint — the engine only ever calls
`ExchangeAdapter` methods, which are memory-local and I/O-bounded to the
exchange itself, never to Redis.

## Decision

Define `ExchangeAdapter` as an abstract base class (Python `ABC`) in
`exchanges/base.py`.  All engine-layer modules import exclusively from this
module — never from `exchanges/binance_adapter.py` or any other concrete
adapter.

The interface exposes four async methods:

| Method | Purpose |
|---|---|
| `subscribe_book_ticker(symbols)` | Async generator yielding `BookTicker` snapshots from the WS stream |
| `place_fok_order(symbol, side, qty, price)` | Submit a Fill-or-Kill order, return `OrderResult` |
| `get_balance(asset)` | Fetch free/locked balance for one asset |
| `get_trading_fees(symbol)` | Fetch effective maker/taker rates (including any discount) |

Supporting data-transfer types (`BookTicker`, `Balance`, `TradingFees`,
`OrderResult`) are also defined in `exchanges/base.py` as frozen dataclasses so
that the engine layer can type-hint against them without importing concrete
adapter code.

`BinanceAdapter` is the sole concrete implementation in Phase 1.  It is the
only class that imports `ccxt`; no other module does.

## Consequences

**Positive:**
- Adding a second exchange in Phase 6 requires writing one new file
  (`exchanges/bybit_adapter.py`) and updating the wiring in `main.py`.
  `evaluator.py`, `executor.py`, and `graph.py` require zero changes.
- Replacing the ccxt.pro WS client with a direct `websockets` integration
  (the escape hatch described in §3.1) is scoped to `binance_adapter.py`
  only.
- The engine layer is fully testable with a mock or stub adapter — no live
  exchange connection required in any unit or integration test.
- The interface enforces the Redis-out-of-hot-path constraint structurally:
  the engine calls adapter methods, which are exchange I/O only.

**Negative / trade-offs:**
- Two extra files (`base.py` and the concrete adapter) instead of one.
- The `subscribe_book_ticker` abstract method must be declared as an async
  generator (`AsyncIterator`), which requires a `yield` stub in the ABC body
  — a slightly non-obvious Python pattern.
- Any exchange-specific feature not expressible through the four interface
  methods (e.g. Binance-specific order types) requires either extending the
  interface (touching all adapters) or accessing the underlying client
  directly (breaking the abstraction).  For Phase 1 scope this is not a
  concern; it becomes a decision point if/when a non-standard order type is
  needed.

## Alternatives considered

**1. Direct ccxt dependency throughout the engine (rejected)**
Would work for a single-exchange lifetime.  Rejected because the Phase 6
multi-exchange requirement is explicit in the plan, and retrofitting an
abstraction layer after months of production operation is higher-risk than
building it now when the engine is new.

**2. Dependency injection without a formal ABC (rejected)**
Passing the ccxt client as a parameter without an ABC would achieve loose
coupling but provide no compile-time or static-analysis guarantee that a new
adapter implements the required interface.  The ABC raises `TypeError` at
instantiation time if any abstract method is missing, which is a safer
failure mode than a runtime `AttributeError` discovered during live trading.

**3. Event-bus / message-passing architecture (deferred)**
Decoupling via an async queue (exchange → queue → engine) would provide even
stronger isolation and enable fan-out to multiple consumers.  Rejected for
Phase 1 because it adds non-trivial complexity before the basic pipeline is
validated.  Revisit if/when Phase 6 introduces cross-exchange arbitrage
requiring true fan-out.
