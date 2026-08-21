"""Unit tests for exchanges/base.py — DTOs and abstract interface.

Covers:
    - Balance.total property (arithmetic and zero cases)
    - OrderResult.is_filled property (all four boolean combinations)
    - Frozen dataclass immutability for Balance, TradingFees, BookTicker
    - ExchangeAdapter ABC prevents direct instantiation
"""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from exchanges.base import (
    Balance,
    BookTicker,
    ExchangeAdapter,
    OrderResult,
    TradingFees,
)


# ── Balance ───────────────────────────────────────────────────────────────────


class TestBalance:
    def test_total_free_plus_locked(self):
        """total returns the sum of free and locked."""
        b = Balance(asset="USDT", free=Decimal("100"), locked=Decimal("25"))
        assert b.total == Decimal("125")

    def test_total_both_zero(self):
        """total is zero when both free and locked are zero."""
        b = Balance(asset="BTC", free=Decimal("0"), locked=Decimal("0"))
        assert b.total == Decimal("0")

    def test_total_locked_zero(self):
        """total equals free when locked is zero."""
        b = Balance(asset="ETH", free=Decimal("3.5"), locked=Decimal("0"))
        assert b.total == Decimal("3.5")

    def test_total_free_zero(self):
        """total equals locked when free is zero."""
        b = Balance(asset="BNB", free=Decimal("0"), locked=Decimal("7"))
        assert b.total == Decimal("7")

    def test_is_frozen(self):
        """Balance is a frozen dataclass — field assignment raises FrozenInstanceError."""
        b = Balance(asset="USDT", free=Decimal("10"), locked=Decimal("0"))
        with pytest.raises(FrozenInstanceError):
            b.free = Decimal("999")  # type: ignore[misc]


# ── OrderResult ───────────────────────────────────────────────────────────────


class TestOrderResult:
    """Tests for OrderResult.is_filled — all four combinations of the two conditions."""

    def _make(self, status: str, filled_qty: str) -> OrderResult:
        return OrderResult(
            symbol="BTCUSDT",
            order_id="1",
            status=status,
            filled_qty=Decimal(filled_qty),
            avg_price=Decimal("0"),
            fee=Decimal("0"),
            fee_asset="BNB",
        )

    def test_filled_status_nonzero_qty_is_true(self):
        """FILLED + filled_qty > 0 → is_filled is True."""
        assert self._make("FILLED", "1").is_filled is True

    def test_filled_status_zero_qty_is_false(self):
        """FILLED + filled_qty == 0 → is_filled is False (qty guard)."""
        assert self._make("FILLED", "0").is_filled is False

    def test_expired_status_nonzero_qty_is_false(self):
        """EXPIRED + filled_qty > 0 → is_filled is False (status guard)."""
        assert self._make("EXPIRED", "1").is_filled is False

    def test_expired_status_zero_qty_is_false(self):
        """EXPIRED + filled_qty == 0 → is_filled is False (both guards fail)."""
        assert self._make("EXPIRED", "0").is_filled is False

    def test_cancelled_status_is_false(self):
        """CANCELLED → is_filled is False regardless of qty."""
        assert self._make("CANCELLED", "5").is_filled is False

    def test_raw_defaults_to_empty_dict(self):
        """raw field defaults to an empty dict when not supplied."""
        result = self._make("FILLED", "1")
        assert result.raw == {}

    def test_is_mutable(self):
        """OrderResult is NOT frozen — status can be updated (used in reconciliation)."""
        result = self._make("EXPIRED", "0")
        result.status = "FILLED"
        result.filled_qty = Decimal("1")
        assert result.is_filled is True


# ── TradingFees ───────────────────────────────────────────────────────────────


class TestTradingFees:
    def test_construction(self):
        """TradingFees stores symbol, maker, taker correctly."""
        fees = TradingFees(
            symbol="BTCUSDT",
            maker=Decimal("0.001"),
            taker=Decimal("0.001"),
        )
        assert fees.symbol == "BTCUSDT"
        assert fees.maker == Decimal("0.001")
        assert fees.taker == Decimal("0.001")

    def test_is_frozen(self):
        """TradingFees is a frozen dataclass."""
        fees = TradingFees(symbol="BTCUSDT", maker=Decimal("0.001"), taker=Decimal("0.001"))
        with pytest.raises(FrozenInstanceError):
            fees.maker = Decimal("0")  # type: ignore[misc]


# ── BookTicker ────────────────────────────────────────────────────────────────


class TestBookTicker:
    def test_construction(self):
        """BookTicker stores all fields correctly."""
        bt = BookTicker(
            symbol="BTCUSDT",
            bid=Decimal("67000"),
            ask=Decimal("67001"),
            timestamp_ms=1_700_000_000_000,
        )
        assert bt.symbol == "BTCUSDT"
        assert bt.bid == Decimal("67000")
        assert bt.ask == Decimal("67001")
        assert bt.timestamp_ms == 1_700_000_000_000

    def test_is_frozen(self):
        """BookTicker is a frozen dataclass."""
        bt = BookTicker(symbol="ETHUSDT", bid=Decimal("3000"), ask=Decimal("3001"), timestamp_ms=0)
        with pytest.raises(FrozenInstanceError):
            bt.bid = Decimal("0")  # type: ignore[misc]


# ── ExchangeAdapter ABC ───────────────────────────────────────────────────────


class TestExchangeAdapterABC:
    def test_cannot_instantiate_directly(self):
        """ExchangeAdapter is abstract — direct instantiation raises TypeError."""
        with pytest.raises(TypeError):
            ExchangeAdapter()  # type: ignore[abstract]

    def test_concrete_subclass_missing_method_cannot_instantiate(self):
        """A subclass that omits any abstract method also cannot be instantiated."""
        class IncompleteAdapter(ExchangeAdapter):
            # only implements one of the four abstract methods
            async def get_trading_fees(self, symbol):
                pass

        with pytest.raises(TypeError):
            IncompleteAdapter()
