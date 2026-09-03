"""Async Redis control-plane client for DeltaQuant.

This module intentionally keeps Redis out of the market-data hot path.
It is used only for control-plane state (kill switch and restart checkpoints).

The key design for Phase 4 is that Redis stores the desired trading state
across process restarts:
    - TRADING_ENABLED = "1" means execution is allowed.
    - TRADING_ENABLED = "0" means execution must remain paused.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from redis.asyncio import Redis

from config.settings import Settings


class RedisControlPlaneClient:
    """Small async wrapper around Redis control-plane keys.

    Attributes:
        _redis: Async Redis client instance.
    """

    TRADING_ENABLED_KEY = "deltaquant:control:trading_enabled"
    PAUSE_REASON_KEY = "deltaquant:control:pause_reason"
    CHECKPOINT_PREFIX = "deltaquant:checkpoint:"

    def __init__(self, redis_client: Redis) -> None:
        """Create a RedisControlPlaneClient from an async Redis client.

        Args:
            redis_client: Connected async Redis instance.
        """
        self._redis = redis_client

    @classmethod
    def from_settings(cls, settings: Settings) -> "RedisControlPlaneClient":
        """Build a RedisControlPlaneClient from application settings.

        Args:
            settings: Loaded application settings.

        Returns:
            Configured RedisControlPlaneClient.
        """
        redis_client = Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            decode_responses=True,
        )
        return cls(redis_client)

    async def connect(self) -> None:
        """Validate Redis connectivity and initialize default control keys.

        Raises:
            redis.exceptions.RedisError: If connectivity check fails.
        """
        await self._redis.ping()
        await self._redis.setnx(self.TRADING_ENABLED_KEY, "1")
        await self._redis.setnx(self.PAUSE_REASON_KEY, "")

    async def close(self) -> None:
        """Close underlying Redis connections."""
        await self._redis.aclose()

    async def get_trading_enabled(self) -> bool:
        """Return the desired control-plane trading state.

        Missing or malformed values are treated as enabled to avoid a dead
        process due to absent keys; ``connect()`` initializes defaults.
        """
        raw_value = await self._redis.get(self.TRADING_ENABLED_KEY)
        if raw_value is None:
            return True
        return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}

    async def set_trading_enabled(self, enabled: bool, pause_reason: str = "") -> None:
        """Persist desired trading state.

        Args:
            enabled: Whether trading should be enabled.
            pause_reason: Human-readable reason used when paused.
        """
        value = "1" if enabled else "0"
        reason = "" if enabled else pause_reason
        await self._redis.set(self.TRADING_ENABLED_KEY, value)
        await self._redis.set(self.PAUSE_REASON_KEY, reason)

    async def get_pause_reason(self) -> str:
        """Return persisted pause reason from Redis.

        Returns:
            Empty string if no reason is stored.
        """
        reason = await self._redis.get(self.PAUSE_REASON_KEY)
        return "" if reason is None else str(reason)

    async def set_checkpoint(self, name: str, payload: dict[str, Any]) -> None:
        """Persist a JSON checkpoint payload under a namespaced key.

        Args:
            name: Logical checkpoint name.
            payload: JSON-serializable checkpoint data.
        """
        key = f"{self.CHECKPOINT_PREFIX}{name}"
        await self._redis.set(key, json.dumps(payload, sort_keys=True))

    async def get_checkpoint(self, name: str) -> Optional[dict[str, Any]]:
        """Load a JSON checkpoint payload if it exists.

        Args:
            name: Logical checkpoint name.

        Returns:
            Parsed checkpoint dictionary, or None if absent.
        """
        key = f"{self.CHECKPOINT_PREFIX}{name}"
        raw_value = await self._redis.get(key)
        if raw_value is None:
            return None
        return json.loads(raw_value)