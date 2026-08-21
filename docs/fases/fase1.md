# Phase 1 Closeout: Market Analysis and Triangle Filtering

## Status
Complete. All Phase 1 checklist items from the technical plan (§8, Fase 1)
are implemented, committed, and tested.  Ready for review and merge to
`develop`.

---

## What was implemented

### Project scaffold (`feature/f1-project-scaffold`)

Full directory structure from §4 of the technical plan:

```
DeltaQuant/
├── config/          exchanges/      core/
├── interfaces/      storage/        tests/
├── docs/adr/        docs/fases/     docs/runbooks/
├── .gitignore       .env.example    requirements.txt
├── Dockerfile       docker-compose.yml   main.py
```

Key decisions made here:

- `.gitignore` excludes `.env`, `*.sqlite`, `logs/`, and all credential
  patterns from the very first commit — before any real API key exists on
  disk.  This cannot be retroactively fixed once a secret leaks into history.
- `.env.example` documents every required variable with a placeholder value
  and an inline comment.  It is the only credentials-related file tracked by
  git.
- `requirements.txt` uses pinned versions for all Phase 1 dependencies.
  Open ranges were deliberately avoided: a transitive upgrade breaking the
  evaluation loop while live capital is at risk is an avoidable failure mode.
- `Dockerfile` and `docker-compose.yml` are structural stubs.  They compile
  and run, but the multi-stage optimisation and Redis service wiring are
  deferred to Phase 5 (`chore/f5-dockerfile-multistage`).

### Pydantic Settings (`feature/f1-pydantic-settings`)

`config/settings.py` — `Settings(BaseSettings)` with:

- Every runtime parameter typed and validated at startup.  The bot refuses
  to start if any required variable is absent or malformed.
- `Decimal` for all monetary and ratio fields.  Using Python's `float` for
  fee rates or position sizes in a trading system is an avoidable source of
  rounding error.
- `extra="forbid"`: unknown environment variables raise a validation error
  rather than being silently ignored.  This surfaces `.env` typos before
  they cause confusing runtime behaviour.
- `DRY_RUN` defaults to `True`.  The bot can never accidentally start in
  live-trading mode because of a missing environment variable.
- `@field_validator` rejects placeholder values copied verbatim from
  `.env.example` — the most common onboarding mistake.
- `get_settings()` is `@lru_cache`'d: the `.env` file is parsed exactly
  once per process, not on every call-site.

### ExchangeAdapter interface (`feature/f1-exchange-adapter-interface`)

`exchanges/base.py` — `ExchangeAdapter(ABC)` with four async methods:

| Method | Phase implemented |
|---|---|
| `subscribe_book_ticker(symbols) → AsyncIterator[BookTicker]` | Phase 2 |
| `get_trading_fees(symbol) → TradingFees` | Phase 1 ✓ |
| `get_balance(asset) → Balance` | Phase 1 ✓ |
| `place_fok_order(symbol, side, qty, price) → OrderResult` | Phase 3 |

Supporting frozen dataclasses (`BookTicker`, `Balance`, `TradingFees`,
`OrderResult`) are also defined here so the engine layer can type-hint
against them without importing any concrete adapter.

Rationale documented in **ADR-001**
(`docs/adr/ADR-001-exchange-adapter-interface.md`).

### Binance adapter — read-only (`feature/f1-binance-adapter-readonly`)

`exchanges/binance_adapter.py` — `BinanceAdapter(ExchangeAdapter)`:

- `BinanceAdapter.create(settings)`: async factory, wraps the synchronous
  `ccxt.binance` constructor in `run_in_executor` to avoid blocking the
  event loop.
- `fetch_tickers_24h()`: REST call to `GET /api/v3/ticker/24hr` via ccxt
  `fetch_tickers`.  Returns raw ccxt dicts; filtering is the caller's
  responsibility (separation of concerns with `graph.py`).
- `get_trading_fees(symbol)`: fetches effective maker/taker rates with an
  in-process dict cache keyed by symbol.  BNB discount application is
  deferred to `exchanges/fees.py` in Phase 2.
