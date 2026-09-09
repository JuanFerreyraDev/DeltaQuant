# Phase 5 Stage 1: Binance TESTNET Real-Order Plumbing

## Status
Scenarios 1 and 2 completed. Scenario 3 on hold pending resolution and empirical validation of the parallel-dispatch balance-dependency investigation.

## What was built

- Added `BINANCE_TESTNET` to application settings.
- Added separate Binance TESTNET credentials to `Settings` and `.env.example`.
- Extended `ExchangeAdapter` with an explicit `place_market_order` contract.
- Implemented Binance TESTNET support in `BinanceAdapter`.
- Implemented real FOK placement via ccxt with `timeInForce=FOK`.
- Implemented market-order placement for emergency reconciliation.
- Added local validation against market precision and notional filters before order submission.
- Wired the executor live path to build real order requests from the chosen path and the current ticker snapshot.
- Wired live reconciliation to use a market order after a failed FOK leg.
- Added an ADR covering the testnet-only safety gate and market-order reconciliation design.

## Validation completed locally

- Settings validation now rejects missing Binance TESTNET credentials when `BINANCE_TESTNET=True`.
- Adapter tests cover the testnet gate, FOK precision handling, and market-order notional validation.
- Executor tests cover live-path parallel dispatch and market-order reconciliation.

## Test isolation note (added after live smoke tests)

- A latent unit-test fragility was discovered while fixing Scenario 2 mapping: two adapter tests unintentionally absorbed `BINANCE_TESTNET=True` from the developer's local `.env`, so their pass/fail result depended on machine-local environment state.
- Stage rule reaffirmed: settings/adapter tests must always construct `Settings(...)` explicitly with all relevant fields for the scenario under test, and must never rely on `.env` state from the developer workstation.
- The affected tests were corrected to isolate runtime flags from external environment state.

Follow-up item before closing this stage:
- Run a focused audit across the rest of the suite for the same anti-pattern (tests implicitly depending on external env vars) and convert any remaining cases to explicit, self-contained settings fixtures.

## Parallel dispatch investigation (blocking Scenario 3)

- An architectural question was raised regarding whether parallel dispatch of sequentially-dependent legs via `asyncio.gather` is safe when intermediate assets are not pre-funded.
- ADR-008 enumerates the 8 possible fill combinations, but the behavior of Binance when dependent orders are submitted concurrently requires empirical validation before Scenario 3 proceeds.

## Testnet plan before the first real order

The first smoke tests will be tiny, one-at-a-time testnet orders only, with explicit approval before each execution:

1. FOK full-fill smoke test on a highly liquid USDT pair such as `BTC/USDT`, using a quantity and price that are comfortably inside the current filters.
2. FOK zero-fill smoke test on the same pair by placing the limit price beyond the current book so the order should expire with zero fill.
3. **[NOW APPROVED]** Reconciliation smoke test on a triangle path where the second FOK leg expires after the first leg fills, so the emergency market liquidation path is exercised.

Expected observations:
- Full fill: `OrderResult.status == FILLED`, `OrderResult.is_filled == True`, and the returned fill quantity matches the submitted valid amount.
- FOK expiration: `OrderResult.status == EXPIRED`, `filled_qty == 0`, and `is_filled == False`.
- Market liquidation: `place_market_order` returns an `OrderResult` with a real fill status and the executor records the liquidation result instead of a simulated slippage estimate.

## What is explicitly not covered here

- VPS deployment.
- Dockerization.
- Any transition to real capital.
- Production order placement.

Those are separate future stages and will be scoped independently.
