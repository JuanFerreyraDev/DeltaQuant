"""Tests for BinanceAdapter.create() connectivity contract.

Bug discovered during Phase 1 review (not part of the original three fixes):
create() docstring promised a connectivity check via load_markets() but the
implementation only called ccxt.binance() in memory — no network request, no
credential validation.  An adapter built with an invalid API key would succeed
silently until the first operational call.

Fixed in fix/f1-binance-adapter-create-connectivity-check:
  - _build_and_load() calls load_markets() inside run_in_executor
  - adapter._markets_cache is pre-populated from client.markets on success
  - AuthenticationError / NetworkError from load_markets() propagate to caller

These tests mock ccxt.binance at the class level so no real network calls are
made.  pytest-asyncio (already in requirements.txt) handles the async fixtures.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt
import pytest

from config.settings import Settings
from exchanges.binance_adapter import BinanceAdapter


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_settings() -> Settings:
    """Return a minimal valid Settings instance with dummy credentials."""
    return Settings(
        BINANCE_API_KEY="real_key_abc123",
        BINANCE_API_SECRET="real_secret_xyz789",
        TELEGRAM_BOT_TOKEN="111:AAA",
        TELEGRAM_CHAT_ID="123",
    )


def _mock_ccxt_client(markets: dict | None = None) -> MagicMock:
    """Build a MagicMock that mimics a ccxt.binance instance.

    Args:
        markets: The dict returned by load_markets() and stored in
            client.markets.  Defaults to a minimal one-entry dict.

    Returns:
        A MagicMock with load_markets() and .markets configured.
    """
    fake_markets = markets if markets is not None else {"BTC/USDT": {"symbol": "BTC/USDT"}}
    client = MagicMock(spec=ccxt.binance)
    client.load_markets.return_value = fake_markets
    client.markets = fake_markets
    return client


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestBinanceAdapterCreate:
    """Verify the connectivity contract of BinanceAdapter.create()."""

    @pytest.mark.asyncio
    async def test_create_calls_load_markets(self):
        """create() must call load_markets() during initialisation.

        The original bug: ccxt.binance(...) builds the client in memory with no
        network request, so invalid credentials or an unreachable host would
        not be caught until the first operational call.  load_markets() makes a
        real REST request and is the earliest point where auth is validated.
        """
        fake_client = _mock_ccxt_client()
        settings = _make_settings()

        with patch("exchanges.binance_adapter.ccxt.binance", return_value=fake_client):
            adapter = await BinanceAdapter.create(settings)

        # load_markets() must have been called exactly once during create()
        fake_client.load_markets.assert_called_once()
        assert isinstance(adapter, BinanceAdapter)

    @pytest.mark.asyncio
    async def test_create_prepopulates_markets_cache(self):
        """After create(), _markets_cache must be set — get_markets() is free.

        Pre-populating the cache from client.markets (which load_markets()
        already populated) means the first get_markets() call returns instantly
        without a second network round-trip.
        """
        fake_markets = {
            "BTC/USDT": {"symbol": "BTC/USDT"},
            "ETH/USDT": {"symbol": "ETH/USDT"},
        }
        fake_client = _mock_ccxt_client(markets=fake_markets)
        settings = _make_settings()

        with patch("exchanges.binance_adapter.ccxt.binance", return_value=fake_client):
            adapter = await BinanceAdapter.create(settings)

        assert adapter._markets_cache is not None, (
            "_markets_cache must be pre-populated after create()"
        )
        assert adapter._markets_cache == fake_markets

    @pytest.mark.asyncio
    async def test_create_propagates_authentication_error(self):
        """ccxt.AuthenticationError from load_markets() propagates to the caller.

        This is the primary motivator for the bug fix: an invalid API key must
        cause create() to raise, not silently succeed and fail later.
        """
        fake_client = _mock_ccxt_client()
        fake_client.load_markets.side_effect = ccxt.AuthenticationError("Invalid API key")
        settings = _make_settings()

        with patch("exchanges.binance_adapter.ccxt.binance", return_value=fake_client):
            with pytest.raises(ccxt.AuthenticationError):
                await BinanceAdapter.create(settings)

    @pytest.mark.asyncio
    async def test_create_propagates_network_error(self):
        """ccxt.NetworkError from load_markets() propagates to the caller.

        An unreachable host must also fail fast at startup rather than silently
        returning an adapter that will error on every subsequent call.
        """
        fake_client = _mock_ccxt_client()
        fake_client.load_markets.side_effect = ccxt.NetworkError("Connection timeout")
        settings = _make_settings()

        with patch("exchanges.binance_adapter.ccxt.binance", return_value=fake_client):
            with pytest.raises(ccxt.NetworkError):
                await BinanceAdapter.create(settings)

    @pytest.mark.asyncio
    async def test_create_does_not_call_load_markets_twice(self):
        """load_markets() is called exactly once in create() — not again in get_markets().

        Because _markets_cache is pre-populated from client.markets, the first
        get_markets() call must return the cached value without a second
        load_markets() invocation.
        """
        fake_client = _mock_ccxt_client()
        settings = _make_settings()

        with patch("exchanges.binance_adapter.ccxt.binance", return_value=fake_client):
            adapter = await BinanceAdapter.create(settings)
            # Calling get_markets() after create() must NOT trigger load_markets() again.
            await adapter.get_markets()

        fake_client.load_markets.assert_called_once()
