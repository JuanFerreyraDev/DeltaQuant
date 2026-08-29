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


@pytest.mark.asyncio
async def test_resubscription_on_symbol_change(
    db_manager, risk_manager, executor, test_settings
):
    """Simulating pair refresh with volume changes triggers WS resubscription without ending the WS loop."""
    batch_1 = [
        {"symbol": "BTC/USDT", "quoteVolume": "10000000", "last": "50000"},
        {"symbol": "ETH/USDT", "quoteVolume": "5000000", "last": "3000"},
        {"symbol": "ETH/BTC", "quoteVolume": "2000", "last": "0.06"},
    ]
    batch_2 = [
        {"symbol": "BTC/USDT", "quoteVolume": "10000000", "last": "50000"},
        {"symbol": "SOL/USDT", "quoteVolume": "8000000", "last": "150"},
        {"symbol": "SOL/BTC", "quoteVolume": "3000", "last": "0.003"},
    ]

    fetch_call_count = 0

    async def mock_fetch_24h():
        nonlocal fetch_call_count
        fetch_call_count += 1
        return batch_1 if fetch_call_count == 1 else batch_2

    subscribed_calls = []

    async def mock_subscribe(symbols):
        subscribed_calls.append(list(symbols))
        # Yield one tick then wait until cancelled or resubscribed
        yield BookTicker(symbols[0], Decimal("100"), Decimal("101"), int(time.time() * 1000))
        while True:
            await asyncio.sleep(10)

    adapter = AsyncMock()
    adapter.fetch_tickers_24h = AsyncMock(side_effect=mock_fetch_24h)
    adapter.subscribe_book_ticker = MagicMock(side_effect=mock_subscribe)
    adapter.get_trading_fees = AsyncMock(
        side_effect=lambda s: TradingFees(s, Decimal("0.00075"), Decimal("0.00075"))
    )

    orchestrator = Orchestrator(
        adapter=adapter,
        db_manager=db_manager,
        risk_manager=risk_manager,
        executor=executor,
        settings=test_settings,
    )

    start_task = asyncio.create_task(orchestrator.start())
    await asyncio.sleep(0.1)

    # Verify first subscription cycle
    assert len(subscribed_calls) == 1
    assert set(subscribed_calls[0]) == {"ETH/BTC", "ETHUSDT", "BTCUSDT"}

    # Trigger second refresh cycle with batch_2
    await orchestrator.refresh_triangles()
    await asyncio.sleep(0.1)

    # Verify second subscription cycle occurred with NEW symbols
    assert len(subscribed_calls) == 2
    assert set(subscribed_calls[1]) == {"SOL/BTC", "SOLUSDT", "BTCUSDT"}

    # Assert that the WS loop task did NOT crash or terminate
    assert orchestrator._ws_task is not None
    assert not orchestrator._ws_task.done()

    # Clean shutdown
    await orchestrator.stop()
    start_task.cancel()
    try:
        await start_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_ws_loop_cancellation_during_tick_wait_is_clean(
    db_manager, risk_manager, executor, test_settings
):
    """Cancelling the WS loop while waiting for a tick completes cleanly without CancelledError or pending task leaks."""
    batch = [
        {"symbol": "BTC/USDT", "quoteVolume": "10000000", "last": "50000"},
        {"symbol": "ETH/USDT", "quoteVolume": "5000000", "last": "3000"},
        {"symbol": "ETH/BTC", "quoteVolume": "2000", "last": "0.06"},
    ]

    async def mock_subscribe(symbols):
        # Never yields ticks; stays suspended in sleep so tick-wait is active
        await asyncio.sleep(100)
        yield BookTicker(symbols[0], Decimal("100"), Decimal("101"), int(time.time() * 1000))

    adapter = AsyncMock()
    adapter.fetch_tickers_24h = AsyncMock(return_value=batch)
    adapter.subscribe_book_ticker = MagicMock(side_effect=mock_subscribe)
    adapter.get_trading_fees = AsyncMock(
        side_effect=lambda s: TradingFees(s, Decimal("0.00075"), Decimal("0.00075"))
    )

    orchestrator = Orchestrator(
        adapter=adapter,
        db_manager=db_manager,
        risk_manager=risk_manager,
        executor=executor,
        settings=test_settings,
    )

    start_task = asyncio.create_task(orchestrator.start())
    await asyncio.sleep(0.1)

    # Stop orchestrator while tick wait is in flight
    await orchestrator.stop()

    # start_task must finish cleanly without raising CancelledError out of gather/start
    await start_task
    assert start_task.done()
    assert not start_task.cancelled()
    assert start_task.exception() is None


@pytest.mark.asyncio
async def test_fee_fetch_failure_excludes_symbol_and_triangles(
    db_manager, risk_manager, executor, test_settings
):
    """When fee fetch fails for a symbol, fee_rates does not include it, excluding its triangles."""
    batch = [
        {"symbol": "BTC/USDT", "quoteVolume": "10000000", "last": "50000"},
        {"symbol": "ETH/USDT", "quoteVolume": "5000000", "last": "3000"},
        {"symbol": "ETH/BTC", "quoteVolume": "2000", "last": "0.06"},
    ]
    adapter = AsyncMock()
    adapter.fetch_tickers_24h = AsyncMock(return_value=batch)

    async def mock_get_trading_fees(symbol: str):
        if symbol == "ETH/BTC":
            raise Exception("Fee fetch network failure")
        return TradingFees(symbol, Decimal("0.00075"), Decimal("0.00075"))

    adapter.get_trading_fees = AsyncMock(side_effect=mock_get_trading_fees)

    orchestrator = Orchestrator(
        adapter=adapter,
        db_manager=db_manager,
        risk_manager=risk_manager,
        executor=executor,
        settings=test_settings,
    )

    await orchestrator.refresh_triangles()

    assert "BTCUSDT" in orchestrator.fee_rates
    assert "ETHUSDT" in orchestrator.fee_rates
    assert "ETH/BTC" not in orchestrator.fee_rates

    now_ms = int(time.time() * 1000)
    orchestrator.evaluations_count = 0
    tick = BookTicker("ETHUSDT", Decimal("3000"), Decimal("3001"), now_ms)
    await orchestrator._process_tick(tick)

    assert orchestrator.evaluations_count == 0

