# ADR-007: Phase 3 Calibration Instrumentation Gap

## Status
Accepted

## Context

The Phase 3 dry-run observation ran for more than 72 hours and persisted aggregate evaluation, signal, execution, risk-state, and heartbeat metrics. It did not persist the per-tick `max_age_ms` values used by the staleness filter or near-margin `net_return` values needed to inspect the return distribution around `SAFETY_MARGIN`.

Consequently, the observation data cannot support empirical P95/P99 latency analysis or slippage/near-margin analysis. Aggregate counts must not be used to retrospectively estimate either distribution.

## Decision

Keep `MAX_TICK_AGE_MS = 200` ms and `SAFETY_MARGIN = 0.0010` (0.10%) unchanged and explicitly classified as heuristic, not empirically confirmed.

Do not tighten `MAX_TICK_AGE_MS` until a testnet/live rehearsal in Phase 5, or a short dedicated follow-up run, persists and reviews the real per-tick latency distribution. Tightening a staleness threshold without that distribution risks discarding genuinely fresh ticks.

This ADR records the limitation and recommendation only. It does not scope or implement the missing instrumentation.

## Consequences

**Positive:**
- Prevents unsupported threshold changes based on aggregate data.
- Preserves the existing slippage and latency buffer until it can be empirically evaluated.

**Negative / Trade-offs:**
- The current thresholds remain uncalibrated heuristics.
- A future rehearsal or dedicated run must close the telemetry gap before `MAX_TICK_AGE_MS` is tightened.

## References
- `docs/fases/fase3.md`: completed observation and calibration limitation.
- `docs/calibrations.md`: current heuristic calibration records.
- ADR-004: tick staleness threshold criterion.
