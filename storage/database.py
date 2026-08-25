"""Async SQLite database manager for DeltaQuant persistence.

Configures SQLAlchemy async engine with SQLite Write-Ahead Logging (WAL) mode
and robust PRAGMA settings for high concurrency and zero lock contention during
real-time evaluation and incident logging.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from storage.models import Base


def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Set load-bearing SQLite PRAGMAs on every new raw DBAPI connection.

    - journal_mode=WAL: Enables concurrent readers and single writer without blocking.
    - synchronous=NORMAL: Safe durability with significantly reduced disk sync I/O.
    - foreign_keys=ON: Enforces relational constraints.
    - busy_timeout=5000: Waits up to 5s for locks before raising OperationalError.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()


class DatabaseManager:
    """Manages async SQLite connections, schema initialization, and session lifecycle.

    Attributes:
        database_url: Fully qualified SQLAlchemy async connection string
            (e.g. ``"sqlite+aiosqlite:///data/deltaquant.db"`` or ``"sqlite+aiosqlite:///:memory:"``).
        engine: The underlying SQLAlchemy ``AsyncEngine`` instance.
        session_factory: The ``async_sessionmaker`` for spawning ``AsyncSession`` objects.
    """

    def __init__(self, database_url: str = "sqlite+aiosqlite:///deltaquant.db") -> None:
        """Initialize the DatabaseManager with an async SQLite engine.

        Args:
            database_url: SQLAlchemy async connection URI.
        """
        self.database_url = database_url
        self.engine: AsyncEngine = create_async_engine(
            self.database_url,
            echo=False,
            future=True,
        )

        # Attach PRAGMA listener to raw DBAPI connections
        event.listen(self.engine.sync_engine, "connect", _set_sqlite_pragmas)

        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def init_db(self) -> None:
        """Create all tables in the database schema if they do not exist."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Provide an async transactional database session context.

        Yields:
            An active ``AsyncSession``.

        Rolls back automatically if an unhandled exception occurs inside the block.
        """
        session: AsyncSession = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def close(self) -> None:
        """Dispose of the engine connection pool cleanly."""
        await self.engine.dispose()