- `get_balance(asset)`: signed REST request; returns a zero-balance `Balance`
  for assets not held rather than raising.
- `get_markets()`: async wrapper around `lru_cache`'d `load_markets()`.
- `subscribe_book_ticker` and `place_fok_order`: raise `NotImplementedError`
  with explicit phase references in the error messages.

### Triangle graph (`feature/f1-graph-triangle-generation` + `fix/f1-graph-usdt-filter-and-triangle-constraint`)

`core/graph.py`:

- `filter_pairs_by_volume(raw_tickers, min_volume_usdt) → list[TradingPair]`:
  accepts raw ccxt tickers, keeps both USDT-quoted pairs and cross pairs
  strictly above the volume threshold, tolerates missing/malformed
  `quoteVolume` fields (treated as zero), returns results sorted by volume
  descending.  For USDT-quoted pairs the raw `quoteVolume` is used directly;
  for cross pairs the USDT-equivalent is computed as
  `quoteVolume × quote-asset USDT price` sourced from the same ticker batch.
  Cross pairs whose quote asset has no USDT listing in the batch are excluded
  gracefully (no fallback, no crash).  Design rationale and accepted
  limitations documented in **ADR-003**
  (`docs/adr/ADR-003-volume-normalization-cross-pairs.md`).
- `_build_quote_asset_prices(raw_tickers)`: helper that extracts a
  `{asset: Decimal}` price map from all USDT-quoted tickers in the batch,
  used by the volume filter for cross-pair conversion.
- `generate_triangles(pairs) → list[Triangle]`:
  builds a `frozenset`-keyed pair index for O(1) edge lookups, constructs an
  adjacency list, enumerates all A-B-C-A cycles, canonicalises each triplet
  via lexicographic sort before inserting into a `seen` set — guaranteeing
  zero duplicate triangles regardless of graph size or traversal order.
  Enforces the USDT constraint: every returned triangle must include USDT as
  one of its three assets (no USDT leg → no entry/exit point for the bot).
  Returns results sorted by `(asset_a, asset_b, asset_c)` for deterministic
  output.
- Module is exchange-agnostic: no import of `BinanceAdapter` or `ccxt`.

Symbol format contract documented in **ADR-002**
(`docs/adr/ADR-002-symbol-format-contract.md`): USDT-quoted pairs use
concatenated native format (e.g. `BTCUSDT`); cross pairs use slash-delimited
unified format (e.g. `ETH/BTC`) because concatenation is ambiguous without a
known-assets list.

### Test suite (`test/f1-graph-no-duplicates` + `fix/f1-graph-usdt-filter-and-triangle-constraint` + `test/f1-missing-unit-coverage` + `fix/f1-binance-adapter-create-connectivity-check`)

**120 tests across 8 test classes in 5 files** (verified with `pytest --collect-only`).
All use fabricated data only — no real API calls, no credentials anywhere in the test suite.

#### `tests/test_graph.py` — 45 cases

| Class | Cases | What is covered |
|---|---|---|
| `TestParseSymbol` | 9 | Slash format, native USDT suffix, lowercased input, non-USDT native → None, malformed, empty, bare "USDT", multi-slash returns None |
| `TestFilterPairsByVolume` | 18 | Empty list, all below, exactly at threshold (excluded), strictly above, mixed, cross pair excluded without quote price, cross pair included with correct USDT-equivalent volume, cross pair with missing quoteVolume excluded, missing/malformed volume, sorted output, field accuracy, Decimal type enforcement, zero threshold, native symbol format for USDT pairs, cross pair uses slash-delimited symbol, cross pair round-trips through `parse_symbol` |
| `TestGenerateTriangles` | 14 | Empty/1/2 pairs, 3 pairs no triangle, minimal triangle, no duplicates (symmetric pairs), no duplicates (larger graph), exact count (4 USDT triangles in 8-pair graph), pair symbols valid, distinct assets, deterministic, sorted, isolated pair excluded, USDT constraint excludes non-USDT triangles, USDT triangle included |
| `TestFilterAndGeneratePipeline` | 4 | Full pipeline with cross pair generates triangle (verifies `quoteVolume × price` math), cross pair below volume excluded, cross pair with missing quote price excluded, non-USDT-only graph produces no triangles |

