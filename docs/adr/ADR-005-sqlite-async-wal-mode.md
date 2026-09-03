# ADR-005: Async SQLite with WAL Mode for Local Persistence

## Status
Accepted

## Context

DeltaQuant requires local persistence for three distinct operational domains:
1. **Trade records**: Logging completed, failed, or simulated triangular arbitrage executions.
2. **Incident tracking**: Logging partial leg execution failures and emergency inventory liquidations (crucial for reconciliation and circuit breaker triggers).
3. **Operational metrics**: Recording tick-to-execution latency distributions and fill rates.

While real-time tick evaluation runs 100 % in memory (RAM), database writes occur whenever trades execute, reconciliation incidents occur, or telemetry samples are flushed.

Under default SQLite configuration (journaling mode `DELETE` or `TRUNCATE`), writing to the database places an exclusive lock on the entire database file, causing `sqlite3.OperationalError: database is locked` when async background tasks attempt concurrent read/write operations.

External client-server databases (e.g. PostgreSQL, MySQL) add deployment complexity, container overhead, and network latency that are unnecessary for a single-process bot running on a single host.

## Decision

Use **SQLite with `aiosqlite` and SQLAlchemy 2.0 async engine**, configured with **Write-Ahead Logging (WAL) mode**.

Every new database connection executes the following PRAGMA statements:
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

- `journal_mode=WAL`: Writes append to a `.db-wal` log file. Readers do not block writers, and writers do not block readers.
- `synchronous=NORMAL`: Reduces disk fsync calls while preserving durability in WAL mode.
- `busy_timeout=5000`: Causes SQLite to wait up to 5,000 milliseconds for locks to clear before throwing an error.

## Implementation

- Encapsulated in `storage/database.py` via `DatabaseManager`.
- ORM models defined in `storage/models.py` (`Trade`, `Incident`, `Metric`).
- Monetary and return fields stored as exact `String` representations to prevent floating-point rounding errors in SQLite.
- Verified in `tests/test_database.py`.

## Consequences

**Positive:**
- Zero external database process or infrastructure dependency.
- Asynchronous non-blocking I/O using `aiosqlite`.
- High concurrency support for simultaneous trade logging and telemetry flushing.
- High durability with minimal performance penalty in WAL mode.

**Negative / Trade-offs:**
- WAL mode creates auxiliary files (`.db-wal`, `.db-shm`) alongside the main database file that must be maintained together.
- Database is local to the host filesystem (not network-accessible), requiring volume persistence in Docker deployments (Phase 5).

## Alternatives Considered

**1. PostgreSQL / MySQL (rejected for Phase 3)**
Adds infrastructure management overhead, port allocation, and network latency without providing benefits for a single-instance bot.

**2. Synchronous SQLite (rejected)**
Blocking SQLite operations in Python's `asyncio` event loop would stall tick evaluation whenever disk I/O occurs.

## References
- **Technical Plan §2, §4, §7**: SQLite async and WAL mode requirements.
- **SQLAlchemy 2.0 Async Documentation**.
