# Phase 2 Closeout: WebSocket Connectivity and Real-Time Evaluation

## Status
Complete. All Phase 2 checklist items from the technical plan (§8, Fase 2) and PRD are fully implemented, tested, and committed across dedicated feature branches. Ready for review before integration into `develop`.

---

## What was implemented

### 1. Binance WebSocket `subscribe_book_ticker` & Weight Tracking (`feature/f2-binance-ws-bookticker`)

`exchanges/binance_adapter.py`:

- **WebSocket stream integration**: Implemented `subscribe_book_ticker` using `ccxt.pro.binance.watch_bids_asks` to stream best bid/ask updates asynchronously.
- **ADR-002 symbol handling**: Converts engine-native symbols (concatenated `BTCUSDT`, slash-delimited `ETH/BTC`) into ccxt unified format via `_native_to_unified` before invoking `watch_bids_asks`. Yields frozen `BookTicker` objects with high-resolution local timestamps (`timestamp_ms`).
- **Test harness teardown & Error logging**: Catches `StopAsyncIteration` to allow deterministic test loop completion, while real WebSocket network failures in production are caught by `except Exception`, logged at `ERROR` level, and re-raised as `ConnectionError`. Stream termination triggers a `finally` block logging a `WARNING` so WebSocket drops are easily identified in log files.
- **Proactive rate-limit weight tracking**: Implemented `_track_weight_from_headers(response_headers)` and `get_last_used_weight()` to monitor `x-mbx-used-weight-1m` HTTP headers proactively on every REST and WebSocket request, logging warning thresholds before hitting API bans (technical plan §9.3).
- **Async event loop safety**: Refactored all adapter calls to use `asyncio.get_running_loop()` instead of `asyncio.get_event_loop()`, eliminating Python 3.10+ deprecation warnings.

### 2. BNB Fee Discount Calculation (`feature/f2-fees-bnb-discount`)

`exchanges/fees.py`:

- **Centralised discount arithmetic**: Wraps `TradingFees` with `apply_bnb_discount`, reducing maker and taker fee rates by 25 % (`BNB_DISCOUNT_RATE = Decimal("0.25")`).
- **Strict Decimal enforcement**: Guarantees all calculations use `Decimal` arithmetic, preventing float rounding drift across thousands of tick evaluations.
- **Convenience helper**: `get_effective_fees(adapter, symbol, use_bnb_discount=True)` combines raw fee retrieval from `ExchangeAdapter` with optional BNB discount processing.
- **Symbol format preservation**: Preserves input symbol string format intact (both concatenated USDT and slash-delimited cross pairs).

### 3. Core Evaluator (`feature/f2-evaluator-core`)

`core/evaluator.py`:

- **Tick-level net-return calculation**: Computes gross and net return multipliers for triangular arbitrage cycles ($A \to B \to C \to A$) using exact `Decimal` arithmetic.
- **Bid/Ask leg evaluation**: `evaluate_leg` calculates conversion factors for both BUY (quote $\to$ base via `ask`) and SELL (base $\to$ quote via `bid`) legs, applying leg taker fees exact to 18 decimal places.
- **Directional path evaluation**: `evaluate_triangle` evaluates both directional paths starting from USDT (e.g. `USDT -> BTC -> ETH -> USDT` and `USDT -> ETH -> BTC -> USDT`), returning `EvaluationResult` objects sorted by `net_return` descending.
- **Profitability check**: Compares `net_return` against `1.0 + safety_margin` to determine execution readiness.

### 4. Book Staleness Check (`feature/f2-evaluator-staleness-check`)

`core/evaluator.py` & `docs/adr/ADR-004-staleness-threshold-criterion.md`:

- **Staleness threshold enforcement**: `evaluate_path` checks tick timestamp age ($\text{current\_time\_ms} - \text{ticker.timestamp\_ms}$) against `MAX_TICK_AGE_MS`.
- **Automatic filtering**: Triangles containing any tick older than `MAX_TICK_AGE_MS` (or with future timestamp clock skew) set `is_stale = True` and `is_profitable = False`, preventing execution against dead order book snapshots.
- **ADR-004**: Formally recorded the 200 ms staleness threshold criterion and its numerical justification based on cloud VPS execution budgets and order book microstructure decay times.

---

## What was validated

### Test suite

- **152 tests passing** across 8 test modules (`pytest`).
- **Strict testing principles applied**:
  - All return math verified by hand in test docstrings (e.g. $1.012$ gross, $1.00972470732306250$ net return).
  - Explicit type assertions (`assert type(res.net_return) is Decimal`) to ensure no float contamination.
  - Rejection of invalid float parameters in `evaluate_leg` and `evaluate_path`.
  - Integration tests constructed with real `Triangle` objects generated from `core.graph`.

---

## Documentation produced in Phase 2

1. **Docstrings**: Added Google-style English docstrings across `evaluator.py` and `fees.py`.
2. **ADR-004**: `docs/adr/ADR-004-staleness-threshold-criterion.md` detailing the 200ms tick staleness threshold criterion.
3. **Calibrations**: Created `docs/calibrations.md` with initial entries for `MAX_TICK_AGE_MS = 200` and `SAFETY_MARGIN = 0.0010`.
4. **Phase 2 Closeout**: `docs/fases/fase2.md` (this file).

---

## Branch Summary for Review

| Checklist Item | Branch Name | Key Files Modified / Created |
|---|---|---|
| ccxt.pro WebSocket & Rate Limit | `feature/f2-binance-ws-bookticker` | `exchanges/binance_adapter.py`, `tests/test_binance_ws_and_weight.py`, `requirements.txt` |
| BNB Fee Discount | `feature/f2-fees-bnb-discount` | `exchanges/fees.py`, `tests/test_fees_bnb_discount.py` |
| Evaluator Core | `feature/f2-evaluator-core` | `core/evaluator.py`, `tests/test_evaluator.py` |
| Staleness Check & ADR | `feature/f2-evaluator-staleness-check` | `core/evaluator.py`, `tests/test_evaluator.py`, `docs/adr/ADR-004-staleness-threshold-criterion.md`, `docs/calibrations.md`, `docs/fases/fase2.md` |

---

## What is left for Phase 3

Per technical plan §8, Fase 3 scope:

1. **`storage/database.py`**: SQLite async with WAL mode & `models.py` (including `Incidents` table for failed legs).
2. **`core/risk.py`**: Capital limits, max position size per trade, daily loss limit auto-pause breaker, max concurrent triangles limit.
3. **`core/executor.py` in Dry Run mode**: Parallel 3-leg FOK dispatch via `asyncio.gather` + reconciliation logic simulating leg execution failures.
4. **Dry Run validation**: Passive monitoring for several days in `DRY_RUN=True` mode to validate theoretical fill rate and net PnL before live capital deployment.
