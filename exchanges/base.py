"""Exchange adapter interface for DeltaQuant.

Defines the ``ExchangeAdapter`` abstract base class that every exchange
integration must implement.  The trading engine (``core/graph.py``,
``core/evaluator.py``, ``core/executor.py``) depends exclusively on this
interface — never on a concrete adapter class.

Design rationale: see ADR-001 (docs/adr/ADR-001-exchange-adapter-interface.md).

Adding a new exchange means writing a new subclass of ``ExchangeAdapter``
and wiring it into ``main.py``.  No changes to the engine layer are needed,
because the engine never imports a concrete adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import AsyncIterator


# ── Data transfer objects ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class BookTicker:
    """Best bid/ask snapshot for a single trading pair.

    Attributes:
        symbol: Exchange-native symbol string, e.g. ``"BTCUSDT"``.
        bid: Best bid price at the time of the snapshot.
        ask: Best ask price at the time of the snapshot.
        timestamp_ms: Unix timestamp in milliseconds when this snapshot was
            received locally.  Used by the staleness check in the evaluator.
    """

    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp_ms: int


@dataclass(frozen=True)
class Balance:
    """Available and total balance for a single asset.

    Attributes:
        asset: Asset ticker, e.g. ``"USDT"`` or ``"BTC"``.
        free: Amount available for new orders (not locked in open orders).
        locked: Amount currently reserved by open orders.
    """

    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        """Return the sum of free and locked balance.

        Returns:
            Total balance as ``free + locked``.
        """
        return self.free + self.locked


@dataclass(frozen=True)
class TradingFees:
    """Maker/taker fee rates for a single trading pair.

    Rates are expressed as a fraction of the notional, e.g. ``Decimal("0.001")``
    represents 0.1 %.

    Attributes:
        symbol: Exchange-native symbol string, e.g. ``"BTCUSDT"``.
        maker: Fee rate applied when the order adds liquidity.
        taker: Fee rate applied when the order removes liquidity.
    """

    symbol: str
    maker: Decimal
    taker: Decimal


@dataclass
class OrderResult:
    """Result of a single FOK order placement attempt.

    Attributes:
        symbol: Symbol the order was placed on.
        order_id: Exchange-assigned order identifier.
        status: Final order status string as returned by the exchange
            (e.g. ``"FILLED"``, ``"EXPIRED"``).
        filled_qty: Quantity actually executed (``Decimal("0")`` if the
            order was cancelled or expired).
        avg_price: Volume-weighted average fill price.  ``Decimal("0")``
            if no fill occurred.
        fee: Total fee charged for this order.
        fee_asset: Asset in which the fee was charged (e.g. ``"BNB"``).
        raw: Raw response payload from the exchange, preserved for
            reconciliation and audit purposes.
    """

    symbol: str
    order_id: str
    status: str
    filled_qty: Decimal
    avg_price: Decimal
    fee: Decimal
    fee_asset: str
    raw: dict = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        """Return True if the order was fully filled.

        Returns:
            True when ``status`` is ``"FILLED"`` and ``filled_qty > 0``.
        """
        return self.status == "FILLED" and self.filled_qty > Decimal("0")


# ── Abstract base class ───────────────────────────────────────────────────────


class ExchangeAdapter(ABC):
    """Abstract interface for exchange connectivity.

    Every concrete adapter (e.g. ``BinanceAdapter``, future ``BybitAdapter``)
    must implement all abstract methods below.  The engine layer interacts
    exclusively with this interface, keeping exchange-specific logic fully
    encapsulated.

    Lifecycle:
        Concrete adapters may require an async initialisation step (e.g.
        opening a WebSocket connection, loading market metadata).  If so,
        they should implement ``__aenter__`` / ``__aexit__`` and be used as
        async context managers.  This base class does not mandate that pattern
        but does not preclude it either.

    Thread / concurrency safety:
        All methods are async.  Concrete implementations are expected to be
        used within a single ``asyncio`` event loop.  Sharing an adapter
        instance across multiple event loops is not supported.
    """

    # ── Market data ───────────────────────────────────────────────────────────

    @abstractmethod
    async def subscribe_book_ticker(
        self, symbols: list[str]
    ) -> AsyncIterator[BookTicker]:
        """Subscribe to best-bid/ask updates for the given symbols.

        Yields a ``BookTicker`` snapshot each time the exchange pushes an
        update for any of the subscribed symbols.  The iterator runs until
        the caller breaks out of it or the underlying connection is closed.

        This method is deliberately a *generator* (``AsyncIterator``) rather
        than a callback-based design so that callers can use ``async for``
        naturally and the back-pressure model is explicit.

        Args:
            symbols: List of exchange-native symbol strings to subscribe to,
                e.g. ``["BTCUSDT", "ETHBTC", "ETHUSDT"]``.

        Yields:
            ``BookTicker`` snapshots in the order they are received from the
            exchange.  No ordering guarantee across different symbols.

        Raises:
            ConnectionError: If the WebSocket connection cannot be established
                or is lost and cannot be recovered.
        """
        # The ``yield`` below makes this an abstract async generator.
        # Concrete implementations replace this body entirely.
        raise NotImplementedError
        yield  # pragma: no cover — makes the abstract method a generator

    @abstractmethod
    async def get_trading_fees(self, symbol: str) -> TradingFees:
        """Fetch the current maker/taker fee rates for a symbol.

        Results should be cached by concrete implementations where the
        exchange allows it (fee tiers change rarely).  The cache must be
        invalidated when the adapter detects a tier change.

        Args:
            symbol: Exchange-native symbol string, e.g. ``"BTCUSDT"``.

        Returns:
            ``TradingFees`` with the effective maker and taker rates,
            already accounting for any active discount (e.g. BNB fee
            discount on Binance).

        Raises:
            ValueError: If ``symbol`` is not recognised by the exchange.
        """

    # ── Account ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_balance(self, asset: str) -> Balance:
        """Fetch the current balance for a single asset.

        Args:
            asset: Asset ticker to query, e.g. ``"USDT"`` or ``"BTC"``.

        Returns:
            ``Balance`` with ``free`` and ``locked`` amounts for the asset.
            If the asset is not held, returns a ``Balance`` with both fields
            set to ``Decimal("0")``.

        Raises:
            PermissionError: If the API key does not have read-account
                permissions.
        """

    # ── Order placement ───────────────────────────────────────────────────────

    @abstractmethod
    async def place_fok_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        """Place a Fill-or-Kill limit order.

        FOK semantics: the order is either filled entirely at ``price`` or
        better, or immediately cancelled with zero fill.  There is no partial
        fill.  This property is what makes FOK suitable for the triangle legs:
        a partial fill would leave the bot with unplanned inventory.

        Live order placement is only allowed when the adapter has been
        intentionally configured for Binance TESTNET and the caller is not in
        ``DRY_RUN`` mode.  If the adapter is misconfigured, concrete
        implementations must fail closed rather than attempting a production
        order.

        Args:
            symbol: Exchange-native symbol string, e.g. ``"BTCUSDT"``.
            side: ``"BUY"`` or ``"SELL"`` (uppercase).
            quantity: Base-asset quantity to trade, expressed with the
                precision required by the exchange's lot-size filter.
            price: Limit price in quote-asset units, expressed with the
                exchange's tick-size precision.

        Returns:
            ``OrderResult`` describing the final state of the order.
            ``OrderResult.is_filled`` is the primary signal the executor uses
            to decide whether reconciliation is needed.

        Raises:
            ValueError: If ``side`` is not ``"BUY"`` or ``"SELL"``, or if
                ``quantity``/``price`` violate exchange filter rules.
            PermissionError: If the API key does not have trading permissions.
        """

    @abstractmethod
    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
    ) -> OrderResult:
        """Place a market order at the best available price.

        Market orders trade price certainty for execution certainty: there is
        no guaranteed fill price, but barring a fully empty book the order is
        expected to execute immediately.  That property makes market orders
        the correct tool for emergency reconciliation after a FOK leg already
        failed once due to movement in the top of book.

        Like ``place_fok_order``, live execution is only allowed when the
        adapter is explicitly configured for Binance TESTNET and the caller is
        not in ``DRY_RUN`` mode.  Misconfiguration must fail closed.

        Args:
            symbol: Exchange-native symbol string, e.g. ``"BTCUSDT"``.
            side: ``"BUY"`` or ``"SELL"`` (uppercase).
            quantity: Base-asset quantity to trade, expressed with the
                precision required by the exchange's market-lot filter.

        Returns:
            ``OrderResult`` describing the final state of the order.
            ``OrderResult.is_filled`` is still the primary success signal for
            the executor, even though market orders may return partial or
            unexpected exchange statuses in edge cases.

        Raises:
            ValueError: If ``side`` is invalid or ``quantity`` violates the
                exchange's filter rules.
            PermissionError: If live order placement is not enabled.
        """

    async def close(self) -> None:
        """Release underlying exchange connections and resources."""
        pass
