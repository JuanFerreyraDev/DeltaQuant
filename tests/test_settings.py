"""Unit tests for config/settings.py — validators, field constraints, singleton.

Uses environment variable injection (monkeypatch) so no real .env file is
needed. Each test constructs Settings directly with the minimum required
fields rather than going through get_settings(), which is cached.

Covers:
    - validate_log_level: valid levels, case normalisation, invalid values
    - validate_not_placeholder: all rejection triggers, valid passthrough
    - Field constraint violations: gt/lt/ge/le on numeric fields
    - extra="forbid": unknown env var raises ValidationError
    - get_settings: singleton identity and cache-clear behaviour
"""

import pytest
from pydantic import ValidationError

from config.settings import Settings, get_settings


# ── Minimal valid kwargs for Settings construction ────────────────────────────
# TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required but have no defaults;
# we supply dummy-but-valid values throughout.

_VALID_BASE = {
    "BINANCE_API_KEY": "real_api_key_abc123",
    "BINANCE_API_SECRET": "real_api_secret_xyz789",
    "TELEGRAM_BOT_TOKEN": "123456:ABCdef",
    "TELEGRAM_CHAT_ID": "987654321",
}


def _make(**overrides) -> Settings:
    """Build a Settings instance from _VALID_BASE merged with overrides."""
    return Settings(**{**_VALID_BASE, **overrides})


# ── validate_log_level ────────────────────────────────────────────────────────


