"""Application-wide configuration via Pydantic Settings.

All runtime parameters are loaded from environment variables (or a .env file).
Pydantic validates types and raises a clear error at startup if a required
variable is missing or malformed — the bot never starts in a silently broken
state.

Usage:
    from config.settings import get_settings

    settings = get_settings()
    print(settings.BINANCE_API_KEY)

Environment:
    Copy .env.example to .env and fill in real values.
    Never commit .env — it is excluded by .gitignore from the first commit.
"""

from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralised, validated configuration for DeltaQuant.

    Every field maps 1-to-1 to a variable in .env.example.  Fields without
    defaults are *required*: the application will refuse to start if they are
    absent, preventing silent misconfiguration in production.

    Attributes:
        BINANCE_API_KEY: Binance REST/WS API key.  Spot-trading permissions
            only; withdrawal permission must NOT be enabled.
        BINANCE_API_SECRET: Corresponding API secret.
        DRY_RUN: When True, order placement is simulated against live prices
            without sending real orders to the exchange.
        MIN_VOLUME_USDT: Minimum 24-hour USDT-equivalent volume a trading pair
            must have to be included in the triangle graph.  Pairs below this
            threshold are silently dropped during the periodic REST refresh.
        SAFETY_MARGIN: Net return must exceed ``1.0 + SAFETY_MARGIN`` for a
            triangle to be considered actionable.  Set before running dry run;
            do not calibrate retrospectively (see docs/calibrations.md).
        MAX_TICK_AGE_MS: Maximum age in milliseconds of a book-ticker price
            before it is considered stale.  A triangle containing any stale
            price is discarded even if the maths look profitable.
        MAX_POSITION_USDT: Hard cap on USDT notional allocated to a single
            triangle execution.
        DAILY_LOSS_LIMIT_USDT: Cumulative daily PnL floor (negative value).
            The bot self-pauses when PnL drops below this threshold.
        MAX_CONCURRENT_TRIANGLES: Upper bound on the number of triangle
            executions that may be in-flight simultaneously.
        REDIS_HOST: Hostname of the Redis instance used for control-plane
            state (kill switch, checkpoints).  Redis is never in the
            evaluation hot path.
        REDIS_PORT: Redis TCP port.
        REDIS_DB: Redis logical database index.
        TELEGRAM_BOT_TOKEN: Token for the python-telegram-bot interface.
        TELEGRAM_CHAT_ID: Destination chat for alerts and control commands.
        LOG_LEVEL: Loguru log level (DEBUG, INFO, WARNING, ERROR).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="forbid",  # reject unknown env vars to surface typos early
    )

    # ── Binance credentials ───────────────────────────────────────────────────
    BINANCE_API_KEY: str = Field(
        ...,
        description="Binance API key — spot trading, no withdrawals.",
    )
    BINANCE_API_SECRET: str = Field(
        ...,
        description="Binance API secret.",
    )

    # ── Trading behaviour ─────────────────────────────────────────────────────
    DRY_RUN: bool = Field(
        default=True,
        description="Simulate execution without sending real orders.",
    )
    MIN_VOLUME_USDT: Decimal = Field(
        default=Decimal("1_000_000"),
        gt=Decimal("0"),
        description="Minimum 24h volume (USDT) for a pair to enter the graph.",
    )
    SAFETY_MARGIN: Decimal = Field(
        default=Decimal("0.0010"),
        gt=Decimal("0"),
        description="Required net return above 1.0 to flag an opportunity.",
    )
    MAX_TICK_AGE_MS: int = Field(
        default=200,
        gt=0,
        description="Max book-ticker age in ms before the price is stale.",
    )

    # ── Risk limits ───────────────────────────────────────────────────────────
    MAX_POSITION_USDT: Decimal = Field(
        default=Decimal("100"),
        gt=Decimal("0"),
        description="Maximum USDT notional per single triangle execution.",
    )
    DAILY_LOSS_LIMIT_USDT: Decimal = Field(
        default=Decimal("-50"),
        lt=Decimal("0"),
        description="Self-pause threshold: cumulative daily PnL floor (negative).",
    )
    MAX_CONCURRENT_TRIANGLES: int = Field(
        default=2,
        ge=1,
        description="Maximum number of in-flight triangle executions.",
    )
    CIRCUIT_BREAKER_INCIDENT_COUNT: int = Field(
        default=3,
        ge=1,
        description="Number of emergency liquidation incidents before auto-pausing.",
    )
    CIRCUIT_BREAKER_WINDOW_MINUTES: int = Field(
        default=60,
        ge=1,
        description="Time window in minutes for circuit breaker incident counting.",
    )

    # ── Redis (control-plane only) ────────────────────────────────────────────
    REDIS_HOST: str = Field(default="localhost")
    REDIS_PORT: int = Field(default=6379, gt=0, le=65535)
    REDIS_DB: int = Field(default=0, ge=0)

    # ── Telegram ─────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = Field(
        ...,
        description="Token issued by @BotFather.",
    )
    TELEGRAM_CHAT_ID: str = Field(
        ...,
        description="Target chat ID for alerts and control commands.",
    )

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = Field(default="INFO")

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        """Ensure LOG_LEVEL is one of the levels recognised by loguru.

        Args:
            value: Raw string from the environment variable.

        Returns:
            The uppercased log level string.

        Raises:
            ValueError: If the value is not a valid loguru log level.
        """
        allowed = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
        normalised = value.upper()
        if normalised not in allowed:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(allowed)}, got '{value}'"
            )
        return normalised

    @field_validator("BINANCE_API_KEY", "BINANCE_API_SECRET")
    @classmethod
    def validate_not_placeholder(cls, value: str, info: object) -> str:
        """Reject obvious placeholder values that were copied verbatim from .env.example.

        This catches the common mistake of starting the bot with the example
        file's placeholder strings instead of real credentials.

        Args:
            value: Raw credential string from the environment.
            info: Pydantic validation info (unused, required by the signature).

        Returns:
            The credential string unchanged if it passes the check.

        Raises:
            ValueError: If the value looks like a placeholder.
        """
        lower = value.lower()
        if "your_" in lower or "placeholder" in lower or value.strip() == "":
            raise ValueError(
                "Credential appears to be a placeholder. "
                "Set a real value in .env (never commit .env)."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance, loaded once and cached.

    Using lru_cache ensures the .env file is parsed exactly once per process
    lifetime, avoiding repeated disk reads on every call-site.

    Returns:
        The validated Settings instance.

    Raises:
        pydantic_settings.ValidationError: If any required variable is missing
            or any value fails its validator.
    """
    return Settings()
