"""Integration and unit tests for core/executor.py — DRY_RUN execution and inventory reconciliation.

Testing philosophy (same contract as test_evaluator.py):
    - Every reconciliation loss is verified by hand-computed arithmetic in the docstring.
    - Integration tests go through the REAL pipeline:
          generate_triangles → evaluate_triangle → execute_triangle(pair_symbols=result.pair_symbols)
      so that pair_symbols is sourced from the evaluator — the sole owner of execution order —
      not assumed from canonical Triangle field ordering.
    - This is the test category that would have caught the canonical-order / execution-order
      confusion bug described in the Phase 3 review: two modules each correct in isolation,
      broken at the boundary.

Arithmetic verification notes:
    Leg 1 failure, position_usdt = 100:
        liquidation_pnl_usdt = -100 × 0.005 = -0.500 USDT
        actual_net_return    = 1.0 + (-0.500 / 100) = 0.995 exactly

    Leg 2 failure, position_usdt = 100:
        liquidation_pnl_usdt = -100 × 0.01 = -1.000 USDT
        actual_net_return    = 1.0 + (-1.000 / 100) = 0.990 exactly
"""

from decimal import Decimal
import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import text

from config.settings import Settings
from core.executor import Executor, ExecutionResult
from core.graph import TradingPair, Triangle, generate_triangles
from core.risk import RiskManager
from core.evaluator import evaluate_triangle
from exchanges.base import BookTicker, OrderResult, TradingFees
from exchanges.fees import apply_bnb_discount
from storage.database import DatabaseManager


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def settings_dry_run():
    """Settings with DRY_RUN=True and tight risk limits for deterministic testing."""
    return Settings(
        BINANCE_API_KEY="test_key_123",
        BINANCE_API_SECRET="test_secret_456",
        TELEGRAM_BOT_TOKEN="123:ABC",
        TELEGRAM_CHAT_ID="999",
        DRY_RUN=True,
        MAX_POSITION_USDT=Decimal("100"),
        DAILY_LOSS_LIMIT_USDT=Decimal("-50"),
        MAX_CONCURRENT_TRIANGLES=2,
        CIRCUIT_BREAKER_INCIDENT_COUNT=3,
        CIRCUIT_BREAKER_WINDOW_MINUTES=60,
    )


@pytest.fixture
def settings_live_testnet():
    """Settings for live-order tests against Binance TESTNET."""
    return Settings(
        BINANCE_API_KEY="test_key_123",
        BINANCE_API_SECRET="test_secret_456",
        TESTNET_BINANCE_API_KEY="testnet_key_123",
        TESTNET_BINANCE_API_SECRET="testnet_secret_456",
        TELEGRAM_BOT_TOKEN="123:ABC",
        TELEGRAM_CHAT_ID="999",
        DRY_RUN=False,
        BINANCE_TESTNET=True,
        MAX_POSITION_USDT=Decimal("100"),
        DAILY_LOSS_LIMIT_USDT=Decimal("-50"),
        MAX_CONCURRENT_TRIANGLES=2,
        CIRCUIT_BREAKER_INCIDENT_COUNT=3,
        CIRCUIT_BREAKER_WINDOW_MINUTES=60,
    )


@pytest.fixture
def risk_manager(settings_dry_run):
    """Fresh RiskManager using test settings."""
    return RiskManager(settings=settings_dry_run)


@pytest.fixture
def executor(risk_manager, settings_dry_run):
    """Executor with mock adapter and no DB (unit-level tests)."""
    return Executor(
        adapter=MagicMock(),
        risk_manager=risk_manager,
        db_manager=None,
        settings=settings_dry_run,
    )


@pytest.fixture
def live_executor(settings_live_testnet):
    """Executor with a mock adapter for live-path tests."""
    adapter = AsyncMock()
    adapter.place_fok_order = AsyncMock()
    adapter.place_market_order = AsyncMock()
    return Executor(
        adapter=adapter,
        risk_manager=RiskManager(settings=settings_live_testnet),
        db_manager=None,
        settings=settings_live_testnet,
    )


