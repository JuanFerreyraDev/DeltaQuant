"""Binance exchange adapter — REST + WebSocket bookTicker (Phase 2).

Implements ``ExchangeAdapter`` for Binance using ``ccxt`` (REST) and
``ccxt.pro`` (WebSocket).  WebSocket bookTicker subscription is implemented
in this phase; FOK order placement is deferred to Phase 3.

Phase scope (per the technical plan, §8 Fase 2):
    - ``subscribe_book_ticker``: top-of-book (best bid/ask) push updates via
      ``ccxt.pro``'s ``watch_bids_asks`` stream.
    - Rate-limit weight tracking: log weight consumed per request from the
      first WS session onward (technical plan §9.3).
    - Volume, fees, and balance methods from Phase 1 are unchanged.

Out of scope (explicitly deferred):
    - ``place_fok_order``: order execution (Phase 3).

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

Rate-limit weight tracking (§9.3):
    After every REST call, the consumed 1-minute weight is extracted from
    response headers (``X-MBX-USED-WEIGHT-1M``) and emitted via the same
    logging path as WS connection events.  Weight is logged proactively, not
    reactively after a ban.

Security:
    API keys are read from ``Settings`` at construction time.  No key or
    secret is ever stored as a plain module-level variable or logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import AsyncIterator, Optional

import ccxt
import ccxt.pro as ccxt_pro

from config.settings import Settings
from exchanges.base import (
    Balance,
    BookTicker,
    ExchangeAdapter,
    OrderResult,
    TradingFees,
)

logger = logging.getLogger(__name__)


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


# ── Rate-limit weight tracking (technical plan §9.3) ────────────────────────


def _extract_weight_from_last_response(client: ccxt.binance) -> Optional[int]:
    """Extract the consumed 1-minute weight from a ccxt client's last HTTP response.

    Binance returns cumulative weight consumed in the current 1-minute window via
    the ``X-MBX-USED-WEIGHT-1M`` response header.  This helper grabs it from
    ``client.last_http_response.headers`` when available, returning ``None`` if
    the header is absent (e.g. first call, or no HTTP round-trip happened yet).

    Args:
        client: A ccxt REST client whose ``last_http_response`` attribute may
            contain the most recent response headers.

    Returns:
        Integer weight value if the header exists, otherwise ``None``.
    """
    try:
        resp = getattr(client, "last_http_response", None)
        if resp is None:
            return None
        headers = getattr(resp, "headers", None)
        if headers is None:
            return None
        raw = headers.get("X-MBX-USED-WEIGHT-1M")
        if raw is None:
            return None
        return int(raw)
    except Exception:
        return None


def _log_weight(client: ccxt.binance, operation: str) -> None:
    """Log the current rate-limit weight after a REST operation.

    Silently no-ops if the weight header cannot be extracted; never raises.

    Args:
        client: ccxt REST client (source of ``last_http_response``).
        operation: Human-readable operation name for the log message, e.g.
            ``"fetch_tickers_24h"``.
    """
    weight = _extract_weight_from_last_response(client)
    if weight is not None:
        logger.info(
            "binance_weight op=%s weight_1m=%s", operation, weight
        )


class BinanceAdapter(ExchangeAdapter):
    """Binance implementation of ``ExchangeAdapter``.

    Wraps ``ccxt.binance`` for REST access and ``ccxt.pro.binance`` for
    WebSocket bookTicker pushes.  Instantiate via the async factory
    ``BinanceAdapter.create(settings)`` rather than the constructor directly.

    Attributes:
        _client: Underlying ``ccxt.binance`` REST client instance.
        _ws_client: Underlying ``ccxt.pro.binance`` async WebSocket client,
            constructed lazily on the first call to ``subscribe_book_ticker`` so
            that tests that don't exercise WS don't need a live connection.
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

    def __init__(
        self,
        client: ccxt.binance,
        settings: Settings,
        ws_client: Optional[ccxt_pro.binance] = None,
    ) -> None:
        """Initialise the adapter with already-constructed ccxt clients.

        Prefer ``BinanceAdapter.create(settings)`` over calling this directly.

        Args:
            client: A configured ``ccxt.binance`` REST instance.
            settings: Validated ``Settings`` object from ``config.settings``.
            ws_client: Optional pre-built ``ccxt.pro.binance`` instance.  If
                ``None``, one is constructed lazily on the first WS call.
        """
        self._client: ccxt.binance = client
        self._ws_client: Optional[ccxt_pro.binance] = ws_client
        self._settings: Settings = settings
        self._fee_cache: dict[str, TradingFees] = {}
        self._markets_cache: dict | None = None

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def create(cls, settings: Settings) -> "BinanceAdapter":
        """Async factory: construct and return a ready ``BinanceAdapter``.

        Builds the ``ccxt.binance`` REST client with the credentials from
        ``settings``, then runs a real connectivity and authentication check
        by calling ``load_markets()`` off the event loop.  This surfaces
        invalid credentials (``ccxt.AuthenticationError``) and network
        failures (``ccxt.NetworkError``) at startup rather than on the first
        operational request.

        The ``ccxt.pro`` WebSocket client is NOT opened here.  It is
        constructed lazily on the first call to ``subscribe_book_ticker`` so
        that non-WS callers (e.g. tests, the volume-filter REST-only phase) pay no
        WS connection overhead.

        Args:
            settings: Validated ``Settings`` loaded from the environment.

        Returns:
            A fully initialised ``BinanceAdapter`` ready to serve requests.
            ``_markets_cache`` is pre-populated; the first call to
            ``get_markets()`` returns immediately without a network round-trip.

        Raises:
            ccxt.AuthenticationError: If the API key or secret is invalid.
            ccxt.NetworkError: If Binance is unreachable.
        """
        loop = asyncio.get_running_loop()

        def _build_and_load() -> ccxt.binance:
            client = ccxt.binance(
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
            # load_markets() makes a real REST request to Binance.
            # This is the connectivity + credential check: an invalid API key
            # or unreachable host raises here, not on the first operational call.
            client.load_markets()
            return client

        client = await loop.run_in_executor(None, _build_and_load)
        _log_weight(client, "create/load_markets")
        adapter = cls(client, settings, ws_client=None)
        # Pre-populate the instance cache from the already-loaded markets so
        # the first get_markets() call is free.
        adapter._markets_cache = client.markets
        return adapter

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
        loop = asyncio.get_running_loop()
        raw: dict[str, dict] = await loop.run_in_executor(
            None, self._client.fetch_tickers
        )
        _log_weight(self._client, "fetch_tickers_24h")
        return list(raw.values())

    async def subscribe_book_ticker(
        self, symbols: list[str]
    ) -> AsyncIterator[BookTicker]:
        """Subscribe to best-bid/ask (top-of-book) WebSocket updates.

        Uses ``ccxt.pro``'s ``watch_bids_asks`` stream, which maps 1-to-1 to
        Binance's ``bookTicker`` WebSocket endpoint — the cheapest and most
        efficient way to receive push updates of the best bid/ask price every
        time either changes.

        **Symbol format**: accepts the mixed native format defined by ADR-002:
        USDT-quoted pairs arrive concatenated (e.g. ``"BTCUSDT"``) and cross
        pairs arrive slash-delimited (e.g. ``"ETH/BTC"``).  Both sub-formats
        are translated through ``_native_to_unified`` before being handed to
        ccxt.pro, which expects unified ``"BASE/QUOTE"`` strings.

        **Payload semantics & Staleness correctness**: On each WebSocket push,
        ``ccxt.pro.binance.watch_bids_asks(symbols)`` returns **only** the symbol(s)
        whose price actually changed in that push message (verified directly in
        ``ccxt.pro.binance`` source: ``watch_bids_asks`` -> ``watch_multi_ticker_helper``
        -> ``handle_tickers_and_bids_asks``).  Symbols whose prices did not change
        are omitted from the returned payload, so their cached ``BookTicker.timestamp_ms``
        in the evaluator correctly stops advancing, allowing the staleness check
        to accurately flag un-updated market legs.

        **Staleness semantics**: the yielded ``BookTicker.timestamp_ms`` is
        populated locally with ``time.time_ns() // 1_000_000`` at the moment
        the message is received from the WebSocket.  This is intentionally a
        *local-receive* timestamp, not the exchange-emitted timestamp, because
        the staleness check in the evaluator is a defence against *local*
        data-freshness loss (disconnected socket, ccxt.pro queue backup, etc.)
        — exchange-side clock skew is irrelevant to that goal.

        **Testability & Error handling semantics**: The ``except StopAsyncIteration``
        branch exists solely to make this generator deterministically testable in
        the test suite (allowing mocks to signal end of stream).  It does NOT
        correspond to any exchange or ccxt.pro disconnection event.  Real network
        disconnections or WebSocket crashes in production trigger ``except Exception``,
        which logs an ``ERROR`` message and raises ``ConnectionError``.

        Args:
            symbols: List of native-format symbol strings to subscribe to,
                e.g. ``["BTCUSDT", "ETH/BTC", "ETHUSDT"]``.  Empty list is
                accepted but yields nothing.

        Yields:
            ``BookTicker`` snapshot each time the exchange pushes an update
            for any of the subscribed symbols.  The ``symbol`` field on the
            yielded dataclass is in *engine-native* format (the same shape
            that was passed in) so downstream consumers never need to know
            about ccxt unified notation.

        Raises:
            ConnectionError: If the underlying ccxt.pro WebSocket cannot be
                established or is terminated fatally after retries exhaust.
        """
        if not symbols:
            return

        # ── Step 1: build a bidirectional symbol mapping ────────────────────
        native_to_unified: dict[str, str] = {}
        unified_to_native: dict[str, str] = {}
        for native in symbols:
            unified = _native_to_unified(native)
            native_to_unified[native] = unified
            unified_to_native[unified] = native
        unified_symbols = list(native_to_unified.values())

        # ── Step 2: ensure a ccxt.pro client is ready ───────────────────────

        if self._ws_client is None:
            self._ws_client = ccxt_pro.binance(
                {
                    "enableRateLimit": True,
                    "options": {
                        "defaultType": "spot",
                        "adjustForTimeDifference": True,
                    },
                }
            )
            logger.info(
                "binance_ws connect symbols=%s",
                len(unified_symbols),
            )

        ws = self._ws_client

        # ── Step 3: stream updates forever (until caller breaks or stream closes) ─

        try:
            while True:
                try:
                    payload: dict = await ws.watch_bids_asks(unified_symbols)
                except StopAsyncIteration:
                    # Test harness loop termination signal.  Not a real exchange disconnection.
                    logger.warning(
                        "binance_ws stream closed by test harness / iterator for symbols=%s",
                        len(unified_symbols),
                    )
                    return
                except Exception as exc:  # pragma: no cover — network path
                    # Real exchange connection failure path in production.
                    logger.error("binance_ws connection error: %s", exc)
                    raise ConnectionError(
                        f"Binance bookTicker WS failed: {exc}"
                    ) from exc

                recv_ts_ms = time.time_ns() // 1_000_000

                for unified, tick in payload.items():
                    native = unified_to_native.get(unified)
                    if native is None:
                        continue

                    # ccxt >=4.5 returns flat 'bid'/'ask' floats from
                    # watch_bids_asks; earlier versions used nested
                    # 'bids'/'asks' arrays.  Read flat fields first,
                    # fall back to nested arrays for test compatibility.
                    best_bid_price = tick.get("bid")
                    best_ask_price = tick.get("ask")

                    if best_bid_price is None or best_ask_price is None:
                        bids = tick.get("bids") or []
                        asks = tick.get("asks") or []
                        if not bids or not asks:
                            continue
                        best_bid_price = bids[0][0]
                        best_ask_price = asks[0][0]

                    yield BookTicker(
                        symbol=native,
                        bid=Decimal(str(best_bid_price)),
                        ask=Decimal(str(best_ask_price)),
                        timestamp_ms=recv_ts_ms,
                    )
        finally:
            logger.warning(
                "binance_ws stream exited/terminated for symbols=%s",
                len(unified_symbols),
            )
            # Reset ws client so next subscribe_book_ticker call creates
            # a fresh ccxt.pro instance instead of reusing a potentially
            # dirty connection after a server-side disconnect (code 1006).
            if self._ws_client is not None:
                try:
                    await self._ws_client.close()
                except Exception:
                    pass
                self._ws_client = None

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

        loop = asyncio.get_running_loop()
        raw: dict = await loop.run_in_executor(
            None, self._client.fetch_trading_fee, unified_symbol
        )
        _log_weight(self._client, f"fetch_trading_fee:{symbol}")

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
        loop = asyncio.get_running_loop()
        raw: dict = await loop.run_in_executor(None, self._client.fetch_balance)
        _log_weight(self._client, "fetch_balance")

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
        loop = asyncio.get_running_loop()
        markets = await loop.run_in_executor(None, self._load_markets)
        _log_weight(self._client, "load_markets")
        return markets
