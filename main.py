"""DeltaQuant — triangular arbitrage bot main orchestration entry point.

Wires together the exchange adapter, database manager, risk manager, triangle graph generator,
evaluator, and execution engine into a continuous async event loop.
"""

import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set

from loguru import logger

from config.settings import Settings, get_settings
from core.evaluator import evaluate_triangle
from core.executor import Executor
from core.graph import Triangle, filter_pairs_by_volume, generate_triangles
from core.risk import RiskManager
from exchanges.base import BookTicker, ExchangeAdapter, TradingFees
from exchanges.binance_adapter import BinanceAdapter
from exchanges.fees import get_effective_fees
from storage.database import DatabaseManager
from storage.models import Metric


class InterceptHandler(logging.Handler):
    """Bridge standard library logging calls to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(log_level: str = "INFO", log_file: str = "logs/deltaquant.log") -> None:
    """Configure loguru and standard library logging handlers."""
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logger.remove()
    logger.add(sys.stderr, level=log_level)
    logger.add(
        log_file,
        level=log_level,
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        enqueue=True,
    )


class Orchestrator:
    """Coordinates periodic pair refresh, market data streaming, evaluation, and execution."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        db_manager: DatabaseManager,
        risk_manager: RiskManager,
        executor: Executor,
        settings: Settings,
    ) -> None:
        """Initialize the Orchestrator.

        Args:
            adapter: Exchange adapter instance.
            db_manager: Persistence manager.
            risk_manager: Risk control manager.
            executor: Triangular execution manager.
            settings: Loaded configuration parameters.
        """
        self.adapter = adapter
        self.db_manager = db_manager
        self.risk_manager = risk_manager
        self.executor = executor
        self.settings = settings

        self.cached_tickers: Dict[str, BookTicker] = {}
        self.fee_rates: Dict[str, TradingFees] = {}
        self.active_triangles: List[Triangle] = []
        self.symbol_to_triangles: Dict[str, List[Triangle]] = defaultdict(list)
        self.subscribed_symbols: Set[str] = set()

        self.evaluations_count: int = 0
        self.profitable_signals_count: int = 0
        self.executions_count: int = 0

        self._running: bool = False
        self._ws_task: Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._metrics_task: Optional[asyncio.Task] = None

    async def refresh_triangles(self) -> None:
        """Fetch 24h tickers, filter by volume, generate triangles, and update fee cache."""
        logger.info("pair_refresh_started min_volume_usdt={}", self.settings.MIN_VOLUME_USDT)
        raw_tickers = await self.adapter.fetch_tickers_24h()
        pairs = filter_pairs_by_volume(raw_tickers, self.settings.MIN_VOLUME_USDT)
        triangles = generate_triangles(pairs)

        # Pre-fetch fee rates for all symbols present in filtered pairs
        for pair in pairs:
            if pair.symbol not in self.fee_rates:
                try:
                    fees = await get_effective_fees(self.adapter, pair.symbol)
                    self.fee_rates[pair.symbol] = fees
                except Exception as exc:
                    logger.warning("fee_fetch_failed symbol={} err={}", pair.symbol, exc)

        new_symbol_to_triangles: Dict[str, List[Triangle]] = defaultdict(list)
        new_symbols: Set[str] = set()

        for t in triangles:
            for pair_symbol in (t.pair_ab, t.pair_bc, t.pair_ca):
                new_symbol_to_triangles[pair_symbol].append(t)
                new_symbols.add(pair_symbol)

        self.active_triangles = triangles
        self.symbol_to_triangles = new_symbol_to_triangles
        old_subscribed = set(self.subscribed_symbols)
        self.subscribed_symbols = new_symbols

        logger.info(
            "pair_refresh_completed pairs={} triangles={} symbols={}",
            len(pairs),
            len(triangles),
            len(new_symbols),
        )

        if self._running and old_subscribed != new_symbols and self._ws_task:
            logger.info("symbols_changed_restarting_ws_stream")
            self._ws_task.cancel()

    async def _periodic_refresh_loop(self) -> None:
        """Periodically trigger volume/triangle refresh."""
        while self._running:
            try:
                await asyncio.sleep(self.settings.PAIR_REFRESH_INTERVAL_SECONDS)
                await self.refresh_triangles()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("periodic_refresh_error err={}", exc)

    async def _periodic_metrics_loop(self) -> None:
        """Periodically persist metric telemetry rows into SQLite."""
        while self._running:
            try:
                await asyncio.sleep(self.settings.METRICS_PERSIST_INTERVAL_SECONDS)
                now_ms = int(time.time() * 1000)
                risk_status = self.risk_manager.get_status()

                async with self.db_manager.session() as session:
                    session.add_all([
                        Metric(
                            metric_name="evaluations_count",
                            metric_value=float(self.evaluations_count),
                            timestamp_ms=now_ms,
                        ),
                        Metric(
                            metric_name="profitable_signals_count",
                            metric_value=float(self.profitable_signals_count),
                            timestamp_ms=now_ms,
                        ),
                        Metric(
                            metric_name="executions_count",
                            metric_value=float(self.executions_count),
                            timestamp_ms=now_ms,
                        ),
                        Metric(
                            metric_name="risk_is_paused",
                            metric_value=1.0 if risk_status["is_paused"] else 0.0,
                            tags_json=json.dumps(risk_status),
                            timestamp_ms=now_ms,
                        ),
                        Metric(
                            metric_name="heartbeat",
                            metric_value=1.0,
                            timestamp_ms=now_ms,
                        ),
                    ])

                logger.info(
                    "metrics_persisted evaluations={} profitable={} executions={} risk_paused={}",
                    self.evaluations_count,
                    self.profitable_signals_count,
                    self.executions_count,
                    risk_status["is_paused"],
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("metrics_persist_error err={}", exc)

    async def _ws_ticker_loop(self) -> None:
        """Stream book tickers and run real-time triangle evaluations."""
        while self._running:
            symbols = list(self.subscribed_symbols)
            if not symbols:
                logger.warning("no_symbols_subscribed_waiting_refresh")
                await asyncio.sleep(5)
                continue

            try:
                async for tick in self.adapter.subscribe_book_ticker(symbols):
                    if not self._running:
                        break

                    self.cached_tickers[tick.symbol] = tick
                    triangles = self.symbol_to_triangles.get(tick.symbol, [])
                    now_ms = int(time.time() * 1000)

                    for triangle in triangles:
                        p_ab, p_bc, p_ca = triangle.pair_ab, triangle.pair_bc, triangle.pair_ca
                        if (
                            p_ab not in self.cached_tickers
                            or p_bc not in self.cached_tickers
                            or p_ca not in self.cached_tickers
                        ):
                            continue
                        if (
                            p_ab not in self.fee_rates
                            or p_bc not in self.fee_rates
                            or p_ca not in self.fee_rates
                        ):
                            continue

                        results = evaluate_triangle(
                            triangle=triangle,
                            tickers=self.cached_tickers,
                            fee_rates=self.fee_rates,
                            safety_margin=self.settings.SAFETY_MARGIN,
                            current_time_ms=now_ms,
                            max_tick_age_ms=self.settings.MAX_TICK_AGE_MS,
                        )
                        self.evaluations_count += 2

                        for res in results:
                            if res.is_profitable:
                                self.profitable_signals_count += 1
                                logger.info(
                                    "profitable_signal_detected triangle={} net_return={} max_age_ms={}",
                                    res.triangle,
                                    res.net_return,
                                    res.max_age_ms,
                                )
                                can_exec, reason = self.risk_manager.can_execute(
                                    self.settings.MAX_POSITION_USDT, current_time_ms=now_ms
                                )
                                if can_exec:
                                    self.executions_count += 1
                                    logger.info(
                                        "executing_triangle triangle={} path={}",
                                        res.triangle,
                                        res.path,
                                    )
                                    await self.executor.execute_triangle(
                                        triangle=triangle,
                                        pair_symbols=res.pair_symbols,
                                        position_usdt=self.settings.MAX_POSITION_USDT,
                                        expected_net_return=res.net_return,
                                    )
                                else:
                                    logger.info(
                                        "execution_blocked_by_risk reason='{}'", reason
                                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ws_ticker_loop_error err={}", exc)
                if not self._running:
                    break
                await asyncio.sleep(2)

    async def start(self) -> None:
        """Start orchestrator tasks and run main event loop."""
        self._running = True
        await self.refresh_triangles()

        self._refresh_task = asyncio.create_task(self._periodic_refresh_loop())
        self._metrics_task = asyncio.create_task(self._periodic_metrics_loop())

        try:
            await self._ws_ticker_loop()
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop all background tasks cleanly."""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
        if self._metrics_task:
            self._metrics_task.cancel()
        if self._ws_task:
            self._ws_task.cancel()


async def async_main() -> None:
    """Async main entry point: initialize components and start orchestrator."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, "logs/deltaquant.log")
    logger.info("deltaquant_starting dry_run={}", settings.DRY_RUN)

    db_manager = DatabaseManager()
    await db_manager.init_db()

    adapter = await BinanceAdapter.create(settings)
    risk_manager = RiskManager(settings=settings)
    executor = Executor(
        adapter=adapter,
        risk_manager=risk_manager,
        db_manager=db_manager,
        settings=settings,
    )

    orchestrator = Orchestrator(
        adapter=adapter,
        db_manager=db_manager,
        risk_manager=risk_manager,
        executor=executor,
        settings=settings,
    )

    try:
        await orchestrator.start()
    except Exception as exc:
        logger.error("main_loop_fatal_crash error='{}'", exc, exc_info=True)
        raise
    finally:
        await db_manager.close()


def main() -> None:
    """Synchronous entry point."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
