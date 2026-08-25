"""SQLAlchemy ORM models for DeltaQuant persistence.

Defines schemas for:
    - Trade: records completed, failed, or simulated triangular arbitrage executions.
    - Incident: records partial leg execution failures and emergency liquidations.
    - Metric: records operational time-series metrics (latencies, fill rates).

Strict Decimal string representations are used for monetary / return fields to
prevent floating-point precision loss in SQLite storage.
"""

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


class Trade(Base):
    """Record of a triangular arbitrage execution attempt or simulation."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    triangle_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    path_str: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_net_return: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_net_return: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<Trade(id={self.id}, triangle_id='{self.triangle_id}', "
            f"status='{self.status}', actual_net_return='{self.actual_net_return}')>"
        )


class Incident(Base):
    """Record of a partial leg failure requiring emergency inventory liquidation."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    triangle_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    failed_leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str] = mapped_column(String(512), nullable=False)
    liquidation_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    liquidation_amount: Mapped[str] = mapped_column(String(32), nullable=False)
    liquidation_pnl_usdt: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<Incident(id={self.id}, triangle_id='{self.triangle_id}', "
            f"failed_symbol='{self.failed_symbol}', liquidation_pnl_usdt='{self.liquidation_pnl_usdt}')>"
        )


class Metric(Base):
    """Operational metric snapshot for telemetry and performance auditing."""

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_name: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    tags_json: Mapped[str] = mapped_column(String(256), default="{}")
    timestamp_ms: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<Metric(id={self.id}, name='{self.metric_name}', value={self.metric_value})>"
        )
