# Calibration Registry: DeltaQuant Parameters

This document records the exact threshold values, calibration methodologies, and commit hashes for all numerical parameters governing the evaluation, risk, and execution models of DeltaQuant.

Per `docs/planning/plan_doc_DQ.md` §4: every parameter that affects profitability or safety must be documented here when set or modified.

---

## Active Calibrations Table

| Date | Parameter | Value | Calibration Method / Justification | Commit |
|---|---|---|---|---|
| 2026-08-24 | `MAX_TICK_AGE_MS` | `200` (ms) | Initial heuristic upper-bound estimate derived from order book stability window (~250-300ms) minus estimated REST execution latency (~50-100ms), yielding a ~150-200ms range. Phase 3 did not persist per-tick latency values, so it remains heuristic and is not empirically confirmed. See ADR-004 and ADR-007. | `feature/f2-evaluator-staleness-check` |
| 2026-08-24 | `SAFETY_MARGIN` | `0.0010` (0.10 %) | Heuristic buffer over nominal 1.0 net return multiplier for estimated top-of-book slippage and execution latency jitter, added on top of BNB-discounted taker fees (0.075 % per leg). Phase 3 did not persist near-margin net returns, so it remains heuristic and is not empirically confirmed. See ADR-007. | `feature/f2-evaluator-staleness-check` |
| 2026-08-24 | `MAX_POSITION_USDT` | `100` (USDT) | Phase 3 conservative starting cap. Consistent with top-of-book depth on Tier 1 USDT pairs. Pending upward recalibration from dry-run fill-rate data. See ADR-006. | `feature/f3-risk-capital-limits` |
| 2026-08-24 | `DAILY_LOSS_LIMIT_USDT` | `-50` (USDT) | Set to 50% of MAX_POSITION_USDT. Represents estimated 5-10 worst-case reconciliation incident losses. Not empirically calibrated — requires Phase 3 dry-run PnL distribution data. See ADR-006. | `feature/f3-risk-capital-limits` |
| 2026-08-24 | `MAX_CONCURRENT_TRIANGLES` | `2` | Phase 3 conservative baseline to prevent overexposure during simultaneous opportunities. Recalibrate from dry-run data. See ADR-006. | `feature/f3-risk-capital-limits` |
| 2026-08-24 | `CIRCUIT_BREAKER_INCIDENT_COUNT` | `3` | 3 reconciliation incidents in 60 minutes = structural failure signal, not statistical noise. See ADR-006. | `feature/f3-risk-capital-limits` |
| 2026-08-24 | `CIRCUIT_BREAKER_WINDOW_MINUTES` | `60` | Phase 3 initial estimate. Recalibrate if incident clustering patterns emerge during dry-run. See ADR-006. | `feature/f3-risk-capital-limits` |

---

## Calibration Details

### 1. `MAX_TICK_AGE_MS = 200`

- **Purpose**: Maximum permissible age (in milliseconds) for a `BookTicker` update before discarding a triangle as stale.
- **Locus of control**: `config/settings.py` (`MAX_TICK_AGE_MS`), enforced in `core/evaluator.py` (`evaluate_path`).
- **Rationale**: Initial heuristic starting point for Phase 2 before live empirical latency metrics are collected. Derived from top-of-book order book stability window (~250-300ms) minus estimated REST execution latency (~50-100ms), yielding a ~150-200ms window; 200ms is selected as a conservative upper bound. Phase 3 did not persist per-tick latency values, so this value remains heuristic and is not empirically confirmed; see ADR-007.
- **ADR Reference**: `docs/adr/ADR-004-staleness-threshold-criterion.md`.

### 2. `SAFETY_MARGIN = 0.0010` (0.10 %)

