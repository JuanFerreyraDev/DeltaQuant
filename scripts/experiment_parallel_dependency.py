#!/usr/bin/env python3
"""Empirical test of parallel dependent orders on Binance testnet.

Tests whether submitting two dependent FOK orders simultaneously via asyncio.gather
results in:
a) Order 1 filling and Order 2 filling (race / in-flight settlement)
b) Order 1 filling and Order 2 rejecting due to insufficient balance
c) Other exchange behavior (rejection, error code, etc.)

Ensures free BTC balance is strictly 0.0 before each run by locking pre-existing
BTC in an unfillable limit order, reproducing an account starting with 0 intermediate asset.
"""

import asyncio
from decimal import Decimal
import json
import traceback
from typing import Any

from config.settings import get_settings
from exchanges.binance_adapter import BinanceAdapter, _floor_to_step, _extract_market_rules, _native_to_unified


def json_safe(val: Any) -> Any:
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, dict):
        return {k: json_safe(v) for k, v in val.items()}
    if isinstance(val, list):
        return [json_safe(x) for x in val]
    return str(val)


async def run_single_iteration(adapter: BinanceAdapter, iteration: int) -> dict[str, Any]:
    print(f"\n{'='*40} ITERATION {iteration} {'='*40}")
    loop = asyncio.get_running_loop()
    
    # 1. Starting balances
    usdt_bal_init = await adapter.get_balance("USDT")
    btc_bal_init = await adapter.get_balance("BTC")
    eth_bal_init = await adapter.get_balance("ETH")
    print(f"Starting USDT: free={usdt_bal_init.free}, locked={usdt_bal_init.locked}")
    print(f"Starting BTC:  free={btc_bal_init.free}, locked={btc_bal_init.locked}")
    print(f"Starting ETH:  free={eth_bal_init.free}, locked={eth_bal_init.locked}")

    # 2. Lock all free BTC so free BTC == Decimal("0.0")
    # 2. Lock all free BTC so free BTC is below lot step size (dust < 0.00001 BTC)
    lock_order_id = None
    markets = await adapter.get_markets()
    btc_rules = _extract_market_rules(markets["BTC/USDT"])
    step = btc_rules["amount_step"] or Decimal("0.00001")

    if btc_bal_init.free >= step:
        btc_ticker = await loop.run_in_executor(None, lambda: adapter._client.fetch_ticker("BTC/USDT"))
        ask_price = float(btc_ticker["ask"])
        lock_price = round(ask_price * 1.3, 2)
        lock_qty = float(_floor_to_step(btc_bal_init.free, step))
        print(f"Locking {lock_qty} BTC with limit sell at {lock_price} (1.3x market ask)...")
        lock_order = await loop.run_in_executor(
            None,
            lambda: adapter._client.create_order(
                "BTC/USDT", "limit", "sell", lock_qty, lock_price
            ),
        )
        lock_order_id = lock_order["id"]
        btc_bal_locked = await adapter.get_balance("BTC")
        print(f"Isolated BTC balance: free={btc_bal_locked.free}, locked={btc_bal_locked.locked}")
        assert btc_bal_locked.free < step, f"Expected free BTC < {step}, got {btc_bal_locked.free}"
    else:
        print(f"Free BTC is already < {step}, no locking order needed.")

    try:
        # 3. Build theoretical 2-leg plan
        position_usdt = Decimal("15.0")  # > $5 min notional on BTCUSDT
        btc_tick = await loop.run_in_executor(None, lambda: adapter._client.fetch_ticker("BTC/USDT"))
        eth_tick = await loop.run_in_executor(None, lambda: adapter._client.fetch_ticker("ETH/BTC"))
        btc_ask = Decimal(str(btc_tick["ask"]))
        eth_ask = Decimal(str(eth_tick["ask"]))
        print(f"Market snapshot: BTCUSDT ask={btc_ask}, ETH/BTC ask={eth_ask}")

        # Leg 0: BUY BTC with USDT
        raw_btc_qty = position_usdt / btc_ask
        btc_qty = _floor_to_step(raw_btc_qty, btc_rules["amount_step"]) if btc_rules["amount_step"] else raw_btc_qty
        print(f"Plan Leg 0 (BTCUSDT): BUY {btc_qty} BTC @ {btc_ask} (theoretical cost: {btc_qty * btc_ask} USDT)")

        # Leg 1: BUY ETH with the theoretical BTC received from Leg 0
        # On ETH/BTC: Base=ETH, Quote=BTC. A BUY spends quote (BTC) to buy base (ETH).
        eth_rules = _extract_market_rules(markets["ETH/BTC"])
        raw_eth_qty = btc_qty / eth_ask
        eth_qty = _floor_to_step(raw_eth_qty, eth_rules["amount_step"]) if eth_rules["amount_step"] else raw_eth_qty
        print(f"Plan Leg 1 (ETH/BTC): BUY {eth_qty} ETH @ {eth_ask} (theoretical cost: {eth_qty * eth_ask} BTC)")

        # 4. Fire both orders simultaneously via asyncio.gather
        print("Firing both FOK orders concurrently via asyncio.gather...")
        t0 = loop.time()
        task0 = adapter.place_fok_order("BTCUSDT", "BUY", btc_qty, btc_ask)
        task1 = adapter.place_fok_order("ETH/BTC", "BUY", eth_qty, eth_ask)
        results = await asyncio.gather(task0, task1, return_exceptions=True)
        duration_ms = int((loop.time() - t0) * 1000)
        print(f"Concurrent gather completed in {duration_ms} ms")
    finally:
        # 5. Immediately unlock BTC
        if lock_order_id:
            print(f"Cancelling lock order {lock_order_id}...")
            try:
                await loop.run_in_executor(
                    None, lambda: adapter._client.cancel_order(lock_order_id, "BTC/USDT")
                )
                print("Lock order cancelled successfully.")
            except Exception as e:
                print(f"WARNING: Failed to cancel lock order: {e}")

    # 6. Parse and print raw results
    iter_summary = {
        "iteration": iteration,
        "duration_ms": duration_ms,
        "leg0": None,
        "leg1": None,
    }

    res0 = results[0]
    res1 = results[1]

    print("\n--- RAW RESULT LEG 0 (BTCUSDT) ---")
    if isinstance(res0, Exception):
        print(f"EXCEPTION: {type(res0).__name__}: {res0}")
        iter_summary["leg0"] = {"exception": f"{type(res0).__name__}: {res0}"}
    else:
        print(f"Status: {res0.status}, filled_qty: {res0.filled_qty}, is_filled: {res0.is_filled}, id: {res0.order_id}")
        print("Raw payload:")
        print(json.dumps(json_safe(res0.raw), indent=2))
        iter_summary["leg0"] = {
            "status": res0.status,
            "filled_qty": str(res0.filled_qty),
            "is_filled": res0.is_filled,
            "order_id": res0.order_id,
            "raw": res0.raw,
        }

    print("\n--- RAW RESULT LEG 1 (ETH/BTC) ---")
    if isinstance(res1, Exception):
        print(f"EXCEPTION: {type(res1).__name__}: {res1}")
        iter_summary["leg1"] = {"exception": f"{type(res1).__name__}: {res1}"}
    else:
        print(f"Status: {res1.status}, filled_qty: {res1.filled_qty}, is_filled: {res1.is_filled}, id: {res1.order_id}")
        print("Raw payload:")
        print(json.dumps(json_safe(res1.raw), indent=2))
        iter_summary["leg1"] = {
            "status": res1.status,
            "filled_qty": str(res1.filled_qty),
            "is_filled": res1.is_filled,
            "order_id": res1.order_id,
            "raw": res1.raw,
        }

    # 7. Unwind any positions acquired to return to starting state
    if not isinstance(res1, Exception) and res1.is_filled and res1.filled_qty > Decimal("0"):
        print(f"\nUnwinding acquired ETH ({res1.filled_qty}) via market sell on ETHUSDT...")
        try:
            unwind_eth = await adapter.place_market_order("ETHUSDT", "SELL", res1.filled_qty)
            print(f"ETH Unwind result: status={unwind_eth.status}, filled_qty={unwind_eth.filled_qty}")
        except Exception as e:
            print(f"ETH Unwind failed: {e}")
    elif not isinstance(res0, Exception) and res0.is_filled and res0.filled_qty > Decimal("0"):
        print(f"\nUnwinding bought BTC ({res0.filled_qty}) via market sell on BTCUSDT...")
        try:
            unwind_btc = await adapter.place_market_order("BTCUSDT", "SELL", res0.filled_qty)
            print(f"BTC Unwind result: status={unwind_btc.status}, filled_qty={unwind_btc.filled_qty}")
        except Exception as e:
            print(f"BTC Unwind failed: {e}")

    # Check ending balances
    btc_bal_final = await adapter.get_balance("BTC")
    eth_bal_final = await adapter.get_balance("ETH")
    usdt_bal_final = await adapter.get_balance("USDT")
    print(f"Ending BTC:  free={btc_bal_final.free}, locked={btc_bal_final.locked}")
    print(f"Ending ETH:  free={eth_bal_final.free}, locked={eth_bal_final.locked}")
    print(f"Ending USDT: free={usdt_bal_final.free}, locked={usdt_bal_final.locked}")

    return iter_summary


async def main():
    settings = get_settings()
    assert settings.BINANCE_TESTNET and not settings.DRY_RUN, "Must have BINANCE_TESTNET=True, DRY_RUN=False"
    adapter = await BinanceAdapter.create(settings)

    all_runs = []
    for i in range(1, 4):
        summary = await run_single_iteration(adapter, i)
        all_runs.append(summary)
        await asyncio.sleep(2)  # Pause between iterations to let exchange state settle

    print("\n" + "="*80)
    print("ALL ITERATIONS SUMMARY")
    print("="*80)
    for r in all_runs:
        print(f"Run {r['iteration']} ({r['duration_ms']}ms):")
        leg0_stat = r['leg0'].get('status') or r['leg0'].get('exception')
        leg1_stat = r['leg1'].get('status') or r['leg1'].get('exception')
        print(f"  Leg 0: {leg0_stat}")
        print(f"  Leg 1: {leg1_stat}")


if __name__ == "__main__":
    asyncio.run(main())