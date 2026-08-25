"""Risk management and circuit breaker engine for DeltaQuant.

Enforces capital, loss, concurrency, and reconciliation incident bounds:
    - MAX_POSITION_USDT: Hard cap on trade size per triangle execution.
    - DAILY_LOSS_LIMIT_USDT: Auto-pauses bot if cumulative daily PnL hits floor.
    - MAX_CONCURRENT_TRIANGLES: Prevents overexposure during volatile spikes.
    - CIRCUIT_BREAKER: Auto-pauses bot if N partial-execution incidents occur within W minutes.
"""

from decimal import Decimal
import time
from typing import Optional

from loguru import logger

from config.settings import Settings, get_settings


class RiskManager:
    """Central risk controller governing pre-execution checks and circuit breakers.

    Attributes:
        settings: Application settings containing risk thresholds.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        """Initialize RiskManager state.

        Args:
            settings: Optional Settings instance; uses default singleton if omitted.
        """
        self.settings = settings or get_settings()
        self._daily_pnl_usdt: Decimal = Decimal("0")
        self._in_flight_count: int = 0
        self._is_paused: bool = False
        self._pause_reason: Optional[str] = None
        self._incident_timestamps_ms: list[int] = []

    @property
    def is_paused(self) -> bool:
        """Return True if the bot is currently paused by risk limits or circuit breaker."""
        return self._is_paused

    @property
    def pause_reason(self) -> Optional[str]:
        """Return human-readable explanation of why the bot is paused."""
        return self._pause_reason

    @property
    def daily_pnl_usdt(self) -> Decimal:
        """Return cumulative daily PnL in USDT."""
        return self._daily_pnl_usdt

    @property
    def in_flight_count(self) -> int:
        """Return number of currently executing in-flight triangles."""
        return self._in_flight_count

    def can_execute(
        self, position_usdt: Decimal, current_time_ms: Optional[int] = None
    ) -> tuple[bool, Optional[str]]:
        """Validate whether a triangle execution of the given size is permitted.

        Args:
            position_usdt: Proposed trade size in USDT.
            current_time_ms: Optional current Unix timestamp in ms for testing.

        Returns:
            Tuple of ``(is_permitted, rejection_reason)``.
            If permitted, returns ``(True, None)``.
        """
        now_ms = current_time_ms if current_time_ms is not None else int(time.time() * 1000)

        # 1. Manual or auto pause check
        if self._is_paused:
            return False, f"RiskManager is paused: {self._pause_reason}"

        # 2. Position size cap check
        if position_usdt > self.settings.MAX_POSITION_USDT:
            reason = (
                f"Position size {position_usdt} USDT exceeds cap "
                f"{self.settings.MAX_POSITION_USDT} USDT"
            )
            logger.warning("risk_check_rejected reason='{}'", reason)
            return False, reason

        # 3. Daily loss limit check
        if self._daily_pnl_usdt <= self.settings.DAILY_LOSS_LIMIT_USDT:
            reason = (
                f"Daily loss limit reached: PnL {self._daily_pnl_usdt} USDT <= "
                f"floor {self.settings.DAILY_LOSS_LIMIT_USDT} USDT"
            )
            self.pause(reason)
            return False, reason

        # 4. Concurrency limit check
        if self._in_flight_count >= self.settings.MAX_CONCURRENT_TRIANGLES:
            reason = (
                f"Max concurrent triangles reached "
                f"({self._in_flight_count}/{self.settings.MAX_CONCURRENT_TRIANGLES})"
            )
            logger.warning("risk_check_rejected reason='{}'", reason)
            return False, reason

        # 5. Circuit breaker incident window check
        self._purge_old_incidents(now_ms)
        if len(self._incident_timestamps_ms) >= self.settings.CIRCUIT_BREAKER_INCIDENT_COUNT:
            reason = (
                f"Circuit breaker triggered: {len(self._incident_timestamps_ms)} incidents "
                f"within {self.settings.CIRCUIT_BREAKER_WINDOW_MINUTES}m window"
            )
            self.pause(reason)
            return False, reason

        return True, None

    def register_execution_start(self) -> None:
        """Increment the in-flight triangle execution counter."""
        self._in_flight_count += 1
        logger.debug("risk_execution_start in_flight={}", self._in_flight_count)

    def register_execution_end(self, pnl_usdt: Decimal) -> None:
        """Decrement in-flight counter and update daily cumulative PnL.

        Args:
            pnl_usdt: Net PnL of completed execution in USDT.
        """
        self._in_flight_count = max(0, self._in_flight_count - 1)
        self._daily_pnl_usdt += pnl_usdt
        logger.info(
            "risk_execution_end pnl_usdt={} daily_pnl_usdt={} in_flight={}",
            pnl_usdt,
            self._daily_pnl_usdt,
            self._in_flight_count,
        )

        if self._daily_pnl_usdt <= self.settings.DAILY_LOSS_LIMIT_USDT:
            reason = (
                f"Daily loss limit reached: PnL {self._daily_pnl_usdt} USDT <= "
                f"floor {self.settings.DAILY_LOSS_LIMIT_USDT} USDT"
            )
            self.pause(reason)

    def record_incident(self, timestamp_ms: Optional[int] = None) -> None:
        """Record a partial leg failure / emergency liquidation incident.

        Args:
            timestamp_ms: Unix timestamp in ms. Uses current time if omitted.
        """
        now_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        self._incident_timestamps_ms.append(now_ms)
        self._purge_old_incidents(now_ms)

        logger.warning(
            "risk_incident_recorded count_in_window={} threshold={}",
            len(self._incident_timestamps_ms),
            self.settings.CIRCUIT_BREAKER_INCIDENT_COUNT,
        )

        if len(self._incident_timestamps_ms) >= self.settings.CIRCUIT_BREAKER_INCIDENT_COUNT:
            reason = (
                f"Circuit breaker triggered: {len(self._incident_timestamps_ms)} incidents "
                f"within {self.settings.CIRCUIT_BREAKER_WINDOW_MINUTES}m window"
            )
            self.pause(reason)

    def pause(self, reason: str) -> None:
        """Pause all trading operations.

        Args:
            reason: Explanation of pause trigger.
        """
        self._is_paused = True
        self._pause_reason = reason
        logger.error("risk_manager_paused reason='{}'", reason)

    def resume(self) -> None:
        """Resume trading operations after manual intervention."""
        self._is_paused = False
        self._pause_reason = None
        logger.info("risk_manager_resumed")

    def reset_daily_pnl(self) -> None:
        """Reset daily cumulative PnL to 0.0 USDT (used at UTC day rollover)."""
        self._daily_pnl_usdt = Decimal("0")
        logger.info("risk_daily_pnl_reset")

    def get_status(self) -> dict:
        """Return diagnostic snapshot dictionary of risk engine state."""
        return {
            "is_paused": self._is_paused,
            "pause_reason": self._pause_reason,
            "daily_pnl_usdt": str(self._daily_pnl_usdt),
            "in_flight_count": self._in_flight_count,
            "recent_incident_count": len(self._incident_timestamps_ms),
            "max_position_usdt": str(self.settings.MAX_POSITION_USDT),
            "daily_loss_limit_usdt": str(self.settings.DAILY_LOSS_LIMIT_USDT),
            "max_concurrent_triangles": self.settings.MAX_CONCURRENT_TRIANGLES,
            "circuit_breaker_incident_count": self.settings.CIRCUIT_BREAKER_INCIDENT_COUNT,
            "circuit_breaker_window_minutes": self.settings.CIRCUIT_BREAKER_WINDOW_MINUTES,
        }

    def _purge_old_incidents(self, current_time_ms: int) -> None:
        """Remove incident timestamps older than the circuit breaker window."""
        window_ms = self.settings.CIRCUIT_BREAKER_WINDOW_MINUTES * 60 * 1000
        cutoff_ms = current_time_ms - window_ms
        self._incident_timestamps_ms = [
            ts for ts in self._incident_timestamps_ms if ts >= cutoff_ms
        ]
