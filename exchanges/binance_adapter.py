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

Symbol format contract (ADR-002):
    The engine layer (``core/graph.py``) produces symbols in a mixed native
    format:

    - USDT-quoted pairs: concatenated native format (e.g. ``"BTCUSDT"``),
      because the fixed 4-char ``"USDT"`` suffix makes the split unambiguous.
    - Cross pairs (non-USDT quote): slash-delimited unified format
      (e.g. ``"ETH/BTC"``), because concatenation is ambiguous without a
      known-assets list and ``parse_symbol`` cannot recover base/quote from
      ``"ETHBTC"``.

    This adapter accepts both formats.  ``_native_to_unified`` converts
    concatenated USDT symbols to ccxt unified format and passes slash-delimited
    symbols through unchanged.  ``_unified_to_native`` is only safe for
    USDT-quoted symbols (``"BTC/USDT"`` → ``"BTCUSDT"``); do not call it on
    cross-pair unified symbols.

Security:
    API keys are read from ``Settings`` at construction time.  No key or
    secret is ever stored as a plain module-level variable or logged.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
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


# ── Symbol format conversion ──────────────────────────────────────────────────


def _native_to_unified(native_symbol: str) -> str:
    """Convert a symbol to ccxt unified format (``"BASE/QUOTE"``).

    Handles the two symbol formats that the engine layer produces:

    - **Concatenated USDT pairs** (e.g. ``"BTCUSDT"``): the fixed 4-char
      ``"USDT"`` suffix is unambiguous, so the function strips it and
      inserts a slash → ``"BTC/USDT"``.
    - **Slash-delimited cross pairs** (e.g. ``"ETH/BTC"``): already in
      unified format — passed through unchanged.

    This function must not be called with an ambiguous concatenated
    cross-pair symbol like ``"ETHBTC"``; ``parse_symbol("ETHBTC")`` returns
    ``None``, and there is no safe way to recover the split without a
    known-assets list.  The graph layer avoids producing such symbols by
    using slash-delimited format for all non-USDT pairs (see ADR-002).

    Args:
        native_symbol: Symbol string from the engine layer — either a
            concatenated USDT pair (e.g. ``"BTCUSDT"``) or a slash-delimited
            cross pair (e.g. ``"ETH/BTC"``).

    Returns:
        ccxt unified symbol string with a ``/`` delimiter,
        e.g. ``"BTC/USDT"`` or ``"ETH/BTC"``.
    """
    # Slash-delimited symbols are already in unified format — pass through.
    if "/" in native_symbol:
        return native_symbol

    # Concatenated USDT pairs: strip the 4-char suffix and insert a slash.
    if native_symbol.endswith("USDT") and len(native_symbol) > 4:
        base = native_symbol[:-4]
        return f"{base}/USDT"

    # Anything else is an unrecognised format.  Return as-is so the ccxt
    # call surfaces the error with a clear BadSymbol exception rather than
    # a silent wrong lookup.
    return native_symbol


def _unified_to_native(unified_symbol: str) -> str:
    """Convert a ccxt unified USDT symbol to concatenated native format.

    Removes the ``/`` delimiter from a USDT-quoted unified symbol:
    ``"BTC/USDT"`` → ``"BTCUSDT"``.

    **Scope**: safe only for USDT-quoted symbols.  Calling this on a
    cross-pair unified symbol (e.g. ``"ETH/BTC"``) produces the ambiguous
    concatenated form ``"ETHBTC"``, which ``parse_symbol`` cannot recover.
    Do not call this function on cross-pair symbols — the engine already
    stores them in slash-delimited format and should receive them back
    unchanged.

    Args:
        unified_symbol: ccxt unified USDT symbol, e.g. ``"BTC/USDT"``.

    Returns:
        Concatenated native symbol, e.g. ``"BTCUSDT"``.
    """
    return unified_symbol.replace("/", "")


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
        _markets_cache: In-process cache of the full market metadata dict
            loaded from Binance.  Populated lazily on the first call to
            ``get_markets()``.  Stored as instance state rather than
            ``lru_cache`` so that multiple adapter instances (tests, future
            multi-exchange setups) maintain independent caches and don't
            evict each other's entries.
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
        self._markets_cache: dict | None = None

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

        **Symbol format**: will accept native format symbols (e.g. ``"BTCUSDT"``)
        and convert to unified format internally for ccxt.pro calls. See ADR-002
        for the symbol format contract.

        Args:
            symbols: List of native format symbols to subscribe to (e.g.
                ``["BTCUSDT", "ETHUSDT"]``).

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

        **Symbol format**: accepts both engine symbol formats (ADR-002):
        concatenated USDT pairs (e.g. ``"BTCUSDT"``) and slash-delimited
        cross pairs (e.g. ``"ETH/BTC"``).  ``_native_to_unified`` converts
        to the ccxt format required by ``fetch_trading_fee``.  The cache key
        is the symbol exactly as received from the engine, so USDT and cross
        pairs are cached independently and consistently.

        BNB fee discount (25 % reduction when paying fees in BNB) is NOT
        applied here — that calculation is deferred to ``exchanges/fees.py``
        in Phase 2, which wraps this method and adjusts the effective rate.

        Args:
            symbol: Symbol string from the engine — concatenated USDT pair
                (e.g. ``"BTCUSDT"``) or slash-delimited cross pair
                (e.g. ``"ETH/BTC"``).

        Returns:
            ``TradingFees`` with ``maker`` and ``taker`` as ``Decimal``
            fractions (e.g. ``Decimal("0.001")`` = 0.1 %).

        Raises:
            ccxt.BadSymbol: If the symbol is not recognised by Binance.
            ccxt.NetworkError: On connectivity failure.
        """
        if symbol in self._fee_cache:
            return self._fee_cache[symbol]

        # Convert from native (engine format) to unified (ccxt format)
        unified_symbol = _native_to_unified(symbol)

        loop = asyncio.get_event_loop()
        raw: dict = await loop.run_in_executor(
            None, self._client.fetch_trading_fee, unified_symbol
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

        **Symbol format**: accepts native format symbols (e.g. ``"BTCUSDT"``)
        and will convert to unified format internally for ccxt calls (see ADR-002).

        Args:
            symbol: Symbol string from the engine — concatenated USDT pair
                (e.g. ``"BTCUSDT"``) or slash-delimited cross pair
                (e.g. ``"ETH/BTC"``).
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

    def _load_markets(self) -> dict:
        """Load and cache the full market metadata from Binance.

        Calls ``ccxt.binance.load_markets()`` which returns a dict of all
        trading pairs with their filters (lot size, tick size, min notional,
        etc.).  The result is cached in ``self._markets_cache`` because market
        metadata rarely changes and the call costs one REST round-trip.

        Lazy caching pattern: the first call fetches from the exchange and
        stores the result; subsequent calls return the cached value directly.
        Using an instance attribute rather than ``@lru_cache`` ensures each
        adapter instance maintains its own independent cache, preventing
        cross-instance eviction when multiple adapters coexist (e.g. in tests
        or future multi-exchange setups).

        This is a synchronous helper intended to be called inside
        ``run_in_executor`` by async callers.

        Returns:
            Dict of ccxt market objects keyed by unified symbol
            (e.g. ``"BTC/USDT"``).
        """
        if self._markets_cache is None:
            self._markets_cache = self._client.load_markets()
        return self._markets_cache

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