- **Purpose**: Required minimum net return multiplier above `1.0` (i.e. `net_return > Decimal("1.0010")`) for a triangle to be flagged `is_profitable = True`.
- **Locus of control**: `config/settings.py` (`SAFETY_MARGIN`), enforced in `core/evaluator.py` (`evaluate_path`).
- **Rationale**:
  - Combined BNB-discounted taker fees across 3 legs: $\approx 3 \times 0.075\% = 0.225\%$.
  - Nominal 1.0 net return calculation already subtracts all three leg fees.
  - The additional 0.10% (10 bps) `SAFETY_MARGIN` ensures that small top-of-book quote shifts or minimal slippage during FOK order placement do not turn a mathematically profitable opportunity into a net loss.
  - Phase 3 did not persist near-margin `net_return` values, so this value remains heuristic and is not empirically confirmed; see ADR-007.
- **Plan Reference**: Technical Plan §3 step 4 and §9.2.

### 3. `MAX_POSITION_USDT = 100` (USDT)

- **Purpose**: Hard cap on capital allocated to a single triangle execution attempt (Leg 0 entry position).
- **Locus of control**: `config/settings.py` (`MAX_POSITION_USDT`), enforced in `core/risk.py` (`can_execute`).
- **Rationale**: Phase 3 starting conservative upper bound. Triangular arbitrage on Binance operates in market-order / FOK regime. At ≥$100 notional, top-of-book depth across USDT-quoted pairs (typically ≥$10k depth at Tier 1 pairs) is sufficient to expect FOK fills without exhausting visible liquidity. Upward recalibration requires live fill-rate data collected during Phase 3 dry-run; see §9.4 of the Technical Plan for depth-based sizing rationale.
- **ADR Reference**: `docs/adr/ADR-006-risk-limits-and-circuit-breaker.md`.

### 4. `DAILY_LOSS_LIMIT_USDT = -50` (USDT)

- **Purpose**: Cumulative daily PnL floor; triggers auto-pause when realized losses reach or exceed this value.
- **Locus of control**: `config/settings.py` (`DAILY_LOSS_LIMIT_USDT`), enforced in `core/risk.py` (`register_execution_end`).
- **Rationale**: Set to 50% of `MAX_POSITION_USDT` to limit runaway losses during adverse market regimes (e.g. persistent FOK rejections with emergency liquidation). Represents an estimated 5-10 consecutive worst-case reconciliation incidents. Not calibrated against live data; must be revised after Phase 3 dry-run PnL distribution analysis.
- **ADR Reference**: `docs/adr/ADR-006-risk-limits-and-circuit-breaker.md`.

### 5. `MAX_CONCURRENT_TRIANGLES = 2`

- **Purpose**: Maximum number of triangle executions that may be simultaneously in-flight.
- **Locus of control**: `config/settings.py` (`MAX_CONCURRENT_TRIANGLES`), enforced in `core/risk.py` (`can_execute`).
- **Rationale**: Phase 3 conservative baseline to prevent portfolio overexposure when multiple opportunities appear simultaneously during high-volatility market spikes. Set to 2 to allow parallel execution without multiplying inventory risk beyond manageable bounds. Recalibrate upward if dry-run fill rates show consistent simultaneous profitability.
- **ADR Reference**: `docs/adr/ADR-006-risk-limits-and-circuit-breaker.md`.

### 6. `CIRCUIT_BREAKER_INCIDENT_COUNT = 3` in `CIRCUIT_BREAKER_WINDOW_MINUTES = 60`

- **Purpose**: Emergency reconciliation circuit breaker — auto-pauses all trading when N partial-fill incidents occur within W minutes.
- **Locus of control**: `config/settings.py`, enforced in `core/risk.py` (`record_incident`).
- **Rationale**: 1 reconciliation incident (FOK expiration after Leg 0 or 1 fills) is a statistically expected edge case. 3 incidents within 60 minutes signals a structural failure mode (sustained network degradation, rate-limit ban, systematic order-book illiquidity at the configured pair set). The 60-minute window is a Phase 3 initial estimate; if Phase 3 dry-run data shows incident clustering patterns, these parameters must be recalibrated. See ADR-006 for the full threshold justification.
- **ADR Reference**: `docs/adr/ADR-006-risk-limits-and-circuit-breaker.md`.
- **Runbook Reference**: `docs/runbooks/reconciliation.md`.

