# Runbook: Inventory Reconciliation & Emergency Liquidation

## 1. Overview and Purpose

In triangular arbitrage, orders across three distinct market legs are submitted to complete a closed loop (e.g., `USDT -> BTC -> ETH -> USDT`). Under ideal conditions, all three orders fill completely via Fill-or-Kill (FOK) execution.

However, if Leg 0 fills but Leg 1 or Leg 2 fails (e.g. due to top-of-book price movement, network latency jitter, or exchange matching engine timeouts), the bot is left holding an **unhedged inventory position** in an intermediate base or quote currency (e.g., holding `BTC` or `ETH` instead of `USDT`).

This runbook documents:
1. The **automated reconciliation mechanism** built into `core/executor.py`.
2. The **operator investigation protocol** when an incident occurs or the `RiskManager` circuit breaker self-pauses trading.

> **Phase 3 scope note**: The monitoring path in this phase is **manual** — no Telegram bot or `/status`/`/resume` commands exist yet (those are Phase 4). Discovery of a paused bot and manual resume both happen through direct Python shell interaction or by reading the SQLite incident log, as described below.

---

## 2. Automated Reconciliation Mechanism

When a partial leg failure is detected in `core/executor.py`:

```text
[ Leg 0 (FOK) ] ── (FILLED) ──> [ Leg 1 (FOK) ] ── (EXPIRED / FAILED)
                                        │
                                        ▼ (Unhedged inventory detected)
                           [ Emergency Market Order ]
                                        │
                                        ▼ (Liquidated back to USDT)
                           [ Incident Persisted to SQLite ]
                                        │
                                        ▼
                           [ RiskManager.record_incident() ]
```

1. **Detection**: `Executor._reconcile_inventory()` identifies the filled legs and the specific unhedged asset. The `failed_symbol` and `liquidation_symbol` are taken from `pair_symbols` — the execution-order tuple passed from `evaluate_triangle`'s output — never from the canonical `Triangle.pair_ab / pair_bc / pair_ca` ordering.
2. **Emergency Liquidation**: In live mode, a market order is immediately dispatched to convert the unhedged asset back to USDT or the primary quote currency. In `DRY_RUN` mode, the liquidation slippage is calculated (-0.5% for Leg 1 failure, -1.0% for Leg 2 failure) and logged without placing any real order.
3. **Database Logging**: An entry is created in the SQLite `incidents` table recording:
   - `triangle_id`
   - `failed_leg_index` (1 or 2)
   - `failed_symbol` — the pair that failed (from execution-order `pair_symbols`)
   - `error_message`
   - `liquidation_symbol` — the pair used for emergency liquidation
   - `liquidation_amount`
   - `liquidation_pnl_usdt`
   - `timestamp_ms`
4. **Circuit Breaker**: `RiskManager.record_incident()` receives the incident timestamp. If **3 incidents occur within a rolling 60-minute window**, `RiskManager` automatically sets `is_paused = True` and logs `"Circuit breaker triggered"`. No automated alert is sent in Phase 3; the operator must discover this through the monitoring steps below.

---

## 3. Operator Incident Investigation Protocol (Phase 3 — Manual)

In Phase 3 there is no Telegram bot. To detect a paused bot or investigate incidents, use the methods below directly.

### Step 1: Detect pause state

Check the bot's log output for `risk_manager_paused` log lines:

```bash
grep -i "risk_manager_paused\|circuit_breaker\|executor_reconciliation" logs/deltaquant.log | tail -20
```

Or query the in-process `RiskManager` state in a Python shell (if the bot supports a control socket in Phase 4+):

```python
print(risk_manager.is_paused)
print(risk_manager.pause_reason)
```

### Step 2: Query SQLite Incident Log

Inspect recent incident records directly via SQLite CLI:

```bash
sqlite3 deltaquant.db \
  "SELECT id, triangle_id, failed_symbol, liquidation_symbol, liquidation_pnl_usdt, \
   datetime(timestamp_ms/1000, 'unixepoch') FROM incidents ORDER BY id DESC LIMIT 10;"
```

### Step 3: Verify Exchange Account Balances

Verify the automated liquidation successfully cleared all unhedged inventory. Check each asset involved in the failed triangle. Using `get_balance(asset: str)` for each relevant asset:

```python
# triangle assets involved in the failed execution:
# pair_symbols = ("BTCUSDT", "ETH/BTC", "ETHUSDT")
# intermediate assets that may be unhedged: BTC, ETH
for asset in ("BTC", "ETH", "USDT"):
    bal = await adapter.get_balance(asset)
    print(f"{asset}: free={bal.free} locked={bal.locked}")
```

If residual unhedged asset balance exists (e.g. the emergency market liquidation order partially failed):
1. Log into the Binance Web or mobile app immediately.
2. Market-sell the residual asset to USDT manually.
3. Record the manual intervention in the incident log (update the relevant SQLite row with a note).

### Step 4: Identify Root Cause

Examine application logs around the incident `timestamp_ms` (use `datetime(timestamp_ms/1000, 'unixepoch')` from the SQLite query above):

```bash
grep -i "executor_reconciliation_triggered" logs/deltaquant.log
```

Common causes:
- **High network latency**: If `execution_duration_ms` > 150ms, VPS-to-Binance RTT may have degraded.
- **Top-of-book depth exhaustion**: If FOK orders expire frequently, `MAX_POSITION_USDT` may be too large for current order book liquidity at the configured pairs.
- **Rate-limit bans**: Look for HTTP 429 / 418 responses from the Binance REST API in the log.

### Step 5: Resume Trading

Once the account balance is verified clean and the root cause addressed:

1. Fix the underlying issue (e.g., reduce `MAX_POSITION_USDT` in `.env`, wait for network recovery).
2. Resume trading by calling `risk_manager.resume()` directly in a Python shell:

```python
# In a Python REPL attached to the running process, or after restart:
risk_manager.resume()
print(risk_manager.is_paused)   # → False
print(risk_manager.pause_reason) # → None
```

> **Note**: Calling `risk_manager.resume()` clears the incident window history (`_incident_timestamps_ms`). This resets the circuit breaker counter to zero so that subsequent trades within the 60-minute window will not re-pause the bot without new incidents occurring.

> **Note**: In Phase 4, this resume step will be replaced by the Telegram `/resume` command. Until then, direct Python shell access is required.

---

## 4. References
- **Technical Plan §3 (Step 8), §5, §7**: Reconciliation and incident logging rules.
- **`core/executor.py`**: Implementation of `_reconcile_inventory()`.
- **`docs/adr/ADR-006-risk-limits-and-circuit-breaker.md`**: Circuit breaker threshold rationale.
- **`docs/calibrations.md`**: `CIRCUIT_BREAKER_INCIDENT_COUNT` and `CIRCUIT_BREAKER_WINDOW_MINUTES` entries.
