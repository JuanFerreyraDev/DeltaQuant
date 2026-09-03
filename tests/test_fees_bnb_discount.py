"""Probing tests for exchanges/fees.py BNB discount calculation.

Every test below targets a specific plausible-but-wrong implementation.
Arithmetic values are verified by hand in each test's docstring — the user
forbids "magic numbers that just clear a threshold because they're big".

Plausible bugs each test is designed to catch:
  B1. apply_bnb_discount uses float 0.75 instead of Decimal("0.75").
      → Caught by: exact Decimal equality + isinstance check on return values.
  B2. Discount is *added* (rate * 1.25) instead of subtracted (rate * 0.75).
      → Caught by: value equality assertions on known inputs.
  B3. Discount applied only to maker, not taker (or vice versa).
      → Caught by: testing BOTH fields against hand-verified values.
  B4. Values hardcoded to 0.00075 (standard 0.1% → 0.075%) instead of
      actually computing rate * 0.75.
      → Caught by: tests with NON-STANDARD fee rates (VIP tier values).
  B5. apply_bnb_discount silently accepts float inputs and returns floats.
      → Caught by: explicit isinstance(result.maker, Decimal) +
         TypeError precondition on float inputs.
  B6. get_effective_fees ignores use_bnb_discount=False and always discounts.
      → Caught by: the passthrough test comparing fetched vs returned identity.
"""

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from exchanges.base import TradingFees
from exchanges.fees import (
    BNB_DISCOUNT_RATE,
    apply_bnb_discount,
    get_effective_fees,
)


# ── apply_bnb_discount ───────────────────────────────────────────────────────