@pytest.fixture
async def db_executor(risk_manager, settings_dry_run, tmp_path):
    """Executor wired to a real in-process SQLite test database."""
    db_file = tmp_path / "test_executor.db"
    db = DatabaseManager(database_url=f"sqlite+aiosqlite:///{db_file}")
    await db.init_db()
    ex = Executor(
        adapter=MagicMock(),
        risk_manager=risk_manager,
        db_manager=db,
        settings=settings_dry_run,
    )
    yield ex
    await db.close()


@pytest.fixture
def real_pipeline():
    """Build a real triangle and market via the full generate_triangles + evaluate_triangle pipeline.

    Returns:
        (triangle, tickers, fees, best_result) where best_result.pair_symbols
        is the actual execution-order tuple (BTCUSDT, ETH/BTC, ETHUSDT) for the
        profitable path USDT → BTC → ETH → USDT.
    """
    pairs = [
        TradingPair("BTCUSDT", "BTC", "USDT", Decimal("1000000")),
        TradingPair("ETHUSDT", "ETH", "USDT", Decimal("1000000")),
        TradingPair("ETH/BTC", "ETH", "BTC", Decimal("1000000")),
    ]
    triangles = generate_triangles(pairs)
    assert len(triangles) == 1
    triangle = triangles[0]

    tickers = {
        "BTCUSDT": BookTicker("BTCUSDT", Decimal("49990"), Decimal("50000"), 1000),
        "ETH/BTC": BookTicker("ETH/BTC", Decimal("0.0499"), Decimal("0.05"), 1000),
        "ETHUSDT": BookTicker("ETHUSDT", Decimal("2530"), Decimal("2531"), 1000),
    }
    raw_fees = {s: TradingFees(s, Decimal("0.001"), Decimal("0.001"))
                for s in ("BTCUSDT", "ETH/BTC", "ETHUSDT")}
    fees = {s: apply_bnb_discount(f) for s, f in raw_fees.items()}

    results = evaluate_triangle(triangle, tickers, fees, Decimal("0.0010"))
    best = results[0]

    # Verify this fixture produces the expected profitable path (guard against future regressions)
    assert best.path == ("USDT", "BTC", "ETH", "USDT"), (
        f"Fixture broken: expected profitable path (USDT,BTC,ETH,USDT), got {best.path}"
    )
    assert best.pair_symbols == ("BTCUSDT", "ETH/BTC", "ETHUSDT"), (
        f"Fixture broken: expected pair_symbols (BTCUSDT, ETH/BTC, ETHUSDT), "
        f"got {best.pair_symbols}"
    )
    return triangle, tickers, fees, best


# ── Integration tests: real evaluator → executor pipeline ────────────────────