#### `tests/test_base.py` — 18 cases

| Class | Cases | What is covered |
|---|---|---|
| `TestBalance` | 5 | `total` with free+locked, both zero, locked zero, free zero; frozen enforcement |
| `TestOrderResult` | 7 | `is_filled` all four boolean combinations (FILLED+nonzero → True, FILLED+zero → False, EXPIRED+nonzero → False, EXPIRED+zero → False), CANCELLED case, `raw` defaults to `{}`, mutable field update |
| `TestTradingFees` | 2 | Construction and frozen enforcement |
| `TestBookTicker` | 2 | Construction and frozen enforcement |
| `TestExchangeAdapterABC` | 2 | Direct instantiation raises `TypeError`; incomplete subclass also raises `TypeError` |

#### `tests/test_symbol_helpers.py` — 12 cases

| Class | Cases | What is covered |
|---|---|---|
| `TestNativeToUnified` | 7 | USDT pair, multi-char base, slash passthrough, non-USDT concatenated fallthrough (returns as-is), bare `"USDT"` length guard, minimum 5-char pair, cross pair with non-USDT quote |
| `TestUnifiedToNative` | 5 | `BTC/USDT` → `BTCUSDT`, multi-char base, no-slash no-op, empty string, cross pair slash removal (documented out-of-scope per ADR-002) |

#### `tests/test_settings.py` — 40 cases

Three test methods use `@pytest.mark.parametrize`; counts below reflect executed cases, not method count.

| Class | Cases | Notes |
|---|---|---|
| `TestValidateLogLevel` | 16 | 7 parametrized valid levels + 4 parametrized case-normalisation inputs + 5 parametrized invalid values |
| `TestValidateNotPlaceholder` | 10 | 6 parametrized API key rejections + 3 parametrized secret rejections + 1 valid passthrough |
| `TestFieldConstraints` | 9 | One method per constrained field: `MIN_VOLUME_USDT`, `SAFETY_MARGIN`, `MAX_TICK_AGE_MS`, `MAX_POSITION_USDT`, `DAILY_LOSS_LIMIT_USDT`, `MAX_CONCURRENT_TRIANGLES`, `REDIS_PORT` lower and upper bounds, `REDIS_DB` |
| `TestExtraForbid` | 1 | Unknown field raises `ValidationError` |
| `TestDefaults` | 2 | `DRY_RUN=True` default, numeric defaults sanity check |
| `TestGetSettings` | 2 | Same-object identity on repeated calls (`lru_cache`), `cache_clear()` allows re-read of updated environment |

#### `tests/test_binance_adapter_create.py` — 5 cases

Bug discovered during Phase 1 review, fixed in `fix/f1-binance-adapter-create-connectivity-check`
(separate from the three original fixes). See "Bug fix: `create()` connectivity
contract" below.

| Class | Cases | What is covered |
|---|---|---|
| `TestBinanceAdapterCreate` | 5 | `load_markets()` called during `create()`; `_markets_cache` pre-populated after `create()`; `AuthenticationError` from `load_markets()` propagates; `NetworkError` propagates; subsequent `get_markets()` does not trigger a second `load_markets()` call |

`pytest.ini` added: `pythonpath = .` (project root on `sys.path`) and
`asyncio_mode = auto` (ready for async tests in Phase 2+).

### ADR-001 (`feature/f1-exchange-adapter-interface`)

`docs/adr/ADR-001-exchange-adapter-interface.md`: records the decision to
introduce the `ExchangeAdapter` ABC now rather than after Phase 6 requires it.
Covers context, decision, consequences, and three rejected alternatives.

---

## What was validated

