"""BNB fee discount wrapper around :class:`TradingFees`.

Binance offers a 25 % fee reduction (as of 2024) when trading fees are paid
in BNB rather than in the trade's quote asset.  Rather than baking that
multiplier into every caller of ``BinanceAdapter.get_trading_fees``, we
centralise the arithmetic here so that:

* The 25 % constant is defined in exactly one place (``BNB_DISCOUNT_RATE``).
* The multiplication is performed with ``Decimal`` arithmetic (not float),
  avoiding binary-rounding drift that would silently compound across
  hundreds of evaluated triangles.
* The evaluator (Phase 2 / ``core/evaluator.py``) consumes the discounted
  rate through a single stable interface instead of re-deriving it.

Symbol format contract (ADR-002):
    This module operates on already-fetched ``TradingFees`` objects; the
    symbol field is passed through unchanged so both USDT-concatenated and
    cross-pair slash-delimited symbols (``"BTCUSDT"``, ``"ETH/BTC"``) round-
    trip without modification.

Google-style docstrings per ``docs/planning/plan_doc_DQ.md`` §2.1.
"""

from __future__ import annotations

from decimal import Decimal

from exchanges.base import TradingFees


# ── Constants ────────────────────────────────────────────────────────────────

# As of late 2024 the default Binance BNB discount is 25 %.  Defined here so
# that the exact same factor is used by every caller; changing the number
# means touching this one constant, not grep-and-hoping in the evaluator.
#
# Value is exact as a Decimal fraction (1/4 = 25/100), so multiplication by
# it does not introduce rounding error into the result.
BNB_DISCOUNT_RATE: Decimal = Decimal("0.25")

# The multiplier applied to the raw (undiscounted) fee rate:
#   effective_rate = raw_rate * (1 - BNB_DISCOUNT_RATE)
# We precompute the complement once so callers don't re-derive it.
_BNB_MULTIPLIER: Decimal = Decimal("1") - BNB_DISCOUNT_RATE


# ── Public API ───────────────────────────────────────────────────────────────


def apply_bnb_discount(raw_fees: TradingFees) -> TradingFees:
    """Return a new ``TradingFees`` with the BNB 25 % discount applied.

    Arithmetic is performed with ``Decimal`` throughout, so the result is
    *exact* for any Binance-standard fee rate (all of which are small
    decimal fractions with ≤ 4 significant digits).

    The returned object preserves the ``symbol`` field from ``raw_fees``
    exactly — so engine-native concatenated-USDT symbols and slash-delimited
    cross-pair symbols both survive the round-trip unchanged.

    Args:
        raw_fees: Pre-fetched ``TradingFees`` with undiscounted maker/taker
            rates, typically from ``BinanceAdapter.get_trading_fees``.

    Returns:
        New ``TradingFees`` instance where::

            discounted_maker = raw_fees.maker * (1 - BNB_DISCOUNT_RATE)
            discounted_taker = raw_fees.taker * (1 - BNB_DISCOUNT_RATE)

        Both ``maker`` and ``taker`` on the returned instance are verified
        ``Decimal`` instances (never float).

    Raises:
        TypeError: If ``raw_fees.maker`` or ``raw_fees.taker`` is not a
            ``Decimal``.  Calling code is expected to fetch rates from the
            adapter (which always returns ``Decimal``); passing a float here
            is a contract violation and we fail loudly rather than silently
            lose precision.

    Examples:
        Hand-verified arithmetic for the standard 0.1 % / 0.1 % VIP-0 rates::

            raw = TradingFees("BTCUSDT", Decimal("0.001"), Decimal("0.001"))
            out = apply_bnb_discount(raw)
            # maker = 0.001 * 0.75 = 0.00075   (0.075 %)
            # taker = 0.001 * 0.75 = 0.00075   (0.075 %)
            assert out.maker == Decimal("0.00075")
            assert out.taker == Decimal("0.00075")

        And for a VIP-tier rate that would catch a hardcoded-0.00075 bug::

            raw = TradingFees("BTCUSDT", Decimal("0.00012"), Decimal("0.00012"))
            out = apply_bnb_discount(raw)
            # maker = 0.00012 * 0.75 = 0.00009   (0.009 %)
            assert out.maker == Decimal("0.00009")
    """
    if not isinstance(raw_fees.maker, Decimal):
        raise TypeError(
            f"raw_fees.maker must be Decimal, got "
            f"{type(raw_fees.maker).__name__}: {raw_fees.maker!r}"
        )
    if not isinstance(raw_fees.taker, Decimal):
        raise TypeError(
            f"raw_fees.taker must be Decimal, got "
            f"{type(raw_fees.taker).__name__}: {raw_fees.taker!r}"
        )

    discounted_maker: Decimal = raw_fees.maker * _BNB_MULTIPLIER
    discounted_taker: Decimal = raw_fees.taker * _BNB_MULTIPLIER

    return TradingFees(
        symbol=raw_fees.symbol,
        maker=discounted_maker,
        taker=discounted_taker,
    )


async def get_effective_fees(
    adapter,
    symbol: str,
    *,
    use_bnb_discount: bool = True,
) -> TradingFees:
    """Fetch raw fees from ``adapter`` and optionally apply the BNB discount.

    Thin convenience wrapper combining ``adapter.get_trading_fees(symbol)``
    and ``apply_bnb_discount``.  Useful for evaluator code that reads the
    ``Settings.USE_BNB_FEE_DISCOUNT`` flag once and passes it through.

    Args:
        adapter: Any object exposing ``async get_trading_fees(str)`` that
            returns a ``TradingFees`` (typically ``BinanceAdapter``).  We
            accept a structural type here so tests can inject fakes without
            importing the concrete adapter class.
        symbol: Engine-native symbol string (ADR-002 format) — passed
            straight through to ``adapter.get_trading_fees``.
        use_bnb_discount: When ``True`` (the default), wrap the fetched
            ``TradingFees`` through :func:`apply_bnb_discount`.  When
            ``False``, return the raw adapter result unmodified.

    Returns:
        ``TradingFees`` — either raw (no discount) or BNB-discounted,
        depending on ``use_bnb_discount``.
    """
    raw = await adapter.get_trading_fees(symbol)
    if use_bnb_discount:
        return apply_bnb_discount(raw)
    return raw
