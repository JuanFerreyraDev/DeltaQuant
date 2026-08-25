# ADR-006: Risk Limits and Emergency Reconciliation Circuit Breaker

## Status
Accepted

## Context

Triangular arbitrage involves executing three sequential or parallel FOK limit orders across distinct trading pairs. While nominal mathematical spreads may appear profitable during evaluation, execution in live crypto markets carries operational risks:
1. **Partial leg failures**: If Leg 1 or Leg 2 fills but a subsequent leg fails or expires unfilled, the bot is left holding unhedged base/quote inventory subject to market volatility.
2. **Consecutive infrastructure failures**: Persistent API rate-limit bans, WebSocket connection dropouts, high engine queue latency, or sudden exchange order-book illiquidity can cause repeated partial-fill failures in quick succession.
3. **Cumulative drawdown**: Compounding small slippage or liquidation losses can erode trading capital if trading continues during adverse market regimes.

To protect trading capital, DeltaQuant requires a deterministic risk management module (`core/risk.py`) operating strictly before order dispatch.

## Decision

Implement `RiskManager` in `core/risk.py` enforcing four explicit, configurable risk thresholds:

1. **Max Position Size Cap (`MAX_POSITION_USDT = 100`)**: Hard upper bound on USDT-equivalent notional allocated to any single triangle execution attempt.
2. **Daily Loss Limit Floor (`DAILY_LOSS_LIMIT_USDT = -50`)**: Cumulative daily PnL floor. If cumulative net PnL drops to or below -$50.00 USDT, the risk engine self-pauses all execution.
3. **Max Concurrent Triangles (`MAX_CONCURRENT_TRIANGLES = 2`)**: Upper bound on simultaneously in-flight triangle executions to prevent portfolio overexposure during market volatility spikes.
4. **Reconciliation Circuit Breaker (`CIRCUIT_BREAKER_INCIDENT_COUNT = 3` in `CIRCUIT_BREAKER_WINDOW_MINUTES = 60`)**: If 3 emergency inventory liquidations (reconciliation incidents) occur within any rolling 60-minute window, `RiskManager` auto-pauses all execution immediately.

### Circuit Breaker Threshold Justification

Emergency liquidations occur when a multi-leg trade fails partially and requires market orders to dump unhedged inventory. A single incident is an expected statistical edge-case (e.g. FOK expiration due to top-of-book movement). However, **3 incidents within 60 minutes** signifies a systemic operational failure — such as Binance matching engine latency spikes, local network packet loss, or extreme order-book quote instability.

Pausing trading automatically stops further capital degradation and requires explicit operator intervention (`/resume` in Telegram after manual investigation).

## Implementation

- Defined in `config/settings.py` (`MAX_POSITION_USDT`, `DAILY_LOSS_LIMIT_USDT`, `MAX_CONCURRENT_TRIANGLES`, `CIRCUIT_BREAKER_INCIDENT_COUNT`, `CIRCUIT_BREAKER_WINDOW_MINUTES`).
- Enforced in `core/risk.py` via `can_execute()`, `register_execution_start()`, `register_execution_end()`, and `record_incident()`.
- Documented in `docs/calibrations.md`.
- Verified in `tests/test_risk.py`.

## Consequences

**Positive:**
- Eliminates runaway capital loss during exchange API degradation or network instability.
- Prevents portfolio overexposure through hard position caps and concurrency limits.
- Provides immediate auditability and deterministic pause states.

**Negative / Trade-offs:**
- False-positive circuit breaker trips during high-volatility regimes (where spreads are highest) will temporarily pause trading until manual operator review.
- Requires UTC midnight reset logic for `DAILY_LOSS_LIMIT_USDT`.

## References
- **Technical Plan §5, §7**: Risk management and circuit breaker requirements.
- **`config/settings.py`**: Risk configuration parameters.