class TestIntegrationRealPipeline:
    """Tests using real generate_triangles + evaluate_triangle → execute_triangle.

    These are the tests that would have caught the canonical-order / execution-order bug:
    pair_symbols comes from EvaluationResult.pair_symbols, not from Triangle.pair_ab etc.
    """

    @pytest.mark.asyncio
    async def test_successful_dry_run_uses_evaluator_pair_symbols(
        self, executor, real_pipeline
    ):
        """Happy path: evaluator pair_symbols → executor → SIMULATED.

        Verifies pair_symbols routing: the canonical triangle for BTC/ETH/USDT has
        pair_ab='ETHBTC' (or similar), but the profitable execution order from the
        evaluator is (BTCUSDT, ETH/BTC, ETHUSDT). The executor must use the latter.
        """
        triangle, _, _, best = real_pipeline
        # Verify canonical pair ordering differs from execution ordering
        assert triangle.pair_ab != best.pair_symbols[0], (
            "Fixture setup error: canonical pair_ab should not match execution leg 0 "
            "for this BTC/ETH/USDT triangle — if they happen to match, the bug is invisible."
        )

        result = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("50"),
            expected_net_return=best.net_return,
        )
        assert result.status == "SIMULATED"
        assert result.legs_filled == 3
        assert result.actual_net_return == best.net_return

    @pytest.mark.asyncio
    async def test_leg1_failure_failed_and_liquidation_symbols_from_evaluator(
        self, executor, real_pipeline
    ):
        """Leg 1 failure: failed_symbol and liquidation_symbol must match evaluator pair_symbols.

        Pair symbols from evaluator (execution order): (BTCUSDT, ETH/BTC, ETHUSDT)
            failed_leg_index = 1  →  failed_symbol      = pair_symbols[1] = 'ETH/BTC'
                                     liquidation_symbol = pair_symbols[0] = 'BTCUSDT'

        This would produce WRONG results if _reconcile_inventory used
        triangle.pair_ab / pair_bc (canonical order) instead of pair_symbols.
        """
        triangle, _, _, best = real_pipeline
        assert best.pair_symbols == ("BTCUSDT", "ETH/BTC", "ETHUSDT")

        result = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            simulated_leg_failures={1: True},
        )
        assert result.status == "FAILED_RECONCILED"
        assert result.legs_filled == 1
        # Error message must reference the actual failed pair symbol (from evaluator)
        assert "ETH/BTC" in result.error_message, (
            f"Expected failed symbol 'ETH/BTC' in error_message, got: {result.error_message}"
        )

    @pytest.mark.asyncio
    async def test_leg2_failure_failed_and_liquidation_symbols_from_evaluator(
        self, executor, real_pipeline
    ):
        """Leg 2 failure: failed_symbol and liquidation_symbol must match evaluator pair_symbols.

        Pair symbols from evaluator (execution order): (BTCUSDT, ETH/BTC, ETHUSDT)
            failed_leg_index = 2  →  failed_symbol      = pair_symbols[2] = 'ETHUSDT'
                                     liquidation_symbol = pair_symbols[1] = 'ETH/BTC'
        """
        triangle, _, _, best = real_pipeline
        assert best.pair_symbols == ("BTCUSDT", "ETH/BTC", "ETHUSDT")

        result = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            simulated_leg_failures={2: True},
        )
        assert result.status == "FAILED_RECONCILED"
        assert result.legs_filled == 2
        assert "ETHUSDT" in result.error_message, (
            f"Expected failed symbol 'ETHUSDT' in error_message, got: {result.error_message}"
        )

    @pytest.mark.asyncio
    async def test_reconciliation_persists_correct_symbols_to_database(
        self, db_executor, real_pipeline
    ):
        """Leg 1 failure persists correct failed_symbol and liquidation_symbol to SQLite.

        This is the integration-level proof: the Incident row in the database must
        contain the execution-order symbols (from the evaluator), not canonical
        Triangle field values.

        Pair symbols from evaluator: (BTCUSDT, ETH/BTC, ETHUSDT)
            expected failed_symbol      in DB = 'ETH/BTC'  (pair_symbols[1])
            expected liquidation_symbol in DB = 'BTCUSDT'  (pair_symbols[0])
        """
        triangle, _, _, best = real_pipeline
        assert best.pair_symbols == ("BTCUSDT", "ETH/BTC", "ETHUSDT")

        result = await db_executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            simulated_leg_failures={1: True},
        )
        assert result.status == "FAILED_RECONCILED"
        assert result.incident_id is not None

        async with db_executor.db_manager.session() as session:
            row = (await session.execute(
                text("SELECT * FROM incidents WHERE id = :id"),
                {"id": result.incident_id},
            )).mappings().one()

        assert row["failed_symbol"] == "ETH/BTC", (
            f"DB incident has wrong failed_symbol: expected 'ETH/BTC', got '{row['failed_symbol']}'. "
            "This would be wrong if _reconcile_inventory used canonical pair_bc instead of pair_symbols[1]."
        )
        assert row["liquidation_symbol"] == "BTCUSDT", (
            f"DB incident has wrong liquidation_symbol: expected 'BTCUSDT', got '{row['liquidation_symbol']}'. "
            "This would be wrong if _reconcile_inventory used canonical pair_ab instead of pair_symbols[0]."
        )
        assert Decimal(row["liquidation_pnl_usdt"]) == Decimal("-0.500")


