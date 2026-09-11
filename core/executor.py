"""Triangular arbitrage execution and inventory reconciliation engine for DeltaQuant.

Handles:
    - Parallel 3-leg order dispatch via ``asyncio.gather`` (planned for Phase 5 — DRY_RUN mode in this phase simulates leg outcomes via simulated_leg_failures).
    - Pre-execution safety filtering via ``RiskManager``.
    - Simulated order placement and fill evaluation in ``DRY_RUN`` mode.
    - Post-failure inventory reconciliation and emergency market liquidation.
    - Incident logging to SQLite via ``DatabaseManager``.

Design contract — pair_symbols ordering:
    ``execute_triangle`` requires the caller to pass ``pair_symbols`` in actual
    **execution order** (Leg 0, Leg 1, Leg 2), sourced from
    ``core.evaluator.evaluate_triangle``'s returned ``EvaluationResult.pair_symbols``
    for the chosen profitable path.

    ``Triangle.pair_ab / pair_bc / pair_ca`` are in *canonical alphabetical
    asset order* defined by ``core.graph`` — this is NOT the same as execution
    order, which depends on which of the two directional paths the evaluator
    selects as profitable.  Never derive leg order from canonical Triangle
    fields; always pass the evaluator's ``pair_symbols`` tuple explicitly.
"""

from dataclasses import dataclass
from decimal import Decimal
import asyncio
import time
from typing import Awaitable, Callable, Mapping, Optional

from loguru import logger

from config.settings import Settings, get_settings
from core.graph import Triangle, parse_symbol
from core.risk import RiskManager
from exchanges.base import BookTicker, ExchangeAdapter, OrderResult
from storage.database import DatabaseManager
from storage.models import Incident, Trade


@dataclass(frozen=True)
class ExecutionResult:
    """Dataclass encapsulating the outcome of a triangle execution attempt.

    Attributes:
        triangle_id: Unique execution identifier string.
        status: Execution status string.
        expected_net_return: Theoretical return multiplier from evaluator.
        actual_net_return: Realized return multiplier (1.0 = breakeven / no loss,
            >1.0 = profit, <1.0 = loss).
        execution_duration_ms: Total duration in milliseconds.
        legs_filled: Number of legs successfully filled (0, 1, 2, or 3).
        error_message: Optional error message string if execution failed.
        incident_id: Optional database incident ID if reconciliation was triggered.
    """

    triangle_id: str
    status: str  # "COMPLETED", "FAILED_RECONCILED", "REJECTED_RISK", "SIMULATED", "FAILED_LEG_0"
    expected_net_return: Decimal
    actual_net_return: Decimal
    execution_duration_ms: int
    legs_filled: int
    error_message: Optional[str] = None
    incident_id: Optional[int] = None


