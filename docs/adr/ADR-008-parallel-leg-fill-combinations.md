# ADR-008: Handling All Parallel Leg Fill Combinations in execute_triangle

**Status:** SUPERSEDED BY ADR-009  
*(Superseded by ADR-009: Live parallel dispatch was empirically proven on Binance testnet to induce non-deterministic InsufficientFunds (-2010) race conditions across capital-dependent spot legs; live execution adopted sequential dispatch, making the 8-case parallel matrix obsolete for live trading.)*  
**Date:** 2026-09-08  
**Context:** Phase 5 Stage 1 — Live Binance testnet execution with parallel 3-leg FOK dispatch via `asyncio.gather`

## Problem Statement

The `_execute_live()` method in `core/executor.py` dispatches all 3 FOK order legs in parallel via `asyncio.gather`, but the result-determination logic below it assumes a sequential failure model inherited from Phase 3 DRY_RUN simulation.

**Sequential Assumption (Incorrect for Parallel):**
- If leg 0 fails, assume legs 1 and 2 were never attempted.
- If leg 1 fails, assume only leg 0 filled.
- If leg 2 fails, assume legs 0 and 1 filled.

**Reality of Parallel Execution:**
In true parallel dispatch, the three real orders are sent to the exchange simultaneously. Each order's outcome (filled/expired) is independent, subject only to:
1. Exchange-level inventory constraints (if legs draw from the same portfolio)
2. Execution timing (all sent within microseconds; results come back asynchronously)

**Gap:** The current code returns `FAILED_UNHANDLED` for logically "impossible" combinations (e.g., leg 0 failed but leg 2 filled) without ever attempting reconciliation. This leaves real, unhedged positions unaccounted for.

**Example:** Leg 0 fills (Asset A acquired), leg 1 expires (cannot proceed), but leg 2's FOK somehow completes anyway (Asset B exists, order succeeds). Current code logs this as an error and abandons leg 0's Asset A. Correct behavior: reconcile leg 0's Asset A via leg 0's pair.

## Case Analysis

Given 3 legs (0, 1, 2), each with binary outcome (filled=T, unfilled=F), there are **2³ = 8 cases**:

| Case | Leg 0 | Leg 1 | Leg 2 | Legs Filled | Inventory Held | Action |
|------|-------|-------|-------|-------------|---|---|
| 1 | F | F | F | 0 | None | Return `FAILED_LEG_0` (abort, zero unhedged) |
| 2 | F | F | T | 1 | Asset B (logically impossible; leg 2 requires leg 1 input) | Error: return `FAILED_UNHANDLED` with detailed note |
| 3 | F | T | F | 1 | Asset A (logically impossible; leg 1 requires leg 0 input) | Error: return `FAILED_UNHANDLED` with detailed note |
| 4 | F | T | T | 2 | Assets A and B (logically impossible; both require leg 0 input) | Error: return `FAILED_UNHANDLED` with detailed note |
| 5 | T | F | F | 1 | Asset A (from leg 0) | Reconcile: liquidate Asset A via `pair_symbols[0]` (equivalent to `_reconcile_inventory(failed_leg_index=1)`) |
| 6 | T | F | T | 2 | Asset A (from leg 0) + Asset B (from leg 2, logically orphaned) | Reconcile: liquidate Asset A via `pair_symbols[0]` (same as case 5; leg 2's Asset B is abandoned as unreachable) |
| 7 | T | T | F | 2 | Asset B (from leg 1) | Reconcile: liquidate Asset B via `pair_symbols[1]` (existing `_reconcile_inventory(failed_leg_index=2)`) |
| 8 | T | T | T | 3 | None (all legs completed the cycle) | Return `COMPLETED` |

### Key Observations

1. **Cases 1, 5, 7, 8 are already handled** by current code (sequentially, but correctly).

2. **Case 6 (T, F, T) is handled** by the existing sequential logic (test added for regression prevention).

3. **Cases 2, 3, 4 (UNVERIFIED HYPOTHESIS):** It was hypothesized that these cases cannot occur due to balance dependencies. However, whether downstream orders in parallel dispatch are rejected or can fill/fail in race conditions is unverified and requires empirical validation on Binance testnet.

4. **Reconciliation behavior:** If any downstream leg fills unexpectedly, the current code returns FAILED_UNHANDLED without reconciling that position. Whether that state is reachable remains under empirical investigation.

## Proposed Fix

### 1. Explicit Enumeration in Result-Determination Logic

Replace the current sequential if-chain with a switch on the fill pattern:

```python
# In _execute_live, after asyncio.gather completes:
leg0_filled = order_results[0].is_filled
leg1_filled = order_results[1].is_filled
leg2_filled = order_results[2].is_filled

if leg0_filled and leg1_filled and leg2_filled:
    # Case 8: All filled
    return ExecutionResult(..., status="COMPLETED", legs_filled=3, ...)

elif not leg0_filled and not leg1_filled and not leg2_filled:
    # Case 1: None filled
    return ExecutionResult(..., status="FAILED_LEG_0", legs_filled=0, ...)

elif leg0_filled and not leg1_filled:
    # Cases 5, 6: Leg 0 filled but leg 1 failed (regardless of leg 2 outcome)
    # Reconcile leg 0's output; leg 2's state is irrelevant
    return await self._reconcile_inventory(failed_leg_index=1, ...)

elif leg0_filled and leg1_filled and not leg2_filled:
    # Case 7: Legs 0 and 1 filled, leg 2 failed
    return await self._reconcile_inventory(failed_leg_index=2, ...)

elif (not leg0_filled) and (leg1_filled or leg2_filled):
    # Cases 2, 3, 4: Leg 0 failed but leg 1 or leg 2 filled (logically impossible)
    # This represents a state error: either exchange bug or portfolio constraint violation
    return ExecutionResult(
        ...,
        status="FAILED_UNHANDLED",
        error_message=f"Logical inconsistency: Leg 0 failed ({pair_symbols[0]}) but downstream legs filled. "
                      f"Pattern: leg0={leg0_filled} leg1={leg1_filled} leg2={leg2_filled}. "
                      f"This indicates either an exchange error or unexpected portfolio constraint violation.",
        legs_filled=sum([leg0_filled, leg1_filled, leg2_filled]),
    )

else:
    # Unhandled state (should be unreachable)
    return ExecutionResult(..., status="FAILED_UNHANDLED", error_message="Unreachable case", ...)
```

### 2. Unit Test Cases

Add tests to `tests/test_executor.py` or new `tests/test_executor_parallel_fills.py`:

- `test_execute_triangle_all_legs_filled()` — Case 8 (already implicitly tested)
- `test_execute_triangle_no_legs_filled()` — Case 1 (already implicitly tested)
- `test_execute_triangle_leg0_filled_leg1_failed()` — Cases 5, 6
- `test_execute_triangle_legs0_and_1_filled_leg2_failed()` — Case 7 (already implicitly tested)
- `test_execute_triangle_logical_impossibility_leg0_failed_leg1_filled()` — Case 3
- `test_execute_triangle_logical_impossibility_leg0_failed_leg2_filled()` — Case 2
- `test_execute_triangle_logical_impossibility_leg0_failed_legs1_and_2_filled()` — Case 4

## Impact

- **No change to correctness** for the seven cases already handled.
- **Clearer readability** via explicit case enumeration.
- **Improved observability** for edge cases via specific error messages.
- **New test coverage** for logical-impossibility cases (currently untested).
- **Set stage for Scenario 3** to exercise the real parallel execution path with confidence.
