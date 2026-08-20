# ADR-002: Symbol Format Contract at Engine/Exchange Boundary

## Status
Accepted

## Context

`core/graph.py` produces trading pair symbols and stores them in
`TradingPair.symbol` and `Triangle.pair_ab/pair_bc/pair_ca`. These symbols
flow to the exchange adapter (`BinanceAdapter`) for fee lookups, WebSocket
subscriptions, and order placement. `ccxt` expects its own unified slash
format (e.g. `"BTC/USDT"`).

Without an explicit contract, every adapter method that receives a symbol
must know which format to expect — and every caller must know which format
to supply. This is a brittle, implicit coupling that fails silently until a
live fee lookup or order placement hits the wrong format.

**The original bug (Phase 1):** `BinanceAdapter.get_trading_fees` was passed
`"BTCUSDT"` but forwarded it unchanged to `ccxt.binance.fetch_trading_fee()`,
which expects `"BTC/USDT"`. This was not caught until Phase 2 code tried to
call the method with a real symbol from a `Triangle`.

**Why the format question is not deferred to Phase 6:** When cross-pair volume
filtering was introduced (fix/f1-graph-usdt-filter-and-triangle-constraint),
`graph.py` began producing triangles with non-USDT legs (e.g. `ETH/BTC`).
The evaluator in Phase 2 will immediately call `get_trading_fees` for all
three legs of a triangle, including the cross-pair leg. The symbol format for
cross pairs therefore had to be decided now, not at Phase 6.

## Decision

The engine/exchange boundary uses a **mixed native format** with two distinct
sub-formats, each unambiguous in isolation:

| Pair type | Symbol format | Example | Parseable by `parse_symbol`? |
|---|---|---|---|
| USDT-quoted | Concatenated, no slash | `"BTCUSDT"` | Yes — fixed 4-char `USDT` suffix |
| Cross pair (non-USDT quote) | Slash-delimited | `"ETH/BTC"` | Yes — slash is unambiguous |

**Why two formats instead of one unified format everywhere?**

Concatenated `"BTCUSDT"` is retained for USDT pairs because it is the natural
Binance native format, `parse_symbol` already handles it, and changing it would
break existing tests and the established contract. Slash-delimited `"ETH/BTC"`
is used for cross pairs because concatenation is ambiguous without a
known-assets list (`"ETHBTC"` could be `ETH/BTC` or `ET/HBTC`) and
`parse_symbol("ETHBTC")` returns `None` — the evaluator would be unable to
recover base/quote at fee-lookup time.

The adapter is the single place responsible for translating both formats to
whatever `ccxt` requires. The engine layer (`graph.py`, `evaluator.py`,
`executor.py`) never calls `ccxt` directly and never needs to know about ccxt
conventions.

## Implementation

### `parse_symbol` round-trip guarantee

Every symbol stored in a `TradingPair` or `Triangle` must satisfy:

```python
parse_symbol(symbol) is not None
```

This property is tested explicitly in `test_cross_pair_symbol_round_trips_through_parse_symbol`
(tests/test_graph.py). Concatenated cross-pair symbols (`"ETHBTC"`) violate it
and are therefore not produced by the filter.

### Conversion helpers in `exchanges/binance_adapter.py`

**`_native_to_unified(symbol) → str`**

Converts engine-format symbols to ccxt unified format:

- Slash-delimited input (e.g. `"ETH/BTC"`): returned unchanged — already unified.
- Concatenated USDT input (e.g. `"BTCUSDT"`): splits at the 4-char `USDT`
  suffix → `"BTC/USDT"`.
- Anything else: returned as-is; the resulting ccxt `BadSymbol` exception
  surfaces the error at the adapter boundary, not deep in the engine.

**`_unified_to_native(symbol) → str`**

Safe only for USDT-quoted unified symbols (e.g. `"BTC/USDT"` → `"BTCUSDT"`).
Do **not** call on cross-pair unified symbols — `"ETH/BTC".replace("/", "")`
produces the ambiguous `"ETHBTC"` that `parse_symbol` cannot recover.

### Adapter methods affected

| Method | Symbol handling |
|---|---|
| `get_trading_fees(symbol)` | Calls `_native_to_unified`; cache key is the input symbol (engine format). Handles both USDT and cross-pair symbols. |
| `subscribe_book_ticker(symbols)` | Phase 2. Will call `_native_to_unified` per symbol before the ccxt.pro WebSocket call. |
| `place_fok_order(symbol, ...)` | Phase 3. Will call `_native_to_unified` before the ccxt order call. |

## Consequences

**Positive:**
- `parse_symbol(s) is not None` holds for every symbol in the engine — the
  evaluator can always reconstruct base/quote for fee lookups and order routing.
- The adapter is the unique translation boundary. Engine modules stay
  exchange-agnostic; they never import ccxt or know about its conventions.
- Both sub-formats are unambiguous and already handled by `parse_symbol`, so no
  new parsing logic is needed in the engine.
- The mixed format is fully backwards-compatible: existing USDT-pair code,
  tests, and the original `get_trading_fees` contract are unchanged.

**Negative / trade-offs:**
- Two symbol formats in the codebase require documentation (this ADR) and
  test coverage of both paths. Reviewers must know the rule: USDT → concatenated,
  cross → slash. This is enforced by `parse_symbol` round-trip tests, not by
  the type system.
- `_unified_to_native` is a footgun if called on cross-pair unified symbols.
  Its docstring states the constraint explicitly; it has no call-sites for cross
  pairs in Phase 1.
- If a future exchange adapter needs a third format (e.g. Bybit uses
  `"BTC-USDT"` with a dash), it must define its own conversion helpers. The
  mixed-format rule is specific to `BinanceAdapter`; the `ExchangeAdapter` ABC
  does not mandate any particular format beyond "whatever the engine produces."

## Alternatives considered

**1. Slash-delimited unified format everywhere (rejected)**
Use `"BTC/USDT"` and `"ETH/BTC"` for all symbols, including USDT pairs.
Rejected because it would break all existing USDT-pair tests and the
established `"BTCUSDT"` contract without any correctness gain — `parse_symbol`
already handles the concatenated USDT form correctly.

**2. Concatenated format everywhere — wait for a known-assets list (deferred)**
Extend `parse_symbol` with a Binance market-metadata list so it can split
`"ETHBTC"` correctly. This would allow a single concatenated format. Rejected
for Phase 1 because it requires `load_markets()` to be called before any
symbol is parsed — adding a network dependency to a pure function. Revisit
in Phase 2 if the mixed format creates friction with the evaluator.

**3. No canonical format — left to caller (rejected)**
Each call site decides which format to use. Leads to the original
`get_trading_fees` bug and makes the interface untestable without knowing which
format was passed. Rejected unconditionally.

## References

- **ADR-001**: ExchangeAdapter interface. Establishes that engine modules never
  import concrete adapters.
- **ADR-003**: Volume normalisation for cross-asset pairs. Records the decision
  to include non-USDT pairs in the volume filter, which is what made the symbol
  format question relevant in Phase 1 rather than Phase 6.
- **`fix/f1-graph-usdt-filter-and-triangle-constraint`**: Introduces cross-pair
  volume filtering and the slash-delimited format for cross-pair symbols.
- **`fix/f1-symbol-format-contract`** (this branch): Fixes `_native_to_unified`
  to handle both formats and updates this ADR.
- **Technical Plan §3 "Flujo de evaluación"**: Symbol flow from graph →
  evaluator → executor; all layers use the same engine format.
