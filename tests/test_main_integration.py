"""Integration tests for main.py Orchestrator and loop integration."""

import asyncio
from decimal import Decimal
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from config.settings import Settings
from core.executor import Executor
from core.graph import Triangle
from core.risk import RiskManager
from exchanges.base import BookTicker, TradingFees
from main import Orchestrator
from storage.database import DatabaseManager
from storage.models import Metric


@pytest.fixture
def test_settings():
    return Settings(
        BINANCE_API_KEY="real_key_abc123",
        BINANCE_API_SECRET="real_secret_xyz789",
        TELEGRAM_BOT_TOKEN="123456:ABCdef",
        TELEGRAM_CHAT_ID="987654321",
        DRY_RUN=True,
        MIN_VOLUME_USDT=Decimal("1000000"),
        SAFETY_MARGIN=Decimal("0.0010"),
        MAX_TICK_AGE_MS=200,
        PAIR_REFRESH_INTERVAL_SECONDS=1,
        METRICS_PERSIST_INTERVAL_SECONDS=1,
    )


@pytest.fixture
def mock_adapter():
    adapter = AsyncMock()
    adapter.fetch_tickers_24h.return_value = [
        {"symbol": "BTC/USDT", "quoteVolume": "10000000", "last": "50000"},
        {"symbol": "ETH/USDT", "quoteVolume": "5000000", "last": "3000"},
        {"symbol": "ETH/BTC", "quoteVolume": "2000", "last": "0.06"},
    ]
    adapter.get_trading_fees = AsyncMock(
        side_effect=lambda s: TradingFees(s, Decimal("0.00075"), Decimal("0.00075"))
    )
    return adapter


@pytest.fixture
async def db_manager():
    db = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    yield db
    await db.close()


@pytest.fixture
def risk_manager(test_settings):
    return RiskManager(settings=test_settings)


@pytest.fixture
def executor(mock_adapter, risk_manager, db_manager, test_settings):
    return Executor(
        adapter=mock_adapter,
        risk_manager=risk_manager,
        db_manager=db_manager,
        settings=test_settings,
    )


@pytest.mark.asyncio
async def test_refresh_triangles_builds_graph(
    mock_adapter, db_manager, risk_manager, executor, test_settings
):
    """refresh_triangles correctly filters pairs, generates triangles, and fetches fees."""
    orchestrator = Orchestrator(
        adapter=mock_adapter,
        db_manager=db_manager,
        risk_manager=risk_manager,
        executor=executor,
        settings=test_settings,
    )

    await orchestrator.refresh_triangles()

    assert len(orchestrator.active_triangles) == 1
    assert orchestrator.active_triangles[0] == Triangle(
        asset_a="BTC",
        asset_b="ETH",
        asset_c="USDT",
        pair_ab="ETH/BTC",
        pair_bc="ETHUSDT",
        pair_ca="BTCUSDT",
    )
    assert set(orchestrator.subscribed_symbols) == {"ETH/BTC", "ETHUSDT", "BTCUSDT"}
    assert "BTCUSDT" in orchestrator.fee_rates


@pytest.mark.asyncio
async def test_stale_tick_prevents_execution(
    mock_adapter, db_manager, risk_manager, executor, test_settings
):
    """A stale tick (> MAX_TICK_AGE_MS) prevents execution end-to-end through evaluation."""
    orchestrator = Orchestrator(
        adapter=mock_adapter,
        db_manager=db_manager,
        risk_manager=risk_manager,
        executor=executor,
        settings=test_settings,
    )

    await orchestrator.refresh_triangles()

    # Create stale tick for BTCUSDT (300ms old, threshold is 200ms)
    now_ms = int(time.time() * 1000)
    orchestrator.cached_tickers = {
        "BTCUSDT": BookTicker("BTCUSDT", Decimal("50000"), Decimal("50010"), now_ms - 300),
        "ETHUSDT": BookTicker("ETHUSDT", Decimal("3000"), Decimal("3001"), now_ms),
        "ETH/BTC": BookTicker("ETH/BTC", Decimal("0.06"), Decimal("0.0601"), now_ms),
    }

    executor.execute_triangle = AsyncMock()

    # Simulate one iteration of WS ticker loop for a fresh tick on ETHUSDT
    tick = BookTicker("ETHUSDT", Decimal("3000"), Decimal("3001"), now_ms)
    triangles = orchestrator.symbol_to_triangles[tick.symbol]

    from core.evaluator import evaluate_triangle
    results = evaluate_triangle(
        triangle=triangles[0],
        tickers=orchestrator.cached_tickers,
        fee_rates=orchestrator.fee_rates,
        safety_margin=test_settings.SAFETY_MARGIN,
        current_time_ms=now_ms,
        max_tick_age_ms=test_settings.MAX_TICK_AGE_MS,
    )

    # Both paths must be marked as stale and not profitable
    assert all(r.is_stale for r in results)
    assert all(not r.is_profitable for r in results)
    executor.execute_triangle.assert_not_called()


@pytest.mark.asyncio
async def test_metrics_persist_loop_saves_rows(
    mock_adapter, db_manager, risk_manager, executor, test_settings
):
    """_periodic_metrics_loop writes Metric rows to SQLite."""
    orchestrator = Orchestrator(
        adapter=mock_adapter,
        db_manager=db_manager,
        risk_manager=risk_manager,
        executor=executor,
        settings=test_settings,
    )

    orchestrator.evaluations_count = 42
    orchestrator.profitable_signals_count = 3
    orchestrator.executions_count = 1
    orchestrator._running = True

    # Run metrics loop for 1 cycle
    task = asyncio.create_task(orchestrator._periodic_metrics_loop())
    await asyncio.sleep(1.2)
    orchestrator._running = False
    task.cancel()

    async with db_manager.session() as session:
        result = await session.execute(select(Metric))
        metrics = result.scalars().all()

    metric_names = {m.metric_name for m in metrics}
    assert "evaluations_count" in metric_names
    assert "profitable_signals_count" in metric_names
    assert "executions_count" in metric_names
    assert "risk_is_paused" in metric_names
    assert "heartbeat" in metric_names

    eval_metric = next(m for m in metrics if m.metric_name == "evaluations_count")
    assert eval_metric.metric_value == 42.0
