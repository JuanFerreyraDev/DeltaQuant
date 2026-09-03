# ADR-008: Redis TRADING_ENABLED as Pause-State Source of Truth

## Status
Accepted

## Context

Phase 4 introduces two pause-control surfaces:
1. In-process risk state (`RiskManager.is_paused`, `pause_reason`), reset on process restart.
2. Redis control-plane state (`TRADING_ENABLED`), durable across restarts.

If these diverge, a systemd restart can silently re-enable trading after an operator-issued kill.
That failure mode is unacceptable because `/kill` is a safety mechanism for future live capital operation.

## Decision

Use Redis `TRADING_ENABLED` as the **durable source of truth** for desired trading state.
`RiskManager` remains the in-process execution gate and is treated as a runtime mirror that must be synchronized from Redis.

Synchronization rules:
- `TRADING_ENABLED=False`: orchestrator pauses `RiskManager` with a control-plane pause reason.
- `TRADING_ENABLED=True`: orchestrator resumes only if pause was control-plane-originated, or if an explicit operator `/resume` force-resume command is issued.
- If `RiskManager` is paused by internal risk logic (daily-loss or circuit-breaker) while Redis still says enabled, orchestrator writes `TRADING_ENABLED=False` back to Redis with the risk pause reason.

Operator command flow:
- `/kill`: write Redis disabled state first, then apply immediate in-process pause.
- `/resume`: write Redis enabled state first, then apply immediate in-process resume.

This preserves both durability and immediate behavior change without querying Redis in the hot tick path.

## Consequences

Positive:
- Restart-safe kill switch behavior: a process restart cannot silently resume trading when Redis says disabled.
- Single durable state for external control and future multi-process control-plane tooling.
- In-process risk checks remain fast (`RiskManager.can_execute`) with no per-tick Redis IO.

Negative / Trade-offs:
- Redis polling introduces bounded control latency for out-of-process state changes (up to poll interval).
- Two-state reconciliation logic is required to keep Redis and `RiskManager` consistent.

## Testing

- Integration test simulating kill -> process restart -> startup sync -> execution remains blocked.
- Integration test proving kill changes execution behavior, not only flags.
- Integration test ensuring `/resume` clears both `is_paused` and `pause_reason`.
- Integration test confirming Redis is not queried in `_process_tick`.

## References

- `main.py`: `Orchestrator._periodic_control_plane_loop`, `_sync_control_plane_once`, `_apply_trading_enabled_state`.
- `storage/redis_client.py`: durable control-plane key wrapper.
- `core/risk.py`: existing pause/resume state machine (reused, not reimplemented).
- `docs/fases/fase3.md`: deferred scope requiring Redis kill switch and restart-safe pause control.