class TestApplyBnbDiscount:
    """Hand-verified arithmetic tests for ``apply_bnb_discount``.

    Each test case below documents the EXACT pencil-and-paper calculation
    so that a reviewer can independently confirm the expected value without
    running the code.  No "magic" numbers.
    """

    # ── Hand-verified: standard VIP-0 rates ────────────────────────────────

    def test_standard_vip0_maker_taker_0_1_percent(self):
        """Standard 0.1% / 0.1% rates → 0.075% each.

        Hand arithmetic:
            maker_raw       = 0.001   (0.1 %)
            taker_raw       = 0.001   (0.1 %)
            BNB discount    = 25 %    = 0.25
            multiplier      = 1 - 0.25 = 0.75
            maker_discounted = 0.001 * 0.75 = 0.00075   (0.075 %)
            taker_discounted = 0.001 * 0.75 = 0.00075   (0.075 %)

        This test alone would pass against bug **B4** (hardcoded 0.00075).
        It needs the non-standard rate tests (below) to catch that bug class.
        """
        raw = TradingFees(symbol="BTCUSDT", maker=Decimal("0.001"), taker=Decimal("0.001"))
        out = apply_bnb_discount(raw)

        # Value check (Decimal exact equality, NOT approximate)
        assert out.maker == Decimal("0.00075"), (
            f"Expected 0.00075 (0.075%), got {out.maker}. "
            "Bug suspects: B2 (discount added instead of subtracted) "
            "or B1 (float multiplier used)."
        )
        assert out.taker == Decimal("0.00075")

        # ── Decimal type check (catches B1, B5) ──────────────────────
        assert isinstance(out.maker, Decimal), (
            f"maker has type {type(out.maker).__name__}, expected Decimal. "
            "Bug B1/B5: float multiplication leaked into the result."
        )
        assert isinstance(out.taker, Decimal)

        # ── Symbol passthrough (catches symbol-mutate regression) ────
        assert out.symbol == "BTCUSDT"

    # ── Hand-verified: non-standard rates (catching B4 hardcode) ────────────

    def test_vip_tier_rate_0_012_percent_maker(self):
        """Maker=0.012% → 0.009%; Taker=0.012% → 0.009%.

        This test specifically catches bug **B4** (hardcoded 0.00075).
        If someone replaced rate * 0.75 with a literal 0.00075, the
        standard-rate test above would still pass but THIS test would
        fail because 0.00009 ≠ 0.00075.

        Hand arithmetic (in basis points for clarity):
            raw_maker_rate  = 0.012 % = 1.2 bps = Decimal("0.00012")
            multiplier      = 0.75
            discounted      = 0.00012 * 0.75
                            = 0.00009      ← 0.009 % = 0.9 bps
        """
        raw = TradingFees(
            symbol="ETH/BTC",
            maker=Decimal("0.00012"),
            taker=Decimal("0.00012"),
        )
        out = apply_bnb_discount(raw)
        # NOT 0.00075. A hardcoded impl would give 0.00075, not 0.00009.
        assert out.maker == Decimal("0.00009"), (
            "Non-standard VIP rate failed. If the VIP-0 test passes but this "
            "fails, suspect bug B4 (return value hardcoded to 0.00075)."
        )
        assert out.taker == Decimal("0.00009")
        assert isinstance(out.maker, Decimal)
        # Cross pair symbol (ADR-002 slash-delimited) rounds through unchanged.
        assert out.symbol == "ETH/BTC"

    def test_dissimilar_maker_taker_rates(self):
        """Different maker/taker rates apply discount independently to each.

        Catches bug **B3**: discount applied only to one side.

        Hand arithmetic:
            maker_raw  = 0.002   (0.2 % = 20 bps)  → 0.002 * 0.75 = 0.0015  (15 bps)
            taker_raw  = 0.0004  (0.04 % = 4 bps)  → 0.0004 * 0.75 = 0.0003 (3 bps)
            maker ≠ taker after discount, so applying the same value to both
            would clearly be wrong.
        """
        raw = TradingFees(
            symbol="BTCUSDT",
            maker=Decimal("0.002"),
            taker=Decimal("0.0004"),
        )
        out = apply_bnb_discount(raw)
        assert out.maker == Decimal("0.0015"), (
            "Maker 0.2% → 0.15% = 0.0015. "
            "Suspect B3: taker value was pasted into maker field."
        )
        assert out.taker == Decimal("0.0003"), (
            "Taker 0.04% → 0.03% = 0.0003. "
            "Suspect B3: maker value was pasted into taker field."
        )
        # Critical: maker must NOT equal taker after discount.
        assert out.maker != out.taker, (
            "Maker and taker started different (0.002 vs 0.0004); "
            "must end different. Bug B3 (one side overwrote the other)."
        )

    # ── TypeError preconditions (catches B5 float inputs) ──────────────────

    def test_float_maker_raises_typeerror(self):
        """Passing a float maker rate raises TypeError.

        Catches a pipeline where a caller accidentally produces float rates
        (e.g. from a JSON parse) and passes them in.  Silently coercing
        float → Decimal would lose precision (0.1 as float = 0.10000000000...0555...).
        """
        raw = TradingFees(
            symbol="BTCUSDT",
            maker=0.001,  # BUG: float, not Decimal
            taker=Decimal("0.001"),
        )
        with pytest.raises(TypeError, match="maker must be Decimal"):
            apply_bnb_discount(raw)

    def test_float_taker_raises_typeerror(self):
        """Float taker rate raises TypeError (symmetric check with maker)."""
        raw = TradingFees(
            symbol="BTCUSDT",
            maker=Decimal("0.001"),
            taker=0.001,  # BUG: float, not Decimal
        )
        with pytest.raises(TypeError, match="taker must be Decimal"):
            apply_bnb_discount(raw)

    # ── Structural invariant: BNB_DISCOUNT_RATE constant is 25% ────────────

    def test_bnb_discount_rate_is_25_percent(self):
        """``BNB_DISCOUNT_RATE`` is exactly 0.25 and is a Decimal.

        If someone changes the constant thinking "it's a config, set it to
        0.1 for experiment X" without going through docs/calibrations.md,
        this test will fail and remind them that every calibration change
        must be documented per the Phase 2 documentation checklist.
        """
        assert isinstance(BNB_DISCOUNT_RATE, Decimal), (
            "BNB_DISCOUNT_RATE must be Decimal (not float) so the precomputed "
            "multiplier _BNB_MULTIPLIER is exact in Decimal arithmetic."
        )
        assert BNB_DISCOUNT_RATE == Decimal("0.25"), (
            "BNB discount rate is 25% (Binance standard as of 2024). "
            "If changing this for an experiment, update docs/calibrations.md "
            "AND this test's expected value together."
        )

    def test_zero_fee_remains_zero(self):
        """Zero maker/taker rates → zero after discount (no NaN/inf surprises).

        Edge case: zero-fee promotional pairs (e.g. TRY/BTC sometimes) are
        handled cleanly.  Hand arithmetic: 0 * anything = 0.
        """
        raw = TradingFees(symbol="XYZUSDT", maker=Decimal("0"), taker=Decimal("0"))
        out = apply_bnb_discount(raw)
        assert out.maker == Decimal("0")
        assert out.taker == Decimal("0")
        assert isinstance(out.maker, Decimal)


