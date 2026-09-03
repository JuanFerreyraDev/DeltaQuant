"""Unit tests for storage/redis_client.py.

Tests focus on Phase 4 control-plane guarantees:
    - TRADING_ENABLED persistence semantics.
    - Pause reason consistency.
    - Checkpoint round-trip behavior.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from config.settings import Settings
from storage.redis_client import RedisControlPlaneClient


class _FakeRedis:
    """In-memory async Redis stand-in for deterministic unit tests."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value: str) -> bool:
        self._store[key] = value
        return True

    async def setnx(self, key: str, value: str) -> bool:
        if key in self._store:
            return False
        self._store[key] = value
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        BINANCE_API_KEY="test_key_123",
        BINANCE_API_SECRET="test_secret_456",
        TELEGRAM_BOT_TOKEN="123:ABC",
        TELEGRAM_CHAT_ID="999",
        MIN_VOLUME_USDT=Decimal("1000000"),
    )


@pytest.fixture
def redis_client() -> RedisControlPlaneClient:
    return RedisControlPlaneClient(_FakeRedis())


@pytest.mark.asyncio
async def test_connect_initializes_control_keys(redis_client: RedisControlPlaneClient) -> None:
    """connect() initializes missing keys to trading enabled and empty reason."""
    await redis_client.connect()
    assert await redis_client.get_trading_enabled() is True
    assert await redis_client.get_pause_reason() == ""


@pytest.mark.asyncio
async def test_set_kill_switch_persists_disabled_state(redis_client: RedisControlPlaneClient) -> None:
    """set_trading_enabled(False) persists disabled state and pause reason."""
    await redis_client.connect()
    await redis_client.set_trading_enabled(False, pause_reason="operator kill")

    assert await redis_client.get_trading_enabled() is False
    assert await redis_client.get_pause_reason() == "operator kill"


@pytest.mark.asyncio
async def test_resume_clears_pause_reason(redis_client: RedisControlPlaneClient) -> None:
    """set_trading_enabled(True) restores enabled state and clears pause reason."""
    await redis_client.connect()
    await redis_client.set_trading_enabled(False, pause_reason="incident")
    await redis_client.set_trading_enabled(True, pause_reason="ignored")

    assert await redis_client.get_trading_enabled() is True
    assert await redis_client.get_pause_reason() == ""


@pytest.mark.asyncio
async def test_checkpoint_round_trip(redis_client: RedisControlPlaneClient) -> None:
    """set_checkpoint/get_checkpoint round-trip JSON payload without data loss."""
    await redis_client.connect()
    payload = {"evaluations": 42, "last_symbol": "BTCUSDT", "paused": False}
    await redis_client.set_checkpoint("engine_state", payload)

    loaded = await redis_client.get_checkpoint("engine_state")
    assert loaded == payload


@pytest.mark.asyncio
async def test_missing_checkpoint_returns_none(redis_client: RedisControlPlaneClient) -> None:
    """get_checkpoint returns None for missing keys."""
    await redis_client.connect()
    assert await redis_client.get_checkpoint("does_not_exist") is None