# ── Unit tests: reconciliation loss arithmetic ────────────────────────────────

class TestReconciliationArithmetic:
    """Verify reconciliation loss calculations against hand-verified arithmetic.

    Uses real_pipeline pair_symbols to avoid canonical-order assumptions.
    """

    @pytest.mark.asyncio
    async def test_leg1_failure_loss_arithmetic(self, executor, real_pipeline):
        """Leg 1 failure loss arithmetic.

        Hand-verified:
            position_usdt            = 100 USDT
            liquidation_pnl_usdt     = -100 × 0.005 = -0.500 USDT
            actual_net_return        = 1.0 + (-0.500 / 100) = 0.995 exactly
        """
        triangle, _, _, best = real_pipeline

        result = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            simulated_leg_failures={1: True},
        )
        assert result.status == "FAILED_RECONCILED"
        assert result.legs_filled == 1
        assert result.actual_net_return == Decimal("0.995"), (
            f"Expected 0.995 (1.0 - 0.5%), got {result.actual_net_return}"
        )
        assert len(executor.risk_manager._incident_timestamps_ms) == 1

    @pytest.mark.asyncio
    async def test_leg2_failure_loss_arithmetic(self, executor, real_pipeline):
        """Leg 2 failure loss arithmetic.

        Hand-verified:
            position_usdt            = 100 USDT
            liquidation_pnl_usdt     = -100 × 0.01 = -1.000 USDT
            actual_net_return        = 1.0 + (-1.000 / 100) = 0.990 exactly
        """
        triangle, _, _, best = real_pipeline

        result = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            simulated_leg_failures={2: True},
        )
        assert result.status == "FAILED_RECONCILED"
        assert result.legs_filled == 2
        assert result.actual_net_return == Decimal("0.990"), (
            f"Expected 0.990 (1.0 - 1%), got {result.actual_net_return}"
        )

    @pytest.mark.asyncio
    async def test_leg0_failure_no_inventory_no_incident(self, executor, real_pipeline):
        """Leg 0 failure: no fill, no unhedged inventory, no incident recorded."""
        triangle, _, _, best = real_pipeline

        result = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("50"),
            expected_net_return=best.net_return,
            simulated_leg_failures={0: True},
        )
        assert result.status == "FAILED_LEG_0"
        assert result.legs_filled == 0
        assert result.actual_net_return == Decimal("1.0")
        assert executor.risk_manager.daily_pnl_usdt == Decimal("0")
        assert executor.risk_manager._incident_timestamps_ms == []


# ── Unit tests: circuit breaker and risk gate ─────────────────────────────────