# ── get_effective_fees (structural adapter wrapper) ──────────────────────────


class _FakeAdapter:
    """Minimal structural fake of BinanceAdapter for get_effective_fees test.

    We do NOT import the real BinanceAdapter here — structural typing lets us
    verify the wrapper's branching logic without needing a ccxt client.
    """

    def __init__(self, fee_map: dict[str, TradingFees]):
        self._fee_map = fee_map
        self.calls: list[str] = []

    async def get_trading_fees(self, symbol: str) -> TradingFees:
        self.calls.append(symbol)
        return self._fee_map[symbol]


class TestGetEffectiveFees:
    """Branching tests for the convenience wrapper ``get_effective_fees``.

    Target bugs:
      B6. ``use_bnb_discount`` flag is ignored (discount always applied).
      B7. The wrong symbol is passed to ``adapter.get_trading_fees``.
      B8. ``adapter.get_trading_fees`` is called more than once per wrapper
          call (double-fetching the same data wastes rate-limit weight).
    """

    def _adapter(self) -> _FakeAdapter:
        return _FakeAdapter({
            "BTCUSDT": TradingFees(
                symbol="BTCUSDT",
                maker=Decimal("0.001"),
                taker=Decimal("0.001"),
            ),
            "ETH/BTC": TradingFees(
                symbol="ETH/BTC",
                maker=Decimal("0.0005"),
                taker=Decimal("0.0005"),
            ),
        })

    @pytest.mark.asyncio
    async def test_use_bnb_discount_true_applies_discount(self):
        """Default flag → discounted result (same hand math as VIP-0 above)."""
        fake = self._adapter()
        out = await get_effective_fees(fake, "BTCUSDT", use_bnb_discount=True)
        assert out.maker == Decimal("0.00075")
        assert out.taker == Decimal("0.00075")
        assert isinstance(out.maker, Decimal)
        assert len(fake.calls) == 1, "get_trading_fees called more than once (B8)"
        assert fake.calls[0] == "BTCUSDT", (
            "Wrong symbol passed to adapter (B7). "
            f"Expected 'BTCUSDT', got '{fake.calls[0]}'."
        )

    @pytest.mark.asyncio
    async def test_use_bnb_discount_false_passthrough_raw(self):
        """Flag=False → raw adapter result returned UNMODIFIED.

        Catches bug **B6** (discount always applied).  A plausible wrong impl
        ignores the flag and just calls apply_bnb_discount unconditionally.
        With the raw VIP-0 maker = 0.001, an unconditionally-discounted impl
        would return 0.00075 and this test fails.
        """
        fake = self._adapter()
        out = await get_effective_fees(fake, "BTCUSDT", use_bnb_discount=False)
        # Must be exactly the RAW 0.001, NOT the discounted 0.00075.
        assert out.maker == Decimal("0.001"), (
            "use_bnb_discount=False but discount was applied. Bug B6: flag ignored."
        )
        assert out.taker == Decimal("0.001")
        assert len(fake.calls) == 1

    @pytest.mark.asyncio
    async def test_cross_pair_slash_symbol_passthrough(self):
        """Cross pair symbol (ETH/BTC) is passed to adapter and returned unchanged.

        Engine produces ETH/BTC in slash-delimited format (ADR-002).  The
        wrapper must pass it through to the adapter and not mutate the
        symbol on the returned TradingFees.
        """
        fake = self._adapter()
        out = await get_effective_fees(fake, "ETH/BTC")
        assert fake.calls == ["ETH/BTC"], (
            "Cross-pair symbol was mutated before reaching the adapter."
        )
        # Hand arithmetic: 0.0005 * 0.75 = 0.000375
        assert out.maker == Decimal("0.000375")
        assert out.symbol == "ETH/BTC"