class TestValidateLogLevel:
    @pytest.mark.parametrize("level", ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"])
    def test_valid_levels_accepted(self, level):
        """All loguru-recognised levels are accepted."""
        s = _make(LOG_LEVEL=level)
        assert s.LOG_LEVEL == level

    @pytest.mark.parametrize("level", ["info", "debug", "Warning", "eRRoR"])
    def test_case_insensitive_normalised_to_upper(self, level):
        """Input is normalised to uppercase regardless of original casing."""
        s = _make(LOG_LEVEL=level)
        assert s.LOG_LEVEL == level.upper()

    @pytest.mark.parametrize("bad", ["VERBOSE", "WARN", "FATAL", "info2", ""])
    def test_invalid_level_raises(self, bad):
        """Unrecognised log levels raise ValidationError at construction."""
        with pytest.raises(ValidationError):
            _make(LOG_LEVEL=bad)


# ── validate_not_placeholder ──────────────────────────────────────────────────


class TestValidateNotPlaceholder:
    @pytest.mark.parametrize("bad_key", [
        "your_api_key_here",
        "YOUR_API_KEY",
        "my_placeholder_value",
        "contains_PLACEHOLDER_in_middle",
        "",
        "   ",          # whitespace only
    ])
    def test_placeholder_values_rejected_for_api_key(self, bad_key):
        """Obvious placeholder values are rejected for BINANCE_API_KEY."""
        with pytest.raises(ValidationError):
            Settings(**{**_VALID_BASE, "BINANCE_API_KEY": bad_key})

    @pytest.mark.parametrize("bad_secret", [
        "your_secret_here",
        "placeholder",
        "",
    ])
    def test_placeholder_values_rejected_for_api_secret(self, bad_secret):
        """Obvious placeholder values are rejected for BINANCE_API_SECRET."""
        with pytest.raises(ValidationError):
            Settings(**{**_VALID_BASE, "BINANCE_API_SECRET": bad_secret})

    def test_real_looking_key_accepted(self):
        """A realistic-looking API key passes the placeholder check."""
        s = Settings(**{**_VALID_BASE, "BINANCE_API_KEY": "abc123XYZ_real_key"})
        assert s.BINANCE_API_KEY == "abc123XYZ_real_key"


# ── Field numeric constraints ─────────────────────────────────────────────────


class TestFieldConstraints:
    def test_min_volume_usdt_must_be_positive(self):
        """MIN_VOLUME_USDT gt=0: zero or negative raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(MIN_VOLUME_USDT="0")
        with pytest.raises(ValidationError):
            _make(MIN_VOLUME_USDT="-1")

    def test_safety_margin_must_be_positive(self):
        """SAFETY_MARGIN gt=0: zero raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(SAFETY_MARGIN="0")

    def test_max_tick_age_ms_must_be_positive(self):
        """MAX_TICK_AGE_MS gt=0: zero raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(MAX_TICK_AGE_MS=0)

    def test_max_position_usdt_must_be_positive(self):
        """MAX_POSITION_USDT gt=0: zero raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(MAX_POSITION_USDT="0")

    def test_daily_loss_limit_must_be_negative(self):
        """DAILY_LOSS_LIMIT_USDT lt=0: zero or positive raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(DAILY_LOSS_LIMIT_USDT="0")
        with pytest.raises(ValidationError):
            _make(DAILY_LOSS_LIMIT_USDT="10")

    def test_max_concurrent_triangles_minimum_one(self):
        """MAX_CONCURRENT_TRIANGLES ge=1: zero raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(MAX_CONCURRENT_TRIANGLES=0)

    def test_redis_port_lower_bound(self):
        """REDIS_PORT gt=0: zero raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(REDIS_PORT=0)

    def test_redis_port_upper_bound(self):
        """REDIS_PORT le=65535: 65536 raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(REDIS_PORT=65536)

    def test_redis_db_must_be_non_negative(self):
        """REDIS_DB ge=0: -1 raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(REDIS_DB=-1)


# ── extra="forbid" ────────────────────────────────────────────────────────────


class TestExtraForbid:
    def test_unknown_field_raises(self):
        """Supplying an unknown field raises ValidationError (extra='forbid')."""
        with pytest.raises(ValidationError):
            _make(UNKNOWN_TYPO_VAR="oops")


# ── Defaults ──────────────────────────────────────────────────────────────────


class TestDefaults:
    def test_dry_run_defaults_true(self):
        """DRY_RUN defaults to True — the bot never accidentally goes live."""
        s = _make()
        assert s.DRY_RUN is True

    def test_binance_testnet_defaults_false(self):
        """BINANCE_TESTNET defaults to False so live testnet orders stay opt-in."""
        s = _make()
        assert s.BINANCE_TESTNET is False

    def test_numeric_defaults_are_sane(self):
        """Spot-check a handful of defaults to guard against accidental changes."""
        s = _make()
        assert s.MIN_VOLUME_USDT > 0
        assert s.SAFETY_MARGIN > 0
        assert s.MAX_TICK_AGE_MS > 0
        assert s.DAILY_LOSS_LIMIT_USDT < 0
        assert s.MAX_CONCURRENT_TRIANGLES >= 1


# ── get_settings singleton ────────────────────────────────────────────────────


class TestGetSettings:
    def test_returns_same_object_on_repeated_calls(self, monkeypatch):
        """get_settings() returns the exact same instance on repeated calls (lru_cache)."""
        # Provide required env vars via monkeypatch so Settings() can construct.
        monkeypatch.setenv("BINANCE_API_KEY", "real_key_abc")
        monkeypatch.setenv("BINANCE_API_SECRET", "real_secret_xyz")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:AAA")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        get_settings.cache_clear()
        first = get_settings()
        second = get_settings()
        assert first is second  # identity, not just equality
        get_settings.cache_clear()  # restore clean state for other tests

    def test_cache_clear_allows_reload(self, monkeypatch):
        """After cache_clear(), get_settings() re-reads the environment."""
        monkeypatch.setenv("BINANCE_API_KEY", "real_key_abc")
        monkeypatch.setenv("BINANCE_API_SECRET", "real_secret_xyz")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:AAA")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")

        get_settings.cache_clear()
        s1 = get_settings()
        assert s1.LOG_LEVEL == "DEBUG"

        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        # Without cache_clear, still returns old instance.
        assert get_settings() is s1

        get_settings.cache_clear()
        s2 = get_settings()
        assert s2.LOG_LEVEL == "ERROR"
        assert s2 is not s1
        get_settings.cache_clear()


class TestTestnetMode:
    def test_testnet_requires_credentials(self):
        """Enabling BINANCE_TESTNET without dedicated credentials raises ValidationError."""
        with pytest.raises(ValidationError):
            _make(
                BINANCE_TESTNET=True,
                TESTNET_BINANCE_API_KEY="",
                TESTNET_BINANCE_API_SECRET="",
            )

    def test_testnet_accepts_credentials(self):
        """BINANCE_TESTNET=True requires explicit testnet API key and secret."""
        s = _make(
            BINANCE_TESTNET=True,
            TESTNET_BINANCE_API_KEY="testnet_key_123",
            TESTNET_BINANCE_API_SECRET="testnet_secret_456",
        )
        assert s.BINANCE_TESTNET is True
        assert s.TESTNET_BINANCE_API_KEY == "testnet_key_123"
        assert s.TESTNET_BINANCE_API_SECRET == "testnet_secret_456"

    def test_live_mode_requires_testnet_in_this_stage(self):
        """DRY_RUN=False and BINANCE_TESTNET=False must fail closed at startup."""
        with pytest.raises(ValidationError):
            _make(
                DRY_RUN=False,
                BINANCE_TESTNET=False,
            )
