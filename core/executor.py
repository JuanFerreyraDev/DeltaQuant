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
import time
from typing import Awaitable, Callable, Optional

from loguru import logger

from config.settings import Settings, get_settings
from core.graph import Triangle
from core.risk import RiskManager
from exchanges.base import ExchangeAdapter
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
    ) -> ExecutionResult:  # pragma: no cover — rehearsed in Phase 5
        """Execute parallel REST FOK orders in live mode (stub for Phase 5)."""
        duration_ms = (time.time_ns() - start_ns) // 1_000_000
        return ExecutionResult(
            triangle_id=triangle_id,
            status="SIMULATED",
            expected_net_return=expected_net_return,
            actual_net_return=expected_net_return,
            execution_duration_ms=duration_ms,
            legs_filled=3,
        )

    async def _reconcile_inventory(
        self,
        pair_symbols: tuple[str, str, str],
        failed_leg_index: int,
        position_usdt: Decimal,
        expected_net_return: Decimal,
        triangle_id: str,
        start_ns: int,
        error_msg: str,
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
            # Emergency market order slippage loss estimate: -0.5% of position
            liquidation_pnl_usdt = -(position_usdt * Decimal("0.005"))
        else:
            failed_symbol = pair_symbols[2]
            liquidation_symbol = pair_symbols[1]
            # Emergency market order slippage loss estimate: -1.0% of position
            liquidation_pnl_usdt = -(position_usdt * Decimal("0.01"))

        unhedged_amount = position_usdt
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
