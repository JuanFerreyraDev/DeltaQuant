# Calibration Registry: DeltaQuant Parameters

This document records the exact threshold values, calibration methodologies, and commit hashes for all numerical parameters governing the evaluation, risk, and execution models of DeltaQuant.

Per `docs/planning/plan_doc_DQ.md` §4: every parameter that affects profitability or safety must be documented here when set or modified.

---

## Active Calibrations Table

| Date | Parameter | Value | Calibration Method / Justification | Commit |
|---|---|---|---|---|
| 2026-08-24 | `MAX_TICK_AGE_MS` | `200` (ms) | Initial heuristic upper-bound estimate derived from order book stability window (~250-300ms) minus estimated REST execution latency (~50-100ms), yielding a ~150-200ms range. 200ms selected as upper bound for Phase 2, explicitly pending recalibration against empirical P95/P99 latency data in Phase 3. See ADR-004. | `feature/f2-evaluator-staleness-check` |
| 2026-08-24 | `SAFETY_MARGIN` | `0.0010` (0.10 %) | Buffer over nominal 1.0 net return multiplier to cover estimated top-of-book slippage (0.025 %) and execution latency jitter, added on top of BNB-discounted taker fees (0.075 % per leg). | `feature/f2-evaluator-staleness-check` |

---

## Calibration Details

### 1. `MAX_TICK_AGE_MS = 200`

- **Purpose**: Maximum permissible age (in milliseconds) for a `BookTicker` update before discarding a triangle as stale.
- **Locus of control**: `config/settings.py` (`MAX_TICK_AGE_MS`), enforced in `core/evaluator.py` (`evaluate_path`).
- **Rationale**: Initial heuristic starting point for Phase 2 before live empirical latency metrics are collected. Derived from top-of-book order book stability window (~250-300ms) minus estimated REST execution latency (~50-100ms), yielding a ~150-200ms window; 200ms is selected as a conservative upper bound. Explicitly subject to recalibration against empirical P95/P99 latency distributions collected during Phase 3 dry-run monitoring.
- **ADR Reference**: `docs/adr/ADR-004-staleness-threshold-criterion.md`.

### 2. `SAFETY_MARGIN = 0.0010` (0.10 %)

- **Purpose**: Required minimum net return multiplier above `1.0` (i.e. `net_return > Decimal("1.0010")`) for a triangle to be flagged `is_profitable = True`.
- **Locus of control**: `config/settings.py` (`SAFETY_MARGIN`), enforced in `core/evaluator.py` (`evaluate_path`).
- **Rationale**:
  - Combined BNB-discounted taker fees across 3 legs: $\approx 3 \times 0.075\% = 0.225\%$.
  - Nominal 1.0 net return calculation already subtracts all three leg fees.
  - The additional 0.10% (10 bps) `SAFETY_MARGIN` ensures that small top-of-book quote shifts or minimal slippage during FOK order placement do not turn a mathematically profitable opportunity into a net loss.
- **Plan Reference**: Technical Plan §3 step 4 and §9.2.
