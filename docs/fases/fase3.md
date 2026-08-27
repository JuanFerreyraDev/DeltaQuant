# Phase 3: Simulation, Risk, and Persistence — Closeout

## Status
**TESTS PASSING / DRY-RUN VALIDATION READY** — see §6 for the explicit distinction and deployment instructions.

---

## What was implemented

### Modules delivered

| Module | Description |
|---|---|
| `storage/models.py` | SQLAlchemy ORM models: `Trade`, `Incident`, `Metric` |
| `storage/database.py` | `DatabaseManager`: async SQLite with WAL mode, session lifecycle, PRAGMA configuration |
| `core/risk.py` | `RiskManager`: position size cap, daily loss limit, concurrency limit, circuit breaker |
| `core/executor.py` | `Executor`: DRY_RUN parallel dispatch, inventory reconciliation, incident logging |
| `main.py` | Event loop integration: volume filtering, ticker streaming, evaluation & execution loops |
| `systemd/deltaquant.service` | User systemd unit file for supervised continuous deployment |
| `docs/runbooks/reconciliation.md` | Operator runbook: reconciliation mechanism, investigation protocol, resume steps |
| `docs/adr/ADR-005-sqlite-async-wal-mode.md` | ADR for SQLite WAL mode selection and trade-offs |
| `docs/adr/ADR-006-risk-limits-and-circuit-breaker.md` | ADR for all Phase 3 risk thresholds and circuit breaker rationale |
| `docs/calibrations.md` | Updated with Phase 3 entries (position size, daily loss limit, concurrency limit, circuit breaker) |

---

## Test suite

**181 / 181 tests passing** across 12 test modules:

| Module | Tests |
|---|---|
| `test_base.py` | 18 |
| `test_binance_adapter_create.py` | 5 |
| `test_binance_ws_and_weight.py` | 11 |
| `test_database.py` | 5 |
| `test_evaluator.py` | 11 |
| `test_fees_bnb_discount.py` | 10 |
| `test_graph.py` | 45 |
| `test_main_integration.py` | 5 |
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

## Systemd Supervised Deployment & Operation

### Unit file installation
To install and start the background user service on Linux Mint / Linux local netbook:

```bash
# 1. Copy unit file to user systemd directory
mkdir -p ~/.config/systemd/user/
cp systemd/deltaquant.service ~/.config/systemd/user/

# 2. Reload systemd manager and enable service
systemctl --user daemon-reload
systemctl --user enable deltaquant.service

# 3. Start service and check status
systemctl --user start deltaquant.service
systemctl --user status deltaquant.service
```

### Log monitoring
Logs are streamed to standard error (captured by `journalctl`) and persisted to `logs/deltaquant.log`:

```bash
# View live journal logs
journalctl --user -u deltaquant.service -f

# Grep application log file per runbook instructions
tail -f logs/deltaquant.log
grep "profitable_signal_detected" logs/deltaquant.log
```

### Power Management Note (Linux Mint / Netbook)
Because the observation period requires 72 hours of uninterrupted execution:
- Ensure laptop lid closure action is set to **"Do Nothing"** or **"Turn off display"** (not Suspend/Sleep).
- Disable automatic system suspend on AC power in Linux Mint Power Management settings (`cinnamon-settings power`).

---

## Operational Progress Queries (Observation Period Telemetry)

To inspect total evaluations, profitable signal count, and risk status during the 72-hour `DRY_RUN` observation period without interrupting the running process, run:

```bash
# Total triangle evaluations count so far
sqlite3 deltaquant.db "SELECT metric_value AS evaluations_count, datetime(timestamp_ms/1000, 'unixepoch', 'localtime') AS timestamp FROM metrics WHERE metric_name = 'evaluations_count' ORDER BY id DESC LIMIT 1;"

# Full operational heartbeat and telemetry snapshot
sqlite3 deltaquant.db "SELECT metric_name, metric_value, datetime(timestamp_ms/1000, 'unixepoch', 'localtime') AS timestamp FROM metrics WHERE metric_name IN ('evaluations_count', 'profitable_signals_count', 'executions_count', 'risk_is_paused') ORDER BY id DESC LIMIT 4;"
```

---

## ⚠️ DRY_RUN Validation Status

Phase 3 dry-run validation is a **real elapsed-time requirement** per the Technical Plan §8 and the roadmap (§5, Fase 3):

> *"Correr en DRY_RUN varios días, revisar métricas de fill rate teórico y PnL neto de comisiones antes de avanzar."*

**Observation protocol (72-hour run):**
1. Ensure the systemd service is active (`systemctl --user status deltaquant`).
2. Run continuously for 72 hours.
3. Periodically review evaluation progress and logs.
4. Review collected telemetry data at hour 72 to calibrate `MAX_TICK_AGE_MS`, `SAFETY_MARGIN`, and risk limits before proceeding to Phase 4.

**`develop` will be tagged `v0.3.0-fase3-dryrun` upon completion of the 72h observation period.**

---

## What was explicitly deferred to Phase 4+

- `interfaces/telegram_bot.py`: `/status`, `/kill`, `/resume`, `/pnl` commands.
- Redis control-plane (`storage/redis_client.py`): `TRADING_ENABLED` kill switch.
- Telegram incident alerts.
- UTC midnight reset for `DAILY_LOSS_LIMIT_USDT`: `RiskManager.reset_daily_pnl()` exists but nothing calls it automatically yet (no scheduler/cron wiring). See ADR-006 trade-offs section.