class TestCircuitBreakerAndRiskGate:
    """Circuit breaker and risk manager pre-execution gate tests."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_triggers_after_3_incidents(
        self, executor, real_pipeline
    ):
        """3 consecutive Leg 1 reconciliation incidents trigger circuit breaker auto-pause."""
        triangle, _, _, best = real_pipeline

        for i in range(2):
            r = await executor.execute_triangle(
                triangle=triangle,
                pair_symbols=best.pair_symbols,
                position_usdt=Decimal("100"),
                expected_net_return=best.net_return,
                simulated_leg_failures={1: True},
            )
            assert r.status == "FAILED_RECONCILED"
            assert executor.risk_manager.is_paused is False, f"Paused after {i+1} incidents"

        # 3rd incident triggers circuit breaker
        r3 = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            simulated_leg_failures={1: True},
        )
        assert r3.status == "FAILED_RECONCILED"
        assert executor.risk_manager.is_paused is True
        assert "Circuit breaker triggered" in executor.risk_manager.pause_reason

        # 4th attempt is blocked at risk gate — no further reconciliation occurs
        r4 = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
        )
        assert r4.status == "REJECTED_RISK"
        assert "paused" in r4.error_message.lower()

    @pytest.mark.asyncio
    async def test_oversized_position_rejected_before_any_leg(
        self, executor, real_pipeline
    ):
        """Position exceeding MAX_POSITION_USDT is rejected without dispatching any leg."""
        triangle, _, _, best = real_pipeline

        result = await executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("500"),  # exceeds MAX_POSITION_USDT=100
            expected_net_return=best.net_return,
        )
        assert result.status == "REJECTED_RISK"
        assert result.legs_filled == 0
        assert "exceeds cap" in result.error_message


class TestIncidentAlerting:
    """Tests for incident and circuit-breaker alert callback semantics."""

    @pytest.mark.asyncio
    async def test_incident_alert_fires_once_per_reconciliation(self, risk_manager, settings_dry_run, real_pipeline):
        """A reconciliation incident sends one incident alert message."""
        alert_sender = AsyncMock()
        ex = Executor(
            adapter=MagicMock(),
            risk_manager=risk_manager,
            db_manager=None,
            settings=settings_dry_run,
            incident_alert_sender=alert_sender,
        )
        triangle, _, _, best = real_pipeline

        result = await ex.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            simulated_leg_failures={1: True},
        )

        assert result.status == "FAILED_RECONCILED"
        assert alert_sender.await_count == 1
        sent_message = alert_sender.await_args_list[0].args[0]
        assert "Reconciliation incident recorded" in sent_message

    @pytest.mark.asyncio
    async def test_circuit_breaker_alert_fires_once_on_trip(self, risk_manager, settings_dry_run, real_pipeline):
        """Circuit-breaker alert fires exactly once when the threshold is crossed."""
        alert_sender = AsyncMock()
        ex = Executor(
            adapter=MagicMock(),
            risk_manager=risk_manager,
            db_manager=None,
            settings=settings_dry_run,
            incident_alert_sender=alert_sender,
        )
        triangle, _, _, best = real_pipeline

        for _ in range(3):
            await ex.execute_triangle(
                triangle=triangle,
                pair_symbols=best.pair_symbols,
                position_usdt=Decimal("100"),
                expected_net_return=best.net_return,
                simulated_leg_failures={1: True},
            )

        # 3 incident alerts + 1 breaker alert
        assert alert_sender.await_count == 4
        breaker_alerts = [
            call.args[0] for call in alert_sender.await_args_list if "Circuit breaker tripped" in call.args[0]
        ]
        assert len(breaker_alerts) == 1

    @pytest.mark.asyncio
    async def test_alert_send_failure_does_not_break_reconciliation(self, risk_manager, settings_dry_run, real_pipeline):
        """Alert sender exceptions are swallowed; reconciliation still completes."""
        alert_sender = AsyncMock(side_effect=RuntimeError("telegram send failure"))
        ex = Executor(
            adapter=MagicMock(),
            risk_manager=risk_manager,
            db_manager=None,
            settings=settings_dry_run,
            incident_alert_sender=alert_sender,
        )
        triangle, _, _, best = real_pipeline

        result = await ex.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            simulated_leg_failures={1: True},
        )

        assert result.status == "FAILED_RECONCILED"
        assert result.legs_filled == 1


class TestLiveExecutionWithMarketReconciliation:
    """Live-path tests that exercise parallel FOK dispatch and market liquidation."""

    @pytest.mark.asyncio
    async def test_live_success_uses_parallel_fok_orders(self, live_executor, real_pipeline):
        triangle, tickers, _, best = real_pipeline

        live_executor.adapter.place_fok_order.side_effect = [
            OrderResult("BTCUSDT", "1", "FILLED", Decimal("0.002"), Decimal("50000"), Decimal("0"), "", {"status": "FILLED"}),
            OrderResult("ETH/BTC", "2", "FILLED", Decimal("0.04"), Decimal("0.05"), Decimal("0"), "", {"status": "FILLED"}),
            OrderResult("ETHUSDT", "3", "FILLED", Decimal("0.04"), Decimal("2530"), Decimal("0"), "", {"status": "FILLED"}),
        ]

        result = await live_executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            path=best.path,
            tickers=tickers,
        )

        assert result.status == "COMPLETED"
        assert result.legs_filled == 3
        assert live_executor.adapter.place_fok_order.await_count == 3
        live_executor.adapter.place_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_leg1_failure_triggers_market_reconciliation(self, live_executor, real_pipeline):
        triangle, tickers, _, best = real_pipeline

        live_executor.adapter.place_fok_order.side_effect = [
            OrderResult("BTCUSDT", "1", "FILLED", Decimal("0.002"), Decimal("50000"), Decimal("0"), "", {"status": "FILLED"}),
            OrderResult("ETH/BTC", "2", "EXPIRED", Decimal("0"), Decimal("0"), Decimal("0"), "", {"status": "EXPIRED"}),
            OrderResult("ETHUSDT", "3", "EXPIRED", Decimal("0"), Decimal("0"), Decimal("0"), "", {"status": "EXPIRED"}),
        ]
        live_executor.adapter.place_market_order.return_value = OrderResult(
            "BTCUSDT",
            "liq-1",
            "FILLED",
            Decimal("0.002"),
            Decimal("49920"),
            Decimal("0"),
            "",
            {"status": "FILLED"},
        )

        result = await live_executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            path=best.path,
            tickers=tickers,
        )

        assert result.status == "FAILED_RECONCILED"
        assert result.legs_filled == 1
        live_executor.adapter.place_market_order.assert_awaited_once()
        market_kwargs = live_executor.adapter.place_market_order.await_args.kwargs
        assert market_kwargs["symbol"] == "BTCUSDT"
        assert market_kwargs["side"] == "SELL"
        assert market_kwargs["quantity"] == Decimal("0.002")

    @pytest.mark.asyncio
    async def test_live_leg2_failure_triggers_market_reconciliation(self, live_executor, real_pipeline):
        triangle, tickers, _, best = real_pipeline

        live_executor.adapter.place_fok_order.side_effect = [
            OrderResult("BTCUSDT", "1", "FILLED", Decimal("0.002"), Decimal("50000"), Decimal("0"), "", {"status": "FILLED"}),
            OrderResult("ETH/BTC", "2", "FILLED", Decimal("0.04"), Decimal("0.05"), Decimal("0"), "", {"status": "FILLED"}),
            OrderResult("ETHUSDT", "3", "EXPIRED", Decimal("0"), Decimal("0"), Decimal("0"), "", {"status": "EXPIRED"}),
        ]
        live_executor.adapter.place_market_order.return_value = OrderResult(
            "ETH/BTC",
            "liq-2",
            "FILLED",
            Decimal("0.04"),
            Decimal("0.0497"),
            Decimal("0"),
            "",
            {"status": "FILLED"},
        )

        result = await live_executor.execute_triangle(
            triangle=triangle,
            pair_symbols=best.pair_symbols,
            position_usdt=Decimal("100"),
            expected_net_return=best.net_return,
            path=best.path,
            tickers=tickers,
        )

        assert result.status == "FAILED_RECONCILED"
        assert result.legs_filled == 2
        live_executor.adapter.place_market_order.assert_awaited_once()
        market_kwargs = live_executor.adapter.place_market_order.await_args.kwargs
        assert market_kwargs["symbol"] == "ETH/BTC"
        assert market_kwargs["side"] == "SELL"
        assert market_kwargs["quantity"] == Decimal("0.04")
