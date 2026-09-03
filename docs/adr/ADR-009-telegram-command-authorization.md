# ADR-009: Telegram Command Authorization Scope

## Status
Accepted

## Context

Phase 4 introduces privileged commands (`/kill`, `/resume`) that directly control whether execution is allowed.
If any chat user can invoke these commands, a leaked bot username or accidental group add can disrupt trading operations.

Phase 4 does not include a full RBAC control-plane. The project currently has one operator channel configured via `TELEGRAM_CHAT_ID`.

## Decision

Only the configured `TELEGRAM_CHAT_ID` is authorized to execute control-plane commands.

Operational availability policy:
- If `TelegramControlPlane.start()` fails at startup (bad token, API/network error), orchestrator fails fast and exits.
- DeltaQuant does not continue trading without an active Telegram control-plane during this phase.

Authorization behavior:
- If `update.effective_chat.id` does not match configured chat id, command is rejected.
- Rejected commands produce an explicit `Unauthorized chat id.` reply and warning log entry.
- Authorized commands still enforce strict argument validation:
  - `/kill`, `/resume`, `/status`, `/pnl` reject extra arguments.

This minimizes attack surface and avoids silent command no-ops from malformed inputs.

## Consequences

Positive:
- Safety-critical commands are restricted to one explicit control channel.
- Malformed and unauthorized command attempts are visible in logs.
- Behavior is deterministic and testable.
- Startup failures are loud and immediate instead of silently degrading operator control.

Negative / Trade-offs:
- No multi-operator support in Phase 4.
- Group-chat operations require explicit `TELEGRAM_CHAT_ID` configuration to match target chat.

## Testing

- Unauthorized chat cannot trigger `/kill` callback.
- `/kill` with unexpected args is rejected with usage response.
- `/kill` and `/resume` invoke control callbacks when authorized.
- `/status` and `/pnl` return expected payloads.
- Telegram startup failure raises a fatal orchestrator error (fail-fast test).

## References

- `interfaces/telegram_bot.py`: authorization and command parsing.
- `tests/test_telegram_bot.py`: unit tests for authorization and malformed input handling.
