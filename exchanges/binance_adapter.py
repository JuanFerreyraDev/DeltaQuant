"""Binance exchange adapter — Phase 1 read-only implementation.

Implements ``ExchangeAdapter`` for Binance using the ``ccxt`` REST client.
Only market-data and account-read methods are implemented in this phase;
WebSocket subscription (``subscribe_book_ticker``) and order placement
(``place_fok_order``) are stubbed with ``NotImplementedError`` and will be
completed in Phase 2 and Phase 3 respectively.

Phase scope (per the technical plan, §8 Fase 1):
    - Fetch all trading pairs and their 24-hour volume via REST.
    - Fetch trading fees for a symbol.
    - Fetch account balance for an asset.

Out of scope for Phase 1 (explicitly deferred):
    - ``subscribe_book_ticker``: requires ccxt.pro WebSocket (Phase 2).
    - ``place_fok_order``: order execution (Phase 3).

Notes on ccxt usage:
    ``ccxt`` (synchronous REST) is used here; ``ccxt.pro`` (WebSocket) is
    introduced in Phase 2.  The adapter wraps the blocking ccxt calls in
    ``asyncio.get_event_loop().run_in_executor`` so they don't block the
    event loop, even though the full async pipeline is not wired until Phase 2.

Security:
    API keys are read from ``Settings`` at construction time.  No key or
    secret is ever stored as a plain module-level variable or logged.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from functools import lru_cache
from typing import AsyncIterator

import ccxt

from config.settings import Settings
from exchanges.base import (
    Balance,
    BookTicker,
    ExchangeAdapter,
    OrderResult,
    TradingFees,
)


class BinanceAdapter(ExchangeAdapter):
    """Binance implementation of ``ExchangeAdapter``.

    Wraps ``ccxt.binance`` for REST access.  Instantiate via the async
    factory ``BinanceAdapter.create(settings)`` rather than the constructor
    directly, to allow for any async initialisation steps added in later phases.

    Attributes:
        _client: Underlying ``ccxt.binance`` REST client instance.
        _settings: Validated application settings.
        _fee_cache: In-process cache of ``TradingFees`` objects keyed by
            symbol, populated lazily on first request.  Fee tiers change
            rarely, so this avoids repeated REST round-trips during the
            periodic volume-refresh cycle.
    """

    def __init__(self, client: ccxt.binance, settings: Settings) -> None:
        """Initialise the adapter with an already-constructed ccxt client.

        Prefer ``BinanceAdapter.create(settings)`` over calling this directly.

        Args:
            client: A configured ``ccxt.binance`` instance.
            settings: Validated ``Settings`` object from ``config.settings``.
        """
        self._client: ccxt.binance = client
        self._settings: Settings = settings
        self._fee_cache: dict[str, TradingFees] = {}

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def create(cls, settings: Settings) -> "BinanceAdapter":
        """Async factory: construct and return a ready ``BinanceAdapter``.

        Builds the ``ccxt.binance`` client with the credentials from
        ``settings`` and runs a lightweight connectivity check (load markets)
        off the event loop so the caller stays non-blocking.

        In Phase 2 this factory will also open the WebSocket connection.

        Args:
            settings: Validated ``Settings`` loaded from the environment.

        Returns:
            A fully initialised ``BinanceAdapter`` ready to serve requests.

        Raises:
            ccxt.AuthenticationError: If the API key or secret is invalid.
            ccxt.NetworkError: If Binance is unreachable.
        """
        loop = asyncio.get_event_loop()

        def _build_client() -> ccxt.binance:
            return ccxt.binance(
                {
                    "apiKey": settings.BINANCE_API_KEY,
                    "secret": settings.BINANCE_API_SECRET,
                    # Enable rate-limit tracking: ccxt will throttle requests
                    # automatically to stay within Binance weight limits.
                    "enableRateLimit": True,
                    # Use the production endpoint; testnet can be toggled here
                    # for integration testing without real capital.
                    "options": {
                        "defaultType": "spot",
                        "adjustForTimeDifference": True,
                    },
                }
            )

        client = await loop.run_in_executor(None, _build_client)
        return cls(client, settings)

    # ── Market data ───────────────────────────────────────────────────────────

    async def fetch_tickers_24h(self) -> list[dict]:
        """Fetch 24-hour ticker statistics for all trading pairs via REST.

        Calls the Binance ``GET /api/v3/ticker/24hr`` endpoint through ccxt's
        ``fetch_tickers`` wrapper.  This is the data source for the volume
        filter in ``core/graph.py``.

        The result is a raw list of ticker dicts as returned by ccxt, each
        containing at minimum:
            - ``symbol``: ccxt unified symbol, e.g. ``"BTC/USDT"``
            - ``quoteVolume``: 24h volume expressed in the quote asset
            - ``baseVolume``: 24h volume in the base asset

        The caller (``graph.py``) is responsible for interpreting and
        filtering these dicts; this method does no filtering of its own.

        Returns:
            List of raw ccxt ticker dicts for every active spot pair.

        Raises:
            ccxt.NetworkError: On connectivity failure.
            ccxt.ExchangeError: If the exchange returns an error response.
        """
        loop = asyncio.get_event_loop()
        raw: dict[str, dict] = await loop.run_in_executor(
            None, self._client.fetch_tickers
        )
        # ccxt.fetch_tickers returns a dict keyed by symbol; return as a list
        # so the caller doesn't need to know the internal ccxt structure.
        return list(raw.values())

    async def subscribe_book_ticker(
        self, symbols: list[str]
    ) -> AsyncIterator[BookTicker]:
        """Subscribe to best-bid/ask WebSocket stream (Phase 2).

        Not implemented in Phase 1.  WebSocket connectivity via ccxt.pro is
        introduced in Phase 2 (``feature/f2-binance-ws-bookticker``).

        Args:
            symbols: List of symbols to subscribe to.

        Raises:
            NotImplementedError: Always, in Phase 1.

        Yields:
            Nothing — this method is not yet implemented.
        """
        raise NotImplementedError(
            "subscribe_book_ticker is implemented in Phase 2 "
            "(feature/f2-binance-ws-bookticker)."
        )
        yield  # pragma: no cover — keeps the abstract generator signature

    async def get_trading_fees(self, symbol: str) -> TradingFees:
        """Fetch maker/taker fees for a symbol, with in-process caching.

        Calls ``ccxt.binance.fetch_trading_fee`` and converts the result to a
        ``TradingFees`` dataclass.  The result is cached for the lifetime of
        this adapter instance; fee tiers change rarely and a per-request round-
        trip is wasteful during the high-frequency evaluation cycle.

        BNB fee discount (25 % reduction when paying fees in BNB) is NOT
        applied here — that calculation is deferred to ``exchanges/fees.py``
        in Phase 2, which wraps this method and adjusts the effective rate.

        Args:
            symbol: Binance native symbol string, e.g. ``"BTCUSDT"``.

        Returns:
            ``TradingFees`` with ``maker`` and ``taker`` as ``Decimal``
            fractions (e.g. ``Decimal("0.001")`` = 0.1 %).

        Raises:
            ccxt.BadSymbol: If ``symbol`` is not recognised by Binance.
            ccxt.NetworkError: On connectivity failure.
        """
        if symbol in self._fee_cache:
            return self._fee_cache[symbol]

        loop = asyncio.get_event_loop()
        raw: dict = await loop.run_in_executor(
            None, self._client.fetch_trading_fee, symbol
        )

        fees = TradingFees(
            symbol=symbol,
            maker=Decimal(str(raw.get("maker", "0.001"))),
            taker=Decimal(str(raw.get("taker", "0.001"))),
        )
        self._fee_cache[symbol] = fees
        return fees

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self, asset: str) -> Balance:
        """Fetch the current spot balance for a single asset.

        Calls ``ccxt.binance.fetch_balance`` (a signed request) and extracts
        the free and locked amounts for ``asset``.  If the asset is not present
        in the account (e.g. never held), returns a zero-balance object rather
        than raising an error.

        Args:
            asset: Asset ticker to query, e.g. ``"USDT"`` or ``"BTC"``.

        Returns:
            ``Balance`` with ``free`` and ``locked`` amounts.  Both are
            ``Decimal("0")`` if the asset is not held.

        Raises:
            ccxt.AuthenticationError: If the API key is invalid.
            ccxt.PermissionDenied: If the key lacks read-account permission.
            ccxt.NetworkError: On connectivity failure.
        """
        loop = asyncio.get_event_loop()
        raw: dict = await loop.run_in_executor(None, self._client.fetch_balance)

        asset_upper = asset.upper()
        free = Decimal(str(raw.get("free", {}).get(asset_upper, "0")))
        locked = Decimal(str(raw.get("used", {}).get(asset_upper, "0")))

        return Balance(asset=asset_upper, free=free, locked=locked)

    # ── Order placement (deferred to Phase 3) ────────────────────────────────

    async def place_fok_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        """Place a Fill-or-Kill order on Binance (Phase 3).

        Not implemented in Phase 1 or Phase 2.  FOK order dispatch is
        introduced in Phase 3 (``feature/f3-executor-parallel-dryrun``),
        initially in DRY_RUN mode, and goes live in Phase 5.

        Args:
            symbol: Binance native symbol string.
            side: ``"BUY"`` or ``"SELL"``.
            quantity: Base-asset quantity.
            price: Limit price in quote-asset units.

        Raises:
            NotImplementedError: Always, in Phase 1.
        """
        raise NotImplementedError(
            "place_fok_order is implemented in Phase 3 "
            "(feature/f3-executor-parallel-dryrun)."
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @lru_cache(maxsize=1)
    def _load_markets(self) -> dict:
        """Load and cache the full market metadata from Binance.

        Calls ``ccxt.binance.load_markets()`` which returns a dict of all
        trading pairs with their filters (lot size, tick size, min notional,
        etc.).  The result is cached via ``lru_cache`` because it rarely
        changes and the call costs one REST round-trip.

        This is a synchronous helper intended to be called inside
        ``run_in_executor`` by async callers.

        Returns:
            Dict of ccxt market objects keyed by unified symbol
            (e.g. ``"BTC/USDT"``).
        """
        return self._client.load_markets()

    async def get_markets(self) -> dict:
        """Return all spot markets loaded from Binance, with caching.

        Async wrapper around ``_load_markets``.  The first call fetches from
        the exchange; subsequent calls return the cached result instantly.

        Returns:
            Dict of ccxt market objects keyed by unified symbol.

        Raises:
            ccxt.NetworkError: On the first call if Binance is unreachable.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._load_markets)
