# Phase 5 Stage 1: Binance TESTNET Real-Order Plumbing

## Status
Implemented and reviewed locally. No Binance TESTNET orders have been placed yet; operator approval is still required before any live testnet execution.

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

## Testnet plan before the first real order

The first smoke tests will be tiny, one-at-a-time testnet orders only, with explicit approval before each execution:

1. FOK full-fill smoke test on a highly liquid USDT pair such as `BTC/USDT`, using a quantity and price that are comfortably inside the current filters.
2. FOK zero-fill smoke test on the same pair by placing the limit price beyond the current book so the order should expire with zero fill.
3. Reconciliation smoke test on a triangle path where the second FOK leg expires after the first leg fills, so the emergency market liquidation path is exercised.

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
