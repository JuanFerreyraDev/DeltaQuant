"""Unit tests for core/graph.py — volume filtering and triangle generation.

Coverage goals (per the Phase 1 checklist):
    - No duplicate triangles are generated, regardless of the order in which
      pairs are presented or the direction a triangle is discovered.
    - Volume filter edge cases:
        * empty pair list → empty result
        * all pairs below threshold → empty result
        * exactly at threshold (not strictly above) → filtered out
        * mixed pairs: some above, some below → only above threshold returned
        * non-USDT-quoted pairs are ignored even if volume is high
        * malformed / missing quoteVolume → treated as zero (filtered out)
    - Triangle generation edge cases:
        * fewer than 3 pairs → empty result
        * three pairs that don't form a closed triangle → empty result
        * minimal valid triangle (exactly 3 pairs) → exactly 1 triangle
        * same triangle discoverable via multiple paths → still 1 triangle
        * larger realistic graph with known triangle count → correct count

No real API calls are made.  All test data uses dummy symbols and
fabricated volume figures.  No credentials appear anywhere in this file.
"""

from decimal import Decimal

import pytest

from core.graph import (
    Triangle,
    TradingPair,
    filter_pairs_by_volume,
    generate_triangles,
    parse_symbol,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_ticker(symbol: str, quote_volume: float | None) -> dict:
    """Build a minimal ccxt-style ticker dict for testing.

    Args:
        symbol: Unified ccxt symbol, e.g. ``"BTC/USDT"``.
        quote_volume: 24-hour quote-asset volume, or ``None`` to simulate
            a missing field.

    Returns:
        Dict with ``"symbol"`` and optionally ``"quoteVolume"`` keys.
    """
    d: dict = {"symbol": symbol}
    if quote_volume is not None:
        d["quoteVolume"] = quote_volume
    return d


def _make_pair(base: str, quote: str, volume: float) -> TradingPair:
    """Construct a TradingPair directly for generate_triangles tests.

    Args:
        base: Base asset ticker, e.g. ``"BTC"``.
        quote: Quote asset ticker, e.g. ``"USDT"``.
        volume: Volume to assign as ``volume_usdt``.

    Returns:
        A ``TradingPair`` instance with a synthetic native symbol.
    """
    return TradingPair(
        symbol=f"{base}{quote}",
        base=base,
        quote=quote,
        volume_usdt=Decimal(str(volume)),
    )


# ── parse_symbol ──────────────────────────────────────────────────────────────


class TestParseSymbol:
    """Tests for the symbol-string parsing helper."""

    def test_unified_slash_format(self):
        """Slash-delimited ccxt unified symbols are parsed correctly."""
        assert parse_symbol("BTC/USDT") == ("BTC", "USDT")

    def test_unified_slash_format_lowercased(self):
        """Input is uppercased regardless of the original case."""
        assert parse_symbol("eth/usdt") == ("ETH", "USDT")

    def test_native_usdt_suffix(self):
        """Native Binance symbols ending in USDT are split at the suffix."""
        assert parse_symbol("BTCUSDT") == ("BTC", "USDT")

    def test_native_multi_char_base(self):
        """Multi-character base assets are handled correctly."""
        assert parse_symbol("ETHUSDT") == ("ETH", "USDT")
        assert parse_symbol("BNBUSDT") == ("BNB", "USDT")

    def test_non_usdt_native_returns_none(self):
        """Native symbols not ending in USDT return None (unsplittable)."""
        # ETHBTC has no delimiter and no USDT suffix — ambiguous
        assert parse_symbol("ETHBTC") is None

    def test_malformed_no_slash_no_usdt(self):
        """Symbols that can't be split return None rather than raising."""
        assert parse_symbol("INVALIDPAIR") is None

    def test_empty_string(self):
        """Empty string returns None."""
        assert parse_symbol("") is None

    def test_just_usdt(self):
        """'USDT' alone (base would be empty) returns None."""
        assert parse_symbol("USDT") is None


# ── filter_pairs_by_volume ────────────────────────────────────────────────────


class TestFilterPairsByVolume:
    """Tests for the 24-hour volume filter."""

    THRESHOLD = Decimal("1_000_000")

    def test_empty_ticker_list(self):
        """Empty input → empty output, no error raised."""
        result = filter_pairs_by_volume([], self.THRESHOLD)
        assert result == []

    def test_all_pairs_below_threshold(self):
        """All pairs below the threshold → empty output."""
        tickers = [
            _make_ticker("BTC/USDT", 500_000),
            _make_ticker("ETH/USDT", 999_999),
        ]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert result == []

    def test_pair_exactly_at_threshold_is_excluded(self):
        """A pair at exactly the threshold (not strictly above) is filtered out."""
        tickers = [_make_ticker("BTC/USDT", 1_000_000)]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert result == []

    def test_pair_strictly_above_threshold_is_included(self):
        """A pair one unit above the threshold passes the filter."""
        tickers = [_make_ticker("BTC/USDT", 1_000_001)]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert len(result) == 1
        assert result[0].base == "BTC"
        assert result[0].quote == "USDT"

    def test_mixed_pairs_only_above_threshold_returned(self):
        """Only pairs strictly above the threshold are returned."""
        tickers = [
            _make_ticker("BTC/USDT", 5_000_000),
            _make_ticker("ETH/USDT", 800_000),   # below
            _make_ticker("BNB/USDT", 2_000_000),
            _make_ticker("XRP/USDT", 1_000_000), # exactly at threshold
        ]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        bases = {p.base for p in result}
        assert bases == {"BTC", "BNB"}

    def test_non_usdt_pairs_are_ignored(self):
        """Non-USDT-quoted pairs (e.g. BTC/BNB) are never included."""
        tickers = [
            _make_ticker("BTC/BNB", 50_000_000),  # very high volume, but not USDT
            _make_ticker("ETH/BTC", 30_000_000),
            _make_ticker("BTC/USDT", 2_000_000),
        ]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert len(result) == 1
        assert result[0].base == "BTC"

    def test_missing_quote_volume_treated_as_zero(self):
        """Tickers without a quoteVolume field are treated as zero volume."""
        tickers = [_make_ticker("BTC/USDT", None)]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert result == []

    def test_malformed_quote_volume_treated_as_zero(self):
        """Tickers with non-numeric quoteVolume are silently skipped."""
        tickers = [{"symbol": "BTC/USDT", "quoteVolume": "not_a_number"}]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert result == []

    def test_result_sorted_by_volume_descending(self):
        """Returned pairs are sorted by volume_usdt in descending order."""
        tickers = [
            _make_ticker("XRP/USDT", 1_500_000),
            _make_ticker("BTC/USDT", 5_000_000),
            _make_ticker("ETH/USDT", 3_000_000),
        ]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        volumes = [p.volume_usdt for p in result]
        assert volumes == sorted(volumes, reverse=True)

    def test_volume_usdt_field_populated_correctly(self):
        """The volume_usdt field on each TradingPair matches the input volume."""
        tickers = [_make_ticker("ETH/USDT", 2_500_000.75)]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert result[0].volume_usdt == Decimal("2500000.75")

    def test_zero_threshold_includes_positive_volume_pairs(self):
        """With a zero threshold, all pairs with positive volume are included."""
        tickers = [
            _make_ticker("BTC/USDT", 1),
            _make_ticker("ETH/USDT", 2),
        ]
        result = filter_pairs_by_volume(tickers, Decimal("0"))
        assert len(result) == 2

    def test_symbol_field_is_native_format(self):
        """The TradingPair.symbol is stored in native format (no slash)."""
        tickers = [_make_ticker("BTC/USDT", 2_000_000)]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert result[0].symbol == "BTCUSDT"
        assert "/" not in result[0].symbol


# ── generate_triangles ────────────────────────────────────────────────────────


class TestGenerateTriangles:
    """Tests for triangle generation and duplicate elimination."""

    def test_empty_pairs_returns_empty(self):
        """Empty pair list → empty triangle list."""
        assert generate_triangles([]) == []

    def test_one_pair_returns_empty(self):
        """A single pair cannot form a triangle."""
        pairs = [_make_pair("BTC", "USDT", 5_000_000)]
        assert generate_triangles(pairs) == []

    def test_two_pairs_returns_empty(self):
        """Two pairs cannot form a closed triangle."""
        pairs = [
            _make_pair("BTC", "USDT", 5_000_000),
            _make_pair("ETH", "USDT", 3_000_000),
        ]
        assert generate_triangles(pairs) == []

    def test_three_pairs_no_triangle(self):
        """Three pairs that don't share the right assets yield no triangle."""
        # BTC/USDT, ETH/USDT, XRP/USDT — all quote USDT but there's no
        # ETH/BTC or XRP/ETH pair, so no closed triangle exists.
        pairs = [
            _make_pair("BTC", "USDT", 5_000_000),
            _make_pair("ETH", "USDT", 3_000_000),
            _make_pair("XRP", "USDT", 2_000_000),
        ]
        assert generate_triangles(pairs) == []

    def test_minimal_triangle_three_pairs(self):
        """Three pairs forming a perfect triangle produce exactly 1 triangle."""
        # BTC/USDT, ETH/USDT, ETH/BTC → BTC-ETH-USDT triangle
        pairs = [
            _make_pair("BTC", "USDT", 5_000_000),
            _make_pair("ETH", "USDT", 3_000_000),
            _make_pair("ETH", "BTC", 2_000_000),
        ]
        result = generate_triangles(pairs)
        assert len(result) == 1
        t = result[0]
        assert set([t.asset_a, t.asset_b, t.asset_c]) == {"BTC", "ETH", "USDT"}

    def test_no_duplicate_triangles_from_symmetric_pairs(self):
        """The same triangle is not generated twice when pairs are symmetric.

        BTC/USDT + ETH/USDT + ETH/BTC defines one economic triangle.
        The algorithm must not return it twice even though it can be
        discovered as BTC→ETH→USDT and also as ETH→BTC→USDT.
        """
        pairs = [
            _make_pair("BTC", "USDT", 5_000_000),
            _make_pair("ETH", "USDT", 3_000_000),
            _make_pair("ETH", "BTC", 2_000_000),
        ]
        result = generate_triangles(pairs)
        # Collect canonical keys to check for duplicates independently
        canonical_keys = [
            tuple(sorted([t.asset_a, t.asset_b, t.asset_c])) for t in result
        ]
        assert len(canonical_keys) == len(set(canonical_keys)), (
            "Duplicate triangles found in output"
        )

    def test_no_duplicates_larger_graph(self):
        """No duplicates in a graph with multiple overlapping triangles."""
        # 5 assets: BTC, ETH, BNB, USDT, XRP
        # Pairs: BTC/USDT, ETH/USDT, BNB/USDT, XRP/USDT,
        #        ETH/BTC, BNB/BTC, BNB/ETH, XRP/BTC
        # Expected triangles:
        #   BTC-ETH-USDT, BTC-BNB-USDT, ETH-BNB-USDT (via BNB/ETH),
        #   BNB-ETH-BTC (BNB/ETH + ETH/BTC + BNB/BTC), XRP-BTC-USDT
        pairs = [
            _make_pair("BTC",  "USDT", 10_000_000),
            _make_pair("ETH",  "USDT",  8_000_000),
            _make_pair("BNB",  "USDT",  6_000_000),
            _make_pair("XRP",  "USDT",  4_000_000),
            _make_pair("ETH",  "BTC",   5_000_000),
            _make_pair("BNB",  "BTC",   3_000_000),
            _make_pair("BNB",  "ETH",   2_000_000),
            _make_pair("XRP",  "BTC",   1_500_000),
        ]
        result = generate_triangles(pairs)
        canonical_keys = [
            tuple(sorted([t.asset_a, t.asset_b, t.asset_c])) for t in result
        ]
        assert len(canonical_keys) == len(set(canonical_keys)), (
            "Duplicate triangles found in larger graph"
        )

    def test_larger_graph_known_triangle_count(self):
        """Verify the exact number of triangles in the larger graph fixture."""
        pairs = [
            _make_pair("BTC",  "USDT", 10_000_000),
            _make_pair("ETH",  "USDT",  8_000_000),
            _make_pair("BNB",  "USDT",  6_000_000),
            _make_pair("XRP",  "USDT",  4_000_000),
            _make_pair("ETH",  "BTC",   5_000_000),
            _make_pair("BNB",  "BTC",   3_000_000),
            _make_pair("BNB",  "ETH",   2_000_000),
            _make_pair("XRP",  "BTC",   1_500_000),
        ]
        result = generate_triangles(pairs)
        # Expected: BTC-ETH-USDT, BTC-BNB-USDT, ETH-BNB-USDT,
        #           BTC-ETH-BNB (via ETH/BTC + BNB/ETH + BNB/BTC),
        #           BTC-XRP-USDT
        assert len(result) == 5

    def test_triangle_assets_match_available_pairs(self):
        """Every pair_ab/pair_bc/pair_ca in a Triangle maps to a real pair."""
        pairs = [
            _make_pair("BTC", "USDT", 5_000_000),
            _make_pair("ETH", "USDT", 3_000_000),
            _make_pair("ETH", "BTC",  2_000_000),
        ]
        available_symbols = {p.symbol for p in pairs}
        result = generate_triangles(pairs)
        for t in result:
            assert t.pair_ab in available_symbols, f"{t.pair_ab} not in pairs"
            assert t.pair_bc in available_symbols, f"{t.pair_bc} not in pairs"
            assert t.pair_ca in available_symbols, f"{t.pair_ca} not in pairs"

    def test_triangle_has_distinct_assets(self):
        """No triangle contains the same asset in two positions."""
        pairs = [
            _make_pair("BTC", "USDT", 5_000_000),
            _make_pair("ETH", "USDT", 3_000_000),
            _make_pair("ETH", "BTC",  2_000_000),
        ]
        result = generate_triangles(pairs)
        for t in result:
            assets = [t.asset_a, t.asset_b, t.asset_c]
            assert len(set(assets)) == 3, (
                f"Triangle has repeated asset: {t}"
            )

    def test_output_is_deterministic(self):
        """Calling generate_triangles twice with the same input returns the same list."""
        pairs = [
            _make_pair("BTC",  "USDT", 10_000_000),
            _make_pair("ETH",  "USDT",  8_000_000),
            _make_pair("BNB",  "USDT",  6_000_000),
            _make_pair("ETH",  "BTC",   5_000_000),
            _make_pair("BNB",  "BTC",   3_000_000),
            _make_pair("BNB",  "ETH",   2_000_000),
        ]
        first_run = generate_triangles(pairs)
        second_run = generate_triangles(pairs)
        assert first_run == second_run

    def test_output_is_sorted(self):
        """Returned list is sorted by (asset_a, asset_b, asset_c)."""
        pairs = [
            _make_pair("BTC",  "USDT", 10_000_000),
            _make_pair("ETH",  "USDT",  8_000_000),
            _make_pair("BNB",  "USDT",  6_000_000),
            _make_pair("ETH",  "BTC",   5_000_000),
            _make_pair("BNB",  "BTC",   3_000_000),
            _make_pair("BNB",  "ETH",   2_000_000),
        ]
        result = generate_triangles(pairs)
        keys = [(t.asset_a, t.asset_b, t.asset_c) for t in result]
        assert keys == sorted(keys)

    def test_isolated_pair_does_not_form_triangle(self):
        """Adding a pair that connects to no existing pair doesn't create a triangle."""
        pairs = [
            _make_pair("BTC", "USDT", 5_000_000),
            _make_pair("ETH", "USDT", 3_000_000),
            _make_pair("ETH", "BTC",  2_000_000),
            _make_pair("DOGE", "USDT", 1_500_000),  # no DOGE/BTC or DOGE/ETH
        ]
        result = generate_triangles(pairs)
        for t in result:
            assert "DOGE" not in (t.asset_a, t.asset_b, t.asset_c), (
                "DOGE appeared in a triangle despite having no connecting pairs"
            )
