"""Unit tests for core/risk.py RiskManager engine.

Covers:
    - Position size cap rejection (MAX_POSITION_USDT)
    - Daily loss limit auto-pause trigger (DAILY_LOSS_LIMIT_USDT)
    - Max concurrent in-flight triangles limit (MAX_CONCURRENT_TRIANGLES)
    - Reconciliation incident circuit breaker (CIRCUIT_BREAKER_INCIDENT_COUNT / WINDOW)
    - Circuit breaker incident timestamp purging outside window
    - Manual pause, resume, and status diagnostics

Probing principle: Tests assert exact boolean rejection results and pause reason messages.
"""

from decimal import Decimal
import pytest

from config.settings import Settings
from core.risk import RiskManager


@pytest.fixture
def risk_settings():
    """Return explicit Settings instance with deterministic risk limits for testing."""
    return Settings(
        BINANCE_API_KEY="test_key_123",
        BINANCE_API_SECRET="test_secret_456",
        TELEGRAM_BOT_TOKEN="123:ABC",
        TELEGRAM_CHAT_ID="999",
        MAX_POSITION_USDT=Decimal("100"),
        DAILY_LOSS_LIMIT_USDT=Decimal("-50"),
        MAX_CONCURRENT_TRIANGLES=2,
        CIRCUIT_BREAKER_INCIDENT_COUNT=3,
        CIRCUIT_BREAKER_WINDOW_MINUTES=60,
    )


@pytest.fixture
def risk_manager(risk_settings):
    """Return RiskManager initialized with test settings."""
    return RiskManager(settings=risk_settings)


class TestRiskManagerPreExecution:
    """Pre-execution check validation tests."""

    def test_happy_path_permits_execution(self, risk_manager):
        """Valid trade size within limits is permitted."""
        ok, reason = risk_manager.can_execute(Decimal("50"))
        assert ok is True
        assert reason is None

    def test_exceeding_position_cap_rejected(self, risk_manager):
        """Trade size exceeding MAX_POSITION_USDT is rejected."""
        ok, reason = risk_manager.can_execute(Decimal("150"))
        assert ok is False
        assert "exceeds cap" in reason

    def test_concurrency_limit_enforced(self, risk_manager):
        """In-flight triangle counter reaching MAX_CONCURRENT_TRIANGLES rejects execution."""
        risk_manager.register_execution_start()
        ok1, _ = risk_manager.can_execute(Decimal("50"))
        assert ok1 is True

        risk_manager.register_execution_start()
        ok2, reason2 = risk_manager.can_execute(Decimal("50"))
        assert ok2 is False
        assert "Max concurrent triangles reached" in reason2

        # Ending one execution frees up concurrency
        risk_manager.register_execution_end(Decimal("0.50"))
        ok3, _ = risk_manager.can_execute(Decimal("50"))
        assert ok3 is True

    def test_manual_pause_rejects_execution(self, risk_manager):
        """Manual pause blocks execution and retains pause reason."""
        risk_manager.pause("Operator requested pause")
        assert risk_manager.is_paused is True
        assert risk_manager.pause_reason == "Operator requested pause"

        ok, reason = risk_manager.can_execute(Decimal("10"))
        assert ok is False
        assert "Operator requested pause" in reason

        risk_manager.resume()
        assert risk_manager.is_paused is False
        ok2, _ = risk_manager.can_execute(Decimal("10"))
        assert ok2 is True


class TestRiskManagerDailyLoss:
    """Daily loss limit auto-pause tests."""

    def test_daily_loss_limit_triggers_auto_pause(self, risk_manager):
        """Cumulative loss dropping below DAILY_LOSS_LIMIT_USDT triggers auto-pause."""
        # Record small loss
        risk_manager.register_execution_end(Decimal("-20"))
        assert risk_manager.is_paused is False

        # Record further loss bringing total to -55 USDT (floor is -50 USDT)
        risk_manager.register_execution_end(Decimal("-35"))
        assert risk_manager.is_paused is True
        assert "Daily loss limit reached" in risk_manager.pause_reason

        ok, reason = risk_manager.can_execute(Decimal("10"))
        assert ok is False
        assert "Daily loss limit reached" in reason

    def test_reset_daily_pnl(self, risk_manager):
        """Resetting daily PnL restores execution capability when resumed."""
        risk_manager.register_execution_end(Decimal("-60"))
        assert risk_manager.is_paused is True

        risk_manager.reset_daily_pnl()
        assert risk_manager.daily_pnl_usdt == Decimal("0")
        risk_manager.resume()

        ok, _ = risk_manager.can_execute(Decimal("10"))
        assert ok is True


class TestRiskManagerCircuitBreaker:
    """Reconciliation incident circuit breaker tests."""

    def test_circuit_breaker_triggers_after_threshold_incidents(self, risk_manager):
        """3 incidents within 60 minutes trigger circuit breaker auto-pause."""
        t0 = 1700000000000

        risk_manager.record_incident(t0)
        assert risk_manager.is_paused is False

        risk_manager.record_incident(t0 + 1000)
        assert risk_manager.is_paused is False

        # 3rd incident within 60m window (60m = 3,600,000 ms)
        risk_manager.record_incident(t0 + 5000)
        assert risk_manager.is_paused is True
        assert "Circuit breaker triggered" in risk_manager.pause_reason

        ok, reason = risk_manager.can_execute(Decimal("10"), current_time_ms=t0 + 6000)
        assert ok is False
        assert "Circuit breaker triggered" in reason

    def test_old_incidents_purged_outside_window(self, risk_manager):
        """Incidents older than 60m are purged and do not trigger circuit breaker."""
        t0 = 1700000000000
        window_ms = 60 * 60 * 1000

        # Two old incidents
        risk_manager.record_incident(t0)
        risk_manager.record_incident(t0 + 1000)

        # 3rd incident happens 61 minutes later (first 2 must be purged)
        t_future = t0 + window_ms + 1000
        risk_manager.record_incident(t_future)
        assert risk_manager.is_paused is False

        ok, _ = risk_manager.can_execute(Decimal("10"), current_time_ms=t_future)
        assert ok is True

    def test_resume_clears_incident_history_window(self, risk_manager):
        """Calling resume() clears incident window so next can_execute() does not re-trigger."""
        t0 = 1700000000000
        risk_manager.record_incident(t0)
        risk_manager.record_incident(t0 + 1000)
        risk_manager.record_incident(t0 + 2000)
        assert risk_manager.is_paused is True

        risk_manager.resume()
        assert risk_manager.is_paused is False

        ok, reason = risk_manager.can_execute(Decimal("10"), current_time_ms=t0 + 3000)
        assert ok is True
        assert reason is None


class TestRiskManagerDiagnostics:
    """Status diagnostic snapshot tests."""

    def test_get_status(self, risk_manager):
        """get_status returns full structured dictionary of risk parameters and state."""
        status = risk_manager.get_status()
        assert status["is_paused"] is False
        assert status["daily_pnl_usdt"] == "0"
        assert status["max_position_usdt"] == "100"
        assert status["daily_loss_limit_usdt"] == "-50"
        assert status["max_concurrent_triangles"] == 2
        assert status["circuit_breaker_incident_count"] == 3
        assert status["circuit_breaker_window_minutes"] == 60
