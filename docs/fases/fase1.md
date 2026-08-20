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

### Triangle graph (`feature/f1-graph-triangle-generation`)

`core/graph.py`:

- `filter_pairs_by_volume(raw_tickers, min_volume_usdt) → list[TradingPair]`:
  accepts raw ccxt tickers, keeps USDT-quoted pairs strictly above the volume
  threshold, tolerates missing/malformed `quoteVolume` fields (treated as
  zero), returns results sorted by volume descending.
- `generate_triangles(pairs) → list[Triangle]`:
  builds a `frozenset`-keyed pair index for O(1) edge lookups, constructs an
  adjacency list, enumerates all A-B-C-A cycles, canonicalises each triplet
  via lexicographic sort before inserting into a `seen` set — guaranteeing
  zero duplicate triangles regardless of graph size or traversal order.
  Returns results sorted by `(asset_a, asset_b, asset_c)` for deterministic
  output.
- Module is exchange-agnostic: no import of `BinanceAdapter` or `ccxt`.

### Test suite (`test/f1-graph-no-duplicates`)

`tests/test_graph.py` — 33 unit tests across three classes:

| Class | Tests | What is covered |
|---|---|---|
| `TestParseSymbol` | 8 | Slash format, native USDT suffix, lowercased input, non-USDT native → None, malformed, empty, bare "USDT" |
| `TestFilterPairsByVolume` | 12 | Empty list, all below, exactly at threshold (excluded), strictly above, mixed, non-USDT ignored, missing/malformed volume, sorted output, field accuracy, zero threshold, native symbol format |
| `TestGenerateTriangles` | 13 | Empty/1/2 pairs, 3 pairs no triangle, minimal triangle, no duplicates (symmetric pairs), no duplicates (larger graph), exact count (5 triangles in 8-pair graph), pair symbols valid, distinct assets, deterministic, sorted, isolated pair excluded |

All tests use fabricated data only.  No real API calls, no credentials.

`pytest.ini` added: `pythonpath = .` (project root on `sys.path`) and
`asyncio_mode = auto` (ready for async tests in Phase 2+).

### ADR-001 (`feature/f1-exchange-adapter-interface`)

`docs/adr/ADR-001-exchange-adapter-interface.md`: records the decision to
introduce the `ExchangeAdapter` ABC now rather than after Phase 6 requires it.
Covers context, decision, consequences, and three rejected alternatives.

---

## What was validated

- **33/33 unit tests pass** against `core/graph.py` with no mocking of
  external dependencies (the module has none).
- Manual import smoke-test: `python main.py` exits cleanly after importing
  `config.settings`, confirming the package structure is correct.
- `.gitignore` verified: `git status` on a branch with a `.env` file present
  shows the file as untracked, not staged — the exclusion works from the
  first commit.
- No credentials, API keys, or secrets appear anywhere in the repository,
  including test fixtures.

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

- **Non-USDT triangle legs**: Phase 1 now accepts cross pairs (like `ETH/BTC`)
  in the volume filter. For non-USDT-quoted pairs, `baseVolume` is compared
  directly against the USDT-denominated threshold (Phase 1 simplification to
  avoid a price-lookup loop). The consequence: a cross pair passes the filter
  if its base-asset volume is high, which is a reasonable proxy for liquidity
  but not a strict USDT-equivalent measure. True volume conversion (cross pair
  quantified in USDT using the quote asset's price) is deferred to Phase 2
  once real pair data has been observed and the overhead is justified.

  **USDT constraint** (now enforced): All triangles returned by
  `generate_triangles` must include USDT as one of the three assets. This
  reflects the bot's dependency on USDT as its held capital: a triangle with
  no USDT leg (e.g. BTC-ETH-BNB) cannot be entered or exited.

- **Leveraged token filtering**: the volume filter currently passes pairs like
  `BTC3LUSDT` if their volume exceeds the threshold.  These are not suitable
  for triangular arbitrage (they don't form valid triangles in practice and
  have different risk profiles).  A symbol-name blocklist or regex filter
  should be added in Phase 2 before the evaluator goes live.