class Executor:
    """Engine responsible for parallel leg dispatch, dry-run simulation, and reconciliation.

    Attributes:
        adapter: ExchangeAdapter instance for REST/WS order placement.
        risk_manager: RiskManager instance for pre-execution risk checks.
        db_manager: Optional DatabaseManager for persistence.
        settings: Application settings.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        risk_manager: RiskManager,
        db_manager: Optional[DatabaseManager] = None,
        settings: Optional[Settings] = None,
        incident_alert_sender: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        """Initialize the Executor engine.

        Args:
            adapter: Exchange adapter instance.
            risk_manager: Risk manager instance.
            db_manager: Database manager instance for recording trades and incidents.
            settings: Settings instance.
            incident_alert_sender: Optional async callback for incident alerts.
        """
        self.adapter = adapter
        self.risk_manager = risk_manager
        self.db_manager = db_manager
        self.settings = settings or get_settings()
        self.incident_alert_sender = incident_alert_sender

    async def execute_triangle(
        self,
        triangle: Triangle,
        pair_symbols: tuple[str, str, str],
        position_usdt: Decimal,
        expected_net_return: Decimal,
        path: Optional[tuple[str, str, str, str]] = None,
        tickers: Optional[Mapping[str, BookTicker]] = None,
        simulated_leg_failures: Optional[dict[int, bool]] = None,
    ) -> ExecutionResult:
        """Execute a 3-leg triangular arbitrage cycle.

        Args:
            triangle: Canonical Triangle object (used for ID and persistence).
            pair_symbols: Tuple of (leg0_symbol, leg1_symbol, leg2_symbol) in actual
                **execution order**, taken directly from
                ``EvaluationResult.pair_symbols`` returned by
                ``core.evaluator.evaluate_triangle``.  This is NOT the same as the
                canonical Triangle.pair_ab / pair_bc / pair_ca ordering.
            position_usdt: Capital allocated to Leg 0 in USDT.
            expected_net_return: Theoretical net return multiplier from evaluator.
            simulated_leg_failures: Optional dict mapping leg index (0, 1, 2) to True
                if that leg should be forced to fail (used for dry-run testing).

        Returns:
            ExecutionResult dataclass detailing execution metrics and status.
        """
        start_ns = time.time_ns()
        now_ms = start_ns // 1_000_000
        leg0, leg1, leg2 = pair_symbols
        triangle_id = f"{leg0}_{leg1}_{leg2}_{now_ms}"

        # ── Step 1: Risk Manager Pre-Execution Gate ───────────────────────────
        can_exec, reject_reason = self.risk_manager.can_execute(
            position_usdt=position_usdt, current_time_ms=now_ms
        )
        if not can_exec:
            duration_ms = (time.time_ns() - start_ns) // 1_000_000
            logger.warning(
                "executor_rejected_by_risk triangle_id='{}' reason='{}'",
                triangle_id,
                reject_reason,
            )
            return ExecutionResult(
                triangle_id=triangle_id,
                status="REJECTED_RISK",
                expected_net_return=expected_net_return,
                actual_net_return=Decimal("0"),
                execution_duration_ms=duration_ms,
                legs_filled=0,
                error_message=reject_reason,
            )

        # Register start of execution in RiskManager
        self.risk_manager.register_execution_start()

        try:
            # ── Step 2: Execution Path (DRY_RUN vs LIVE) ───────────────────────
            if self.settings.DRY_RUN:
                res = await self._execute_dry_run(
                    triangle=triangle,
                    pair_symbols=pair_symbols,
                    position_usdt=position_usdt,
                    expected_net_return=expected_net_return,
                    triangle_id=triangle_id,
                    start_ns=start_ns,
                    simulated_leg_failures=simulated_leg_failures,
                )
            else:  # pragma: no cover — live path rehearsed in Phase 5
                res = await self._execute_live(
                    triangle=triangle,
                    pair_symbols=pair_symbols,
                    position_usdt=position_usdt,
                    expected_net_return=expected_net_return,
                    triangle_id=triangle_id,
                    start_ns=start_ns,
                    path=path,
                    tickers=tickers,
                )

            # ── Step 3: Register PnL and persist Trade ─────────────────────────
            pnl_usdt = (res.actual_net_return - Decimal("1.0")) * position_usdt
            self.risk_manager.register_execution_end(pnl_usdt)

            if self.db_manager:
                await self._persist_trade(res, path_str=f"{leg0}->{leg1}->{leg2}")

            return res

        except Exception as exc:
            duration_ms = (time.time_ns() - start_ns) // 1_000_000
            self.risk_manager.register_execution_end(Decimal("0"))
            logger.error("executor_unhandled_exception error='{}'", exc)
            return ExecutionResult(
                triangle_id=triangle_id,
                status="FAILED_UNHANDLED",
                expected_net_return=expected_net_return,
                actual_net_return=Decimal("0"),
                execution_duration_ms=duration_ms,
                legs_filled=0,
                error_message=str(exc),
            )

    async def _execute_dry_run(
        self,
        triangle: Triangle,
        pair_symbols: tuple[str, str, str],
        position_usdt: Decimal,
        expected_net_return: Decimal,
        triangle_id: str,
        start_ns: int,
        simulated_leg_failures: Optional[dict[int, bool]] = None,
    ) -> ExecutionResult:
        """Simulate parallel 3-leg FOK dispatch in DRY_RUN mode.

        Args:
            pair_symbols: Execution-order symbols from the evaluator's result.
        """
        sim_failures = simulated_leg_failures or {}

        # Leg 0 failure: no inventory unhedged, safe to abort immediately
        if sim_failures.get(0, False):
            duration_ms = (time.time_ns() - start_ns) // 1_000_000
            return ExecutionResult(
                triangle_id=triangle_id,
                status="FAILED_LEG_0",
                expected_net_return=expected_net_return,
                actual_net_return=Decimal("1.0"),
                execution_duration_ms=duration_ms,
                legs_filled=0,
                error_message=(
                    f"Simulated Leg 0 ({pair_symbols[0]}) FOK expiration "
                    f"(0 legs filled, zero unhedged inventory)"
                ),
            )

        # Leg 1 failure: Leg 0 filled → we hold intermediate asset
        if sim_failures.get(1, False):
            return await self._reconcile_inventory(
                pair_symbols=pair_symbols,
                failed_leg_index=1,
                position_usdt=position_usdt,
                expected_net_return=expected_net_return,
                triangle_id=triangle_id,
                start_ns=start_ns,
                error_msg=(
                    f"Simulated Leg 1 ({pair_symbols[1]}) FOK expiration "
                    f"(Leg 0 filled, Leg 1 failed)"
                ),
            )

        # Leg 2 failure: Legs 0+1 filled → we hold second intermediate asset
        if sim_failures.get(2, False):
            return await self._reconcile_inventory(
                pair_symbols=pair_symbols,
                failed_leg_index=2,
                position_usdt=position_usdt,
                expected_net_return=expected_net_return,
                triangle_id=triangle_id,
                start_ns=start_ns,
                error_msg=(
                    f"Simulated Leg 2 ({pair_symbols[2]}) FOK expiration "
                    f"(Legs 0 and 1 filled, Leg 2 failed)"
                ),
            )

        # All 3 legs filled successfully
        actual_net_return = expected_net_return
        duration_ms = (time.time_ns() - start_ns) // 1_000_000

        logger.info(
            "executor_dry_run_success triangle_id='{}' net_return={} duration_ms={}",
            triangle_id,
            actual_net_return,
            duration_ms,
        )

        return ExecutionResult(
            triangle_id=triangle_id,
            status="SIMULATED",
            expected_net_return=expected_net_return,
            actual_net_return=actual_net_return,
            execution_duration_ms=duration_ms,
            legs_filled=3,
        )

    async def _execute_live(
        self,
        triangle: Triangle,
        pair_symbols: tuple[str, str, str],
        position_usdt: Decimal,
        expected_net_return: Decimal,
        triangle_id: str,
        start_ns: int,
        path: Optional[tuple[str, str, str, str]] = None,
        tickers: Optional[Mapping[str, BookTicker]] = None,
    ) -> ExecutionResult:  # pragma: no cover — rehearsed in Phase 5
        """Execute the 3 live legs sequentially and reconcile on failure (ADR-009)."""
        if path is None or tickers is None:
            raise ValueError("Live execution requires both the chosen path and the current ticker snapshot")

        # ── Step 1: Leg 0 Execution ───────────────────────────────────────────
        # Leg 0 is sized from initial position_usdt
        leg0_plan = self._build_single_leg_plan(
            pair_symbol=pair_symbols[0],
            asset_x=path[0],
            asset_y=path[1],
            amount_in=position_usdt,
            tickers=tickers,
        )

        try:
            res0 = await self.adapter.place_fok_order(
                symbol=str(leg0_plan["symbol"]),
                side=str(leg0_plan["side"]),
                quantity=Decimal(str(leg0_plan["quantity"])),
                price=Decimal(str(leg0_plan["price"])),
            )
        except Exception as exc:
            logger.error("live_order_failed triangle_id='{}' leg=0 err='{}'", triangle_id, exc)
            res0 = OrderResult(
                symbol=str(leg0_plan["symbol"]),
                order_id="",
                status="ERROR",
                filled_qty=Decimal("0"),
                avg_price=Decimal("0"),
                fee=Decimal("0"),
                fee_asset="",
                raw={"exception": str(exc)},
            )

        if not res0.is_filled:
            duration_ms = (time.time_ns() - start_ns) // 1_000_000
            logger.info(
                "executor_live_leg0_expired triangle_id='{}' status='{}' duration_ms={}",
                triangle_id,
                res0.status,
                duration_ms,
            )
            return ExecutionResult(
                triangle_id=triangle_id,
                status="FAILED_LEG_0",
                expected_net_return=expected_net_return,
                actual_net_return=Decimal("1.0"),
                execution_duration_ms=duration_ms,
                legs_filled=0,
                error_message=f"Live Leg 0 ({pair_symbols[0]}) FOK expiration (0 legs filled, zero unhedged inventory)",
            )

        # ── Step 2: Leg 1 Execution ───────────────────────────────────────────
        # Leg 1 is sized using the REAL confirmed output from Leg 0 (ADR-009)
        # If Leg 0 fee was deducted in the received asset, net it out
        leg0_net_output = self._net_of_fee(res0, path[1])

        leg1_plan = self._build_single_leg_plan(
            pair_symbol=pair_symbols[1],
            asset_x=path[1],
            asset_y=path[2],
            amount_in=leg0_net_output,
            tickers=tickers,
        )

        try:
            res1 = await self.adapter.place_fok_order(
                symbol=str(leg1_plan["symbol"]),
                side=str(leg1_plan["side"]),
                quantity=Decimal(str(leg1_plan["quantity"])),
                price=Decimal(str(leg1_plan["price"])),
            )
        except Exception as exc:
            logger.error("live_order_failed triangle_id='{}' leg=1 err='{}'", triangle_id, exc)
            res1 = OrderResult(
                symbol=str(leg1_plan["symbol"]),
                order_id="",
                status="ERROR",
                filled_qty=Decimal("0"),
                avg_price=Decimal("0"),
                fee=Decimal("0"),
                fee_asset="",
                raw={"exception": str(exc)},
            )

        if not res1.is_filled:
            # Leg 0 filled, Leg 1 failed → Reconcile Leg 0's actual held inventory
            return await self._reconcile_inventory(
                pair_symbols=pair_symbols,
                failed_leg_index=1,
                position_usdt=position_usdt,
                expected_net_return=expected_net_return,
                triangle_id=triangle_id,
                start_ns=start_ns,
                error_msg=(
                    f"Live Leg 1 ({pair_symbols[1]}) FOK expiration "
                    f"(Leg 0 filled, Leg 1 failed)"
                ),
                order_plan=[leg0_plan, leg1_plan],
                order_results=[res0, res1],
                path=path,
                tickers=tickers,
            )

        # ── Step 3: Leg 2 Execution ───────────────────────────────────────────
        # Leg 2 is sized using the REAL confirmed output from Leg 1 (ADR-009)
        leg1_net_output = self._net_of_fee(res1, path[2])

        leg2_plan = self._build_single_leg_plan(
            pair_symbol=pair_symbols[2],
            asset_x=path[2],
            asset_y=path[3],
            amount_in=leg1_net_output,
            tickers=tickers,
        )

        try:
            res2 = await self.adapter.place_fok_order(
                symbol=str(leg2_plan["symbol"]),
                side=str(leg2_plan["side"]),
                quantity=Decimal(str(leg2_plan["quantity"])),
                price=Decimal(str(leg2_plan["price"])),
            )
        except Exception as exc:
            logger.error("live_order_failed triangle_id='{}' leg=2 err='{}'", triangle_id, exc)
            res2 = OrderResult(
                symbol=str(leg2_plan["symbol"]),
                order_id="",
                status="ERROR",
                filled_qty=Decimal("0"),
                avg_price=Decimal("0"),
                fee=Decimal("0"),
                fee_asset="",
                raw={"exception": str(exc)},
            )

        if not res2.is_filled:
            # Legs 0 and 1 filled, Leg 2 failed → Reconcile Leg 1's actual held inventory
            return await self._reconcile_inventory(
                pair_symbols=pair_symbols,
                failed_leg_index=2,
                position_usdt=position_usdt,
                expected_net_return=expected_net_return,
                triangle_id=triangle_id,
                start_ns=start_ns,
                error_msg=(
                    f"Live Leg 2 ({pair_symbols[2]}) FOK expiration "
                    f"(Legs 0 and 1 filled, Leg 2 failed)"
                ),
                order_plan=[leg0_plan, leg1_plan, leg2_plan],
                order_results=[res0, res1, res2],
                path=path,
                tickers=tickers,
            )

        # ── All 3 legs filled successfully ────────────────────────────────────
        duration_ms = (time.time_ns() - start_ns) // 1_000_000
        # Calculate actual net return from real fills: USDT returned / initial position_usdt
        # Leg 2 returns quote asset of Leg 2 (which is USDT at path[3])
        gross_usdt = res2.filled_qty * res2.avg_price
        usdt_received = self._net_of_fee(res2, "USDT", amount=gross_usdt)
        actual_net_return = usdt_received / position_usdt if position_usdt > Decimal("0") else expected_net_return

        logger.info(
            "executor_live_success triangle_id='{}' net_return={} duration_ms={}",
            triangle_id,
            actual_net_return,
            duration_ms,
        )
        return ExecutionResult(
            triangle_id=triangle_id,
            status="COMPLETED",
            expected_net_return=expected_net_return,
            actual_net_return=actual_net_return,
            execution_duration_ms=duration_ms,
            legs_filled=3,
        )

    def _build_single_leg_plan(
        self,
        pair_symbol: str,
        asset_x: str,
        asset_y: str,
        amount_in: Decimal,
        tickers: Mapping[str, BookTicker],
    ) -> dict[str, Decimal | str]:
        """Build an order request for a single leg from current book and input amount."""
        if pair_symbol not in tickers:
            raise ValueError(f"Missing ticker for live execution symbol {pair_symbol!r}")

        ticker = tickers[pair_symbol]
        parsed = parse_symbol(pair_symbol)
        if parsed is None:
            raise ValueError(f"Could not parse live execution symbol {pair_symbol!r}")
        base, quote = parsed

        if asset_x == quote and asset_y == base:
            side = "BUY"
            price = ticker.ask
            quantity = amount_in / price
        elif asset_x == base and asset_y == quote:
            side = "SELL"
            price = ticker.bid
            quantity = amount_in
        else:
            raise ValueError(
                f"Pair {pair_symbol} does not connect live path leg {asset_x}->{asset_y}"
            )

        return {
            "symbol": pair_symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
        }

    def _build_live_order_plan(
        self,
        path: tuple[str, str, str, str],
        pair_symbols: tuple[str, str, str],
        tickers: Mapping[str, BookTicker],
        position_usdt: Decimal,
    ) -> list[dict[str, Decimal | str]]:
        """Build theoretical order requests for all 3 legs (used in testing and dry-run)."""
        amount = position_usdt
        plan: list[dict[str, Decimal | str]] = []

        for pair_symbol, asset_x, asset_y in zip(pair_symbols, path[:-1], path[1:]):
            leg_plan = self._build_single_leg_plan(
                pair_symbol=pair_symbol,
                asset_x=asset_x,
                asset_y=asset_y,
                amount_in=amount,
                tickers=tickers,
            )
            # Update running amount for next theoretical leg
            if leg_plan["side"] == "BUY":
                amount = Decimal(str(leg_plan["quantity"]))
            else:
                amount = Decimal(str(leg_plan["quantity"])) * Decimal(str(leg_plan["price"]))
            plan.append(leg_plan)

        return plan

    @staticmethod
    def _net_of_fee(
        result: OrderResult,
        asset: str,
        amount: Optional[Decimal] = None,
    ) -> Decimal:
        """Net fee from amount (defaulting to result.filled_qty) if fee was charged in asset.

        If the fee is charged in another asset (e.g. BNB discount when trading BTC/USDT),
        no netting against `asset` is applied.
        """
        qty = result.filled_qty if amount is None else amount
        if result.fee_asset == asset and result.fee > Decimal("0"):
            return max(Decimal("0"), qty - result.fee)
        return qty

    @staticmethod
    def _asset_to_usdt_rate(asset: str, tickers: Mapping[str, BookTicker]) -> Decimal:
        """Return the current conversion rate from one asset to USDT."""
        if asset == "USDT":
            return Decimal("1")

        for symbol, ticker in tickers.items():
            parsed = parse_symbol(symbol)
            if parsed is None:
                continue
            base, quote = parsed
            if base == asset and quote == "USDT":
                return ticker.bid
            if base == "USDT" and quote == asset and ticker.ask > Decimal("0"):
                return Decimal("1") / ticker.ask

        raise ValueError(f"No USDT conversion ticker found for asset {asset!r}")

    def _calculate_live_liquidation_pnl_usdt(
        self,
        liquidation_symbol: str,
        liquidation_side: str,
        previous_result: OrderResult,
        liquidation_result: OrderResult,
        tickers: Mapping[str, BookTicker],
    ) -> Decimal:
        """Compute realized liquidation PnL in USDT from the live market order."""
        parsed = parse_symbol(liquidation_symbol)
        if parsed is None:
            raise ValueError(f"Could not parse liquidation symbol {liquidation_symbol!r}")
        _, quote_asset = parsed

        prev_avg_price = previous_result.avg_price
        if prev_avg_price <= Decimal("0"):
            prev_avg_price = Decimal(str(previous_result.raw.get("price") or "0"))

        liq_avg_price = liquidation_result.avg_price
        if liq_avg_price <= Decimal("0"):
            liq_avg_price = Decimal(str(liquidation_result.raw.get("price") or "0"))

        if liquidation_side == "SELL":
            proceeds_quote = liquidation_result.filled_qty * liq_avg_price
            cost_quote = previous_result.filled_qty * prev_avg_price
            pnl_quote = proceeds_quote - cost_quote
        else:
            proceeds_quote = previous_result.filled_qty * prev_avg_price
            cost_quote = liquidation_result.filled_qty * liq_avg_price
            pnl_quote = proceeds_quote - cost_quote

        return pnl_quote * self._asset_to_usdt_rate(quote_asset, tickers)

    async def _reconcile_inventory(
        self,
        pair_symbols: tuple[str, str, str],
        failed_leg_index: int,
        position_usdt: Decimal,
        expected_net_return: Decimal,
        triangle_id: str,
        start_ns: int,
        error_msg: str,
        order_plan: Optional[list[dict[str, Decimal | str]]] = None,
        order_results: Optional[list[OrderResult]] = None,
        path: Optional[tuple[str, str, str, str]] = None,
        tickers: Optional[Mapping[str, BookTicker]] = None,
    ) -> ExecutionResult:
        """Handle partial leg execution failure via emergency market liquidation.

        The liquidation and failed-symbol fields are derived exclusively from
        ``pair_symbols`` (the execution-order tuple from the evaluator), NOT from
        the canonical ``Triangle.pair_ab / pair_bc / pair_ca`` fields.

        Mapping:
            failed_leg_index == 1: Leg 1 (pair_symbols[1]) failed.
                We hold the output asset of Leg 0 (pair_symbols[0]).
                → liquidation_symbol = pair_symbols[0]
                → failed_symbol      = pair_symbols[1]
            failed_leg_index == 2: Leg 2 (pair_symbols[2]) failed.
                We hold the output asset of Leg 1 (pair_symbols[1]).
                → liquidation_symbol = pair_symbols[1]
                → failed_symbol      = pair_symbols[2]

        Args:
            pair_symbols: Execution-order symbols (leg0, leg1, leg2) from evaluator output.
            failed_leg_index: 1 or 2 (0-indexed; Leg 0 failure never reaches reconciliation).
            position_usdt: Initial trade size.
            expected_net_return: Theoretical net return.
            triangle_id: Unique execution ID string.
            start_ns: Execution start timestamp in nanoseconds.
            error_msg: Failure error message.

        Returns:
            ExecutionResult with status "FAILED_RECONCILED".
        """
        now_ms = time.time_ns() // 1_000_000

        # Resolve symbols from execution-order pair_symbols — NOT canonical Triangle fields
        if failed_leg_index == 1:
            failed_symbol = pair_symbols[1]
            liquidation_symbol = pair_symbols[0]
        else:
            failed_symbol = pair_symbols[2]
            liquidation_symbol = pair_symbols[1]

        unhedged_amount = position_usdt

        if order_plan is None or order_results is None or path is None or tickers is None:
            # Preserve the Phase 3 dry-run path unchanged.
            if failed_leg_index == 1:
                liquidation_pnl_usdt = -(position_usdt * Decimal("0.005"))
            else:
                liquidation_pnl_usdt = -(position_usdt * Decimal("0.01"))
            actual_net_return = Decimal("1.0") + (liquidation_pnl_usdt / position_usdt)
        else:
            previous_index = failed_leg_index - 1
            previous_plan = order_plan[previous_index]
            previous_result = order_results[previous_index]
            previous_side = str(previous_plan["side"])
            liquidation_side = "SELL" if previous_side == "BUY" else "BUY"
            liquidation_pair_ticker = tickers[liquidation_symbol]
            held_asset = path[previous_index + 1]

            if liquidation_side == "SELL":
                liquidation_quantity = self._net_of_fee(previous_result, held_asset)
            else:
                raw_quote_price = (
                    previous_result.avg_price
                    if previous_result.avg_price > Decimal("0")
                    else Decimal(str(previous_plan["price"]))
                )
                gross_quote = previous_result.filled_qty * raw_quote_price
                quote_received = self._net_of_fee(previous_result, held_asset, amount=gross_quote)
                if liquidation_pair_ticker.ask <= Decimal("0"):
                    raise ValueError(f"Invalid ask price for liquidation symbol {liquidation_symbol!r}")
                liquidation_quantity = quote_received / liquidation_pair_ticker.ask

            liquidation_result = await self.adapter.place_market_order(
                symbol=liquidation_symbol,
                side=liquidation_side,
                quantity=liquidation_quantity,
            )

            unhedged_amount = liquidation_result.filled_qty

            liquidation_pnl_usdt = self._calculate_live_liquidation_pnl_usdt(
                liquidation_symbol=liquidation_symbol,
                liquidation_side=liquidation_side,
                previous_result=previous_result,
                liquidation_result=liquidation_result,
                tickers=tickers,
            )
            actual_net_return = Decimal("1.0") + (liquidation_pnl_usdt / position_usdt)

        logger.error(
            "executor_reconciliation_triggered triangle_id='{}' failed_leg={} "
            "failed_symbol='{}' liquidation_symbol='{}' pnl_usdt={}",
            triangle_id,
            failed_leg_index,
            failed_symbol,
            liquidation_symbol,
            liquidation_pnl_usdt,
        )

        # Record incident in RiskManager (may trigger circuit breaker)
        paused_before = self.risk_manager.is_paused
        self.risk_manager.record_incident(timestamp_ms=now_ms)
        paused_after = self.risk_manager.is_paused

        incident_id: Optional[int] = None

        if self.db_manager:
            async with self.db_manager.session() as session:
                inc = Incident(
                    triangle_id=triangle_id,
                    failed_leg_index=failed_leg_index,
                    failed_symbol=failed_symbol,
                    error_message=error_msg,
                    liquidation_symbol=liquidation_symbol,
                    liquidation_amount=str(unhedged_amount),
                    liquidation_pnl_usdt=str(liquidation_pnl_usdt),
                    timestamp_ms=now_ms,
                )
                session.add(inc)
                await session.flush()
                incident_id = inc.id

        await self._send_incident_alert(
            triangle_id=triangle_id,
            failed_leg_index=failed_leg_index,
            failed_symbol=failed_symbol,
            liquidation_symbol=liquidation_symbol,
            liquidation_pnl_usdt=liquidation_pnl_usdt,
            incident_id=incident_id,
        )

        if (not paused_before) and paused_after and self.risk_manager.pause_reason:
            await self._safe_alert(
                "Circuit breaker tripped after reconciliation incidents. "
                f"reason={self.risk_manager.pause_reason}"
            )

        duration_ms = (time.time_ns() - start_ns) // 1_000_000

        return ExecutionResult(
            triangle_id=triangle_id,
            status="FAILED_RECONCILED",
            expected_net_return=expected_net_return,
            actual_net_return=actual_net_return,
            execution_duration_ms=duration_ms,
            legs_filled=failed_leg_index,
            error_message=error_msg,
            incident_id=incident_id,
        )

    async def _send_incident_alert(
        self,
        triangle_id: str,
        failed_leg_index: int,
        failed_symbol: str,
        liquidation_symbol: str,
        liquidation_pnl_usdt: Decimal,
        incident_id: Optional[int],
    ) -> None:
        """Format and dispatch reconciliation incident alerts.

        Args:
            triangle_id: Execution identifier.
            failed_leg_index: Failed leg index in execution order.
            failed_symbol: Failed pair symbol.
            liquidation_symbol: Emergency liquidation symbol.
            liquidation_pnl_usdt: Estimated or realized reconciliation PnL.
            incident_id: Optional persisted incident row id.
        """
        msg = (
            "Reconciliation incident recorded. "
            f"triangle_id={triangle_id} "
            f"incident_id={incident_id} "
            f"failed_leg={failed_leg_index} "
            f"failed_symbol={failed_symbol} "
            f"liquidation_symbol={liquidation_symbol} "
            f"liquidation_pnl_usdt={liquidation_pnl_usdt}"
        )
        await self._safe_alert(msg)

    async def _safe_alert(self, message: str) -> None:
        """Send an alert message without allowing send failures to bubble.

        Args:
            message: Alert body.
        """
        if self.incident_alert_sender is None:
            return
        try:
            await self.incident_alert_sender(message)
        except Exception as exc:
            logger.error("incident_alert_send_failed err='{}'", exc)

    async def _persist_trade(self, result: ExecutionResult, path_str: str) -> None:
        """Persist completed or failed ExecutionResult to SQLite database."""
        if not self.db_manager:
            return
        async with self.db_manager.session() as session:
            trade = Trade(
                triangle_id=result.triangle_id,
                path_str=path_str,
                expected_net_return=str(result.expected_net_return),
                actual_net_return=str(result.actual_net_return),
                status=result.status,
                execution_duration_ms=result.execution_duration_ms,
                timestamp_ms=time.time_ns() // 1_000_000,
            )
            session.add(trade)
