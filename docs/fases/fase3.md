# Phase 3: Simulation, Risk, and Persistence — Closeout

## Status
**TESTS PASSING / DRY-RUN VALIDATION PENDING** — see §6 for the explicit distinction.

---

## What was implemented

### Modules delivered

| Module | Description |
|---|---|
| `storage/models.py` | SQLAlchemy ORM models: `Trade`, `Incident`, `Metric` |
| `storage/database.py` | `DatabaseManager`: async SQLite with WAL mode, session lifecycle, PRAGMA configuration |
| `core/risk.py` | `RiskManager`: position size cap, daily loss limit, concurrency limit, circuit breaker |
| `core/executor.py` | `Executor`: DRY_RUN parallel dispatch, inventory reconciliation, incident logging |
| `docs/runbooks/reconciliation.md` | Operator runbook: reconciliation mechanism, investigation protocol, resume steps |
| `docs/adr/ADR-005-sqlite-async-wal-mode.md` | ADR for SQLite WAL mode selection and trade-offs |
| `docs/adr/ADR-006-risk-limits-and-circuit-breaker.md` | ADR for all Phase 3 risk thresholds and circuit breaker rationale |
| `docs/calibrations.md` | Updated with 5 new Phase 3 entries (position size, daily loss limit, concurrency limit, circuit breaker threshold, circuit breaker window) |

### Branches created

| Branch | Status |
|---|---|
| `feature/f3-sqlite-wal-models` | Committed. Ready for review before merge to develop. |
| `feature/f3-risk-capital-limits` | Committed. Ready for review before merge to develop. |
| `feature/f3-executor-parallel-dryrun` | Committed. Ready for review before merge to develop. |

---

## Test suite

**176 / 176 tests passing** across 11 test modules:

| Module | Tests |
|---|---|
| `test_base.py` | 18 |
| `test_binance_adapter_create.py` | 5 |
| `test_binance_ws_and_weight.py` | 11 |
| `test_database.py` | 5 |
| `test_evaluator.py` | 11 |
| `test_fees_bnb_discount.py` | 10 |
| `test_graph.py` | 45 |
| `test_reconciliation.py` | 9 |
| `test_risk.py` | 10 |
| `test_settings.py` | 40 |
| `test_symbol_helpers.py` | 12 |

Reconciliation tests probe loss arithmetic explicitly:
- Leg 1 failure: `-100 × 0.005 = -0.500 USDT` → `actual_net_return = 0.995` (asserted exactly as `Decimal("0.995")`).
- Leg 2 failure: `-100 × 0.01 = -1.000 USDT` → `actual_net_return = 0.990` (asserted exactly as `Decimal("0.990")`).

---

## What was decided (with ADR references)

### SQLite WAL mode (ADR-005)
- `journal_mode=WAL` eliminates reader/writer lock contention in async context.
- `synchronous=NORMAL` provides adequate durability with reduced disk I/O overhead.
- `busy_timeout=5000ms` prevents `OperationalError` under concurrent write pressure.
- Negative: produces `.db-wal` and `.db-shm` auxiliary files — must be volume-mounted together in Phase 5 Docker deployment.

### Risk thresholds (ADR-006)
All Phase 3 thresholds are **initial heuristic starting points**, not empirically calibrated values:

| Parameter | Value | Calibration status |
|---|---|---|
| `MAX_POSITION_USDT` | 100 USDT | Heuristic — pending fill-rate data from dry-run |
| `DAILY_LOSS_LIMIT_USDT` | -50 USDT | Heuristic (50% of MAX_POSITION_USDT) — pending PnL distribution data |
| `MAX_CONCURRENT_TRIANGLES` | 2 | Conservative baseline — pending simultaneous opportunity frequency data |
| `CIRCUIT_BREAKER_INCIDENT_COUNT` | 3 | Heuristic threshold for "structural failure" vs. "statistical noise" |
| `CIRCUIT_BREAKER_WINDOW_MINUTES` | 60 | Initial estimate — pending incident clustering analysis |

---

## New threshold justification conventions (Phase 3+)

Per `docs/planning/plan_doc_DQ.md` §1 and §9.2 principles:
- No threshold in this phase was calibrated by looking at what would have made the dry-run "look good" — all parameters were set *before* any live observation.
- Circuit breaker parameters represent a genuine calibration judgment call (3 incidents / 60 minutes) — the ADR-006 decision record documents exactly why these numbers were chosen and what evidence would warrant revising them.

---

## ⚠️ DRY_RUN Validation Status — NOT COMPLETED

> **Tests passing ≠ Dry-run validated. These are not the same claim.**

Phase 3 dry-run validation is a **real elapsed-time requirement** per the Technical Plan §8 and the roadmap (§5, Fase 3):

> *"Correr en DRY_RUN varios días, revisar métricas de fill rate teórico y PnL neto de comisiones antes de avanzar."*

**What must happen before Phase 3 is considered validated:**
1. All Phase 3 branches must be merged to `develop`.
2. The bot must run with `DRY_RUN=True` for a minimum of several consecutive days.
3. The following must be collected and reviewed:
   - Frequency of profitability signals (triangle evaluation → `is_profitable=True`).
   - Theoretical fill rate (simulated FOK completion rate).
   - Simulated net PnL after commission deduction across all evaluated opportunities.
   - Incident rate (how often reconciliation events would have been triggered).
4. Based on these observations, the following calibrations must be revisited before Phase 4:
   - `MAX_TICK_AGE_MS` (empirical P95/P99 latency data — deferred from Phase 2).
   - `SAFETY_MARGIN` (actual slippage distribution vs. the 10 bps buffer assumption).
   - All Phase 3 risk thresholds (see table above).

**`develop` will not be tagged `v0.3.0-fase3-dryrun` and will not merge to `main` until the DRY_RUN observation period is complete and documented.**

The specific dry-run observation plan, given the current local/dev environment (no VPS yet — Phase 5), will be agreed with the operator before execution starts.

---

## What was explicitly deferred to Phase 4+

- `interfaces/telegram_bot.py`: `/status`, `/kill`, `/resume`, `/pnl` commands.
- Redis control-plane (`storage/redis_client.py`): `TRADING_ENABLED` kill switch.
- Telegram incident alerts.
- `main.py` event loop integration wiring all Phase 3 modules together.
- UTC midnight reset for `DAILY_LOSS_LIMIT_USDT`: `RiskManager.reset_daily_pnl()` exists but nothing calls it automatically yet (no scheduler/cron wiring). See ADR-006 trade-offs section.
