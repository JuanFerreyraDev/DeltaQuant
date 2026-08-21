"""Unit tests for the symbol-format helpers in exchanges/binance_adapter.py.

These helpers are pure functions with no network calls or side effects.
They enforce the symbol format contract described in ADR-002:

    - Engine produces USDT-quoted pairs in concatenated native format (BTCUSDT)
      and cross pairs in slash-delimited unified format (ETH/BTC).
    - The adapter converts native → unified for ccxt calls (_native_to_unified)
      and unified → native for engine-facing results (_unified_to_native).

Covers:
    - _native_to_unified: USDT pair, cross pair passthrough, fallback path,
      boundary cases
    - _unified_to_native: happy path, no-slash no-op, empty string
"""

import pytest

from exchanges.binance_adapter import _native_to_unified, _unified_to_native


# ── _native_to_unified ────────────────────────────────────────────────────────


class TestNativeToUnified:
    """Tests for _native_to_unified(native_symbol) -> str."""

    def test_usdt_pair_standard(self):
        """Concatenated USDT pair is split at the USDT suffix."""
        assert _native_to_unified("BTCUSDT") == "BTC/USDT"

    def test_usdt_pair_multi_char_base(self):
        """Multi-character base assets are handled correctly."""
        assert _native_to_unified("ETHUSDT") == "ETH/USDT"
        assert _native_to_unified("BNBUSDT") == "BNB/USDT"

    def test_slash_delimited_passthrough(self):
        """Slash-delimited symbols (already unified) are returned unchanged."""
        assert _native_to_unified("ETH/BTC") == "ETH/BTC"
        assert _native_to_unified("BTC/USDT") == "BTC/USDT"

    def test_non_usdt_concatenated_fallthrough(self):
        """A concatenated non-USDT symbol has no delimiter and no USDT suffix.
        The function returns it unchanged (the fallback path). The ccxt call
        will raise BadSymbol — that is the correct failure mode per ADR-002,
        and is better than a silent wrong split.
        """
        assert _native_to_unified("ETHBTC") == "ETHBTC"

    def test_bare_usdt_length_guard(self):
        """'USDT' alone (len == 4) does not enter the USDT-suffix branch.
        len('USDT') > 4 is False, so the symbol falls through to the
        passthrough return. The ccxt call will fail, which is correct.
        """
        assert _native_to_unified("USDT") == "USDT"

    def test_minimum_valid_usdt_pair(self):
        """A 5-character USDT pair (1-char base) is split correctly."""
        # e.g. hypothetical single-char base
        assert _native_to_unified("XUSDT") == "X/USDT"

    def test_already_unified_with_non_usdt_quote(self):
        """Unified cross pairs pass through unchanged regardless of quote asset."""
        assert _native_to_unified("BNB/ETH") == "BNB/ETH"
        assert _native_to_unified("XRP/BTC") == "XRP/BTC"


# ── _unified_to_native ────────────────────────────────────────────────────────


class TestUnifiedToNative:
    """Tests for _unified_to_native(unified_symbol) -> str.

    This function is only safe for USDT-quoted symbols per ADR-002.
    """

    def test_usdt_pair(self):
        """BTC/USDT → BTCUSDT."""
        assert _unified_to_native("BTC/USDT") == "BTCUSDT"

    def test_multi_char_base(self):
        """Multi-character base pairs are concatenated correctly."""
        assert _unified_to_native("ETH/USDT") == "ETHUSDT"
        assert _unified_to_native("BNB/USDT") == "BNBUSDT"

    def test_no_slash_is_no_op(self):
        """A symbol already in native format (no slash) is returned unchanged."""
        assert _unified_to_native("BTCUSDT") == "BTCUSDT"

    def test_empty_string(self):
        """Empty string in → empty string out (no error)."""
        assert _unified_to_native("") == ""

    def test_cross_pair_strips_slash(self):
        """For completeness: slash removal applies even to cross pairs.
        Callers must NOT use this on cross pairs per ADR-002 — the result
        is ambiguous — but documenting the actual behavior is useful.
        """
        # ETH/BTC → ETHBTC (ambiguous, documented as out-of-scope for this fn)
        assert _unified_to_native("ETH/BTC") == "ETHBTC"