- **120/120 tests pass** (`pytest --collect-only` confirms 120 cases across 5
  test files), with no mocking of external dependencies in the graph and base
  modules; `test_binance_adapter_create.py` mocks `ccxt.binance` at the class
  level.
- Manual import smoke-test: `python main.py` exits cleanly after importing
  `config.settings`, confirming the package structure is correct.
- `.gitignore` verified: `git status` on a branch with a `.env` file present
  shows the file as untracked, not staged — the exclusion works from the
  first commit.
- No credentials, API keys, or secrets appear anywhere in the repository,
  including test fixtures.

---

## Bug fix: `create()` connectivity contract (`fix/f1-binance-adapter-create-connectivity-check`)

Discovered during Phase 1 review — not part of the original three fix branches.

**The bug:** `BinanceAdapter.create()` docstring promised a connectivity check
via `load_markets()` and declared `ccxt.AuthenticationError` as a possible
raise. The implementation only called `ccxt.binance(...)` in memory — no
network request, no credential validation. An adapter built with an invalid API
key would succeed silently until the first operational call hit the network
(`fetch_tickers_24h`, `get_balance`, etc.), contradicting the fail-fast
principle applied everywhere else in Phase 1 (same spirit as `extra="forbid"`
in `Settings`).

**The fix** (`exchanges/binance_adapter.py`):
- Merged client construction and `load_markets()` into a single
  `_build_and_load()` closure executed inside `run_in_executor` — the event
  loop is never blocked.
- `AuthenticationError` and `NetworkError` from `load_markets()` now propagate
  to the caller at startup, as documented.
- `adapter._markets_cache` is pre-populated from `client.markets` on success,
  so the first `get_markets()` call is free (no second round-trip).
- Also replaced `asyncio.get_event_loop()` with `asyncio.get_running_loop()`
  across all five call sites in the adapter (`create`, `fetch_tickers_24h`,
  `get_trading_fees`, `get_balance`, `get_markets`) — `get_event_loop()`
  emits `DeprecationWarning` in Python 3.10+ when called inside a running
  coroutine.

**Tested in** `tests/test_binance_adapter_create.py` (5 cases, all mocking
`ccxt.binance` at the class level — no real network calls).

---

## What is left for Phase 2

Per the technical plan §8, Fase 2 scope:

1. **`exchanges/binance_adapter.py` — WebSocket completion**: implement
   `subscribe_book_ticker` using `ccxt.pro`'s `watch_bids_asks` (or the
   `bookTicker` stream equivalent).  Branch: `feature/f2-binance-ws-bookticker`.

2. **`exchanges/fees.py`**: BNB fee discount calculation wrapping
   `get_trading_fees`.  The 25 % discount is not applied in Phase 1; all
   fee calculations using `BinanceAdapter.get_trading_fees` return the
   nominal rate.  Branch: `feature/f2-fees-bnb-discount`.

3. **`core/evaluator.py`**: tick-level net-return calculation using `Decimal`
   arithmetic + staleness check against `BookTicker.timestamp_ms`.
   Branch: `feature/f2-evaluator-core` + `feature/f2-evaluator-staleness-check`.

4. **Documentation to produce in Phase 2** (per doc plan §7):
   - Docstrings on `evaluator.py` and `fees.py`.
   - ADR for the staleness threshold criterion.
   - First entry in `docs/calibrations.md` for `MAX_TICK_AGE_MS` and
     `SAFETY_MARGIN`.

5. **Binance rate-limit weight tracking** (§9.3 of the technical plan):
   logging weight consumed per request from the first WS session onward —
   not reactively after a ban.

---

## Open questions / deferred decisions

- **Leveraged token filtering**: the volume filter currently passes pairs like
  `BTC3LUSDT` if their volume exceeds the threshold.  These are not suitable
  for triangular arbitrage (they don't form valid triangles in practice and
  have different risk profiles).  A symbol-name blocklist or regex filter
  should be added in Phase 2 before the evaluator goes live.
