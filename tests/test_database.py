"""Unit and integration tests for storage/database.py and storage/models.py.

Covers:
    - DatabaseManager initialization and table creation (init_db)
    - SQLite PRAGMA configuration verification (journal_mode=wal, busy_timeout=5000)
    - Trade model CRUD operations and Decimal string accuracy
    - Incident model CRUD operations and reconciliation failure logging
    - Metric model CRUD operations
    - Transaction rollback behavior on error

Probing principle: Tests verify exact field types, PRAGMA settings, and error handling.
"""

from decimal import Decimal
import pytest
from sqlalchemy import text

from storage.database import DatabaseManager
from storage.models import Incident, Metric, Trade


@pytest.fixture
async def db_manager(tmp_path):
    """Provide a DatabaseManager configured with a temporary SQLite file."""
    db_file = tmp_path / "test_deltaquant.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    manager = DatabaseManager(database_url=url)
    await manager.init_db()
    yield manager
    await manager.close()


class TestDatabaseManager:
    """Tests for database lifecycle, PRAGMAs, and session management."""

    @pytest.mark.asyncio
    async def test_pragmas_configured_correctly(self, db_manager):
        """Verify that WAL mode and busy_timeout PRAGMAs are executed on connection."""
        async with db_manager.session() as session:
            res_wal = await session.execute(text("PRAGMA journal_mode;"))
            journal_mode = res_wal.scalar()
            assert journal_mode.lower() == "wal"

            res_timeout = await session.execute(text("PRAGMA busy_timeout;"))
            timeout = res_timeout.scalar()
            assert timeout == 5000

            res_fk = await session.execute(text("PRAGMA foreign_keys;"))
            foreign_keys = res_fk.scalar()
            assert foreign_keys == 1

    @pytest.mark.asyncio
    async def test_session_rollback_on_exception(self, db_manager):
        """Verify that unhandled exceptions inside session context trigger automatic rollback."""
        with pytest.raises(RuntimeError, match="Simulated failure"):
            async with db_manager.session() as session:
                trade = Trade(
                    triangle_id="tri_rollback_test",
                    path_str="USDT->BTC->ETH->USDT",
                    expected_net_return="1.002500",
                    actual_net_return="1.002100",
                    status="SIMULATED",
                    execution_duration_ms=45,
                    timestamp_ms=1700000000000,
                )
                session.add(trade)
                raise RuntimeError("Simulated failure")

        # Confirm trade was not persisted
        async with db_manager.session() as session:
            res = await session.execute(
                text("SELECT count(*) FROM trades WHERE triangle_id='tri_rollback_test'")
            )
            count = res.scalar()
            assert count == 0


class TestModelsCRUD:
    """CRUD tests for Trade, Incident, and Metric ORM models."""

    @pytest.mark.asyncio
    async def test_trade_crud_and_decimal_precision(self, db_manager):
        """Create and read a Trade record, verifying Decimal string accuracy."""
        expected_return = Decimal("1.001500000000000000")
        actual_return = Decimal("1.001250000000000000")

        async with db_manager.session() as session:
            trade = Trade(
                triangle_id="USDT-BTC-ETH",
                path_str="USDT->BTC->ETH->USDT",
                expected_net_return=str(expected_return),
                actual_net_return=str(actual_return),
                status="COMPLETED",
                execution_duration_ms=38,
                timestamp_ms=1700000000100,
            )
            session.add(trade)

        async with db_manager.session() as session:
            res = await session.execute(
                text("SELECT * FROM trades WHERE triangle_id='USDT-BTC-ETH'")
            )
            row = res.mappings().one()
            assert row["status"] == "COMPLETED"
            assert Decimal(row["expected_net_return"]) == expected_return
            assert Decimal(row["actual_net_return"]) == actual_return
            assert row["execution_duration_ms"] == 38

    @pytest.mark.asyncio
    async def test_incident_crud(self, db_manager):
        """Create and read an Incident record representing a partial leg failure."""
        async with db_manager.session() as session:
            incident = Incident(
                triangle_id="USDT-ETH-BTC",
                failed_leg_index=2,
                failed_symbol="ETH/BTC",
                error_message="FOK order expired without fill",
                liquidation_symbol="ETHUSDT",
                liquidation_amount=str(Decimal("0.50000000")),
                liquidation_pnl_usdt=str(Decimal("-0.12500000")),
                timestamp_ms=1700000000200,
            )
            session.add(incident)

        async with db_manager.session() as session:
            res = await session.execute(
                text("SELECT * FROM incidents WHERE triangle_id='USDT-ETH-BTC'")
            )
            row = res.mappings().one()
            assert row["failed_leg_index"] == 2
            assert row["failed_symbol"] == "ETH/BTC"
            assert Decimal(row["liquidation_pnl_usdt"]) == Decimal("-0.12500000")

    @pytest.mark.asyncio
    async def test_metric_crud(self, db_manager):
        """Create and query Metric operational snapshots."""
        async with db_manager.session() as session:
            metric = Metric(
                metric_name="latency_eval_to_order_ms",
                metric_value=42.5,
                tags_json='{"exchange": "binance", "pair": "BTCUSDT"}',
                timestamp_ms=1700000000300,
            )
            session.add(metric)

        async with db_manager.session() as session:
            res = await session.execute(
                text("SELECT * FROM metrics WHERE metric_name='latency_eval_to_order_ms'")
            )
            row = res.mappings().one()
            assert row["metric_value"] == 42.5
            assert "binance" in row["tags_json"]
