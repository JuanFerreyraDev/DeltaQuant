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

    Produces the same symbol format that ``filter_pairs_by_volume`` uses:
    - USDT-quoted pairs: concatenated native format (e.g. ``"BTCUSDT"``)
    - Cross pairs: slash-delimited format (e.g. ``"ETH/BTC"``)

    This keeps test fixtures consistent with real pipeline output so that
    ``test_triangle_assets_match_available_pairs`` and any future evaluator
    tests that reconstruct symbols from Triangles work correctly.

    Args:
        base: Base asset ticker, e.g. ``"BTC"``.
        quote: Quote asset ticker, e.g. ``"USDT"``.
        volume: Volume to assign as ``volume_usdt``.

    Returns:
        A ``TradingPair`` instance with a correctly-formatted native symbol.
    """
    symbol = f"{base}{quote}" if quote == "USDT" else f"{base}/{quote}"
    return TradingPair(
        symbol=symbol,
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

    def test_non_usdt_pairs_excluded_without_quote_price(self):
        """Non-USDT-quoted pairs are excluded when no quote-asset USDT price is available.

        BTC/BNB and ETH/BTC cannot be converted to USDT-equivalent volume because
        the batch provides no BNB/USDT or BTC/USDT ``last`` price field.  The
        filter must exclude them rather than crashing or guessing.

        Note: if a USDT price for the quote asset *is* available (via another
        ticker in the batch supplying a ``last`` field), the cross pair CAN be
        included.  See ``test_pipeline_with_cross_pair_generates_triangle`` in
        ``TestFilterAndGeneratePipeline`` for the positive case.
        """
        tickers = [
            # BTC/BNB: quote = BNB. No BNB/USDT in batch → excluded.
            _make_ticker("BTC/BNB", 50_000_000),
            # ETH/BTC: quote = BTC. No BTC/USDT last-price in batch → excluded.
            _make_ticker("ETH/BTC", 30_000_000),
            # BTC/USDT: included. No 'last' field needed for USDT pairs.
            _make_ticker("BTC/USDT", 2_000_000),
        ]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert len(result) == 1
        assert result[0].base == "BTC"
        assert result[0].quote == "USDT"

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
        # Type check: value equality alone is insufficient because Python allows
        # Decimal == float comparisons to return True when the float is exactly
        # representable in binary (0.75 is).  The field must be a Decimal
        # instance, never a raw float — technical plan §2 hard rule.
        assert isinstance(result[0].volume_usdt, Decimal), (
            "volume_usdt must be Decimal, not float — "
            "binary float rounding violates §2 of the technical plan"
        )

    def test_volume_usdt_field_is_decimal_even_for_float_input(self):
        """volume_usdt is always Decimal regardless of whether quoteVolume was float or str.

        This test would have caught the Decimal/float regression introduced in
        commit 58622fe (``Decimal(str(raw_vol)) if isinstance(raw_vol, str) else raw_vol``).
        ccxt always returns quoteVolume as a Python float from the exchange
        response; the isinstance check left it unconverted for USDT pairs.
        """
        # Use a volume that is NOT exactly representable in binary float so
        # that float != Decimal comparison fails if the type is wrong.
        # 0.1 is the canonical example: float(0.1) != Decimal("0.1")
        tickers = [_make_ticker("BTC/USDT", 1_500_000.1)]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert len(result) == 1
        assert isinstance(result[0].volume_usdt, Decimal), (
            "volume_usdt must be Decimal — raw float from ccxt must be converted"
        )
        # Value must match the string-converted path, not the raw float path.
        # If raw float were kept: float(1_500_000.1) ≠ Decimal("1500000.1")
        # because the float has binary rounding error.
        assert result[0].volume_usdt == Decimal("1500000.1")

    def test_zero_threshold_includes_positive_volume_pairs(self):
        """With a zero threshold, all pairs with positive volume are included."""
        tickers = [
            _make_ticker("BTC/USDT", 1),
            _make_ticker("ETH/USDT", 2),
        ]
        result = filter_pairs_by_volume(tickers, Decimal("0"))
        assert len(result) == 2

    def test_symbol_field_is_native_format(self):
        """USDT-pair TradingPair.symbol is concatenated native format (no slash)."""
        tickers = [_make_ticker("BTC/USDT", 2_000_000)]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        assert result[0].symbol == "BTCUSDT"
        assert "/" not in result[0].symbol

    def test_cross_pair_symbol_is_slash_delimited(self):
        """Cross-pair TradingPair.symbol uses slash-delimited format (e.g. 'ETH/BTC').

        USDT pairs use concatenated format (``'BTCUSDT'``) because the fixed
        4-char 'USDT' suffix makes them unambiguous.  Cross pairs (non-USDT
        quote) use slash-delimited format (``'ETH/BTC'``) because concatenation
        is ambiguous without a known-assets list and ``parse_symbol('ETHBTC')``
        returns ``None``.
        """
        tickers = [
            {**_make_ticker("BTC/USDT", 10_000_000), "last": 67_000},
            {**_make_ticker("ETH/BTC", None), "quoteVolume": 50_000, "last": 0.045},
        ]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        eth_btc = next(
            (p for p in result if p.base == "ETH" and p.quote == "BTC"), None
        )
        assert eth_btc is not None, "ETH/BTC should pass the volume filter"
        assert eth_btc.symbol == "ETH/BTC", (
            f"Cross-pair symbol must be slash-delimited, got '{eth_btc.symbol}'"
        )
        assert "/" in eth_btc.symbol

    def test_cross_pair_symbol_round_trips_through_parse_symbol(self):
        """Every symbol stored in a TradingPair round-trips through parse_symbol.

        This is the key correctness property: any symbol stored in a
        ``TradingPair`` (and therefore in a ``Triangle``) must be parseable by
        ``parse_symbol`` so the evaluator can reconstruct base/quote for fee
        lookups and order routing.

        A concatenated cross-pair symbol like ``'ETHBTC'`` fails this check —
        ``parse_symbol('ETHBTC')`` returns ``None`` because the split is
        ambiguous.  The slash-delimited format ``'ETH/BTC'`` parses correctly.
        """
        tickers = [
            {**_make_ticker("BTC/USDT", 10_000_000), "last": 67_000},
            {**_make_ticker("ETH/BTC", None), "quoteVolume": 50_000, "last": 0.045},
        ]
        result = filter_pairs_by_volume(tickers, self.THRESHOLD)
        for pair in result:
            parsed = parse_symbol(pair.symbol)
            assert parsed is not None, (
                f"parse_symbol('{pair.symbol}') returned None — "
                "symbol format is ambiguous and cannot round-trip"
            )
            base, quote = parsed
            assert base == pair.base, (
                f"Round-trip base mismatch: {base!r} != {pair.base!r}"
            )
            assert quote == pair.quote, (
                f"Round-trip quote mismatch: {quote!r} != {pair.quote!r}"
            )


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
        # Expected with USDT constraint:
        # - BTC-ETH-USDT (via BTC/USDT + ETH/USDT + ETH/BTC)
        # - BNB-BTC-USDT (via BNB/USDT + BTC/USDT + BNB/BTC)
        # - BNB-ETH-USDT (via BNB/USDT + ETH/USDT + BNB/ETH)
        # - BTC-XRP-USDT (via BTC/USDT + XRP/USDT + XRP/BTC)
        # Excluded: BTC-ETH-BNB has no USDT leg, so it violates the constraint.
        assert len(result) == 4

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

    def test_usdt_constraint_excludes_non_usdt_triangles(self):
        """Triangles without a USDT leg are filtered out (bot has no non-USDT capital)."""
        # BTC-ETH-BNB forms a complete triangle, but no USDT leg → must be excluded.
        pairs = [
            _make_pair("BTC", "ETH",   5_000_000),
            _make_pair("ETH", "BNB",   3_000_000),
            _make_pair("BNB", "BTC",   2_000_000),
        ]
        result = generate_triangles(pairs)
        assert result == [], "Non-USDT triangle should be excluded"

    def test_usdt_triangle_is_included(self):
        """Triangles with a USDT leg are included."""
        # BTC-ETH-USDT has a USDT leg → must be included.
        pairs = [
            _make_pair("BTC", "USDT", 5_000_000),
            _make_pair("ETH", "USDT", 3_000_000),
            _make_pair("BTC", "ETH",  2_000_000),
        ]
        result = generate_triangles(pairs)
        assert len(result) == 1, "USDT triangle should be included"
        t = result[0]
        assert "USDT" in (t.asset_a, t.asset_b, t.asset_c), (
            "Included triangle must have USDT as one of the three assets"
        )


# ── Integration tests ─────────────────────────────────────────────────────────


class TestFilterAndGeneratePipeline:
    """Integration tests: full pipeline from raw tickers to triangles.

    These tests validate the end-to-end flow that the bug report highlighted:
    the volume filter and triangle generator must work together. Unit tests
    that construct TradingPair objects by hand cannot catch defects at the
    seam between these two stages.
    """

    THRESHOLD = Decimal("1_000_000")

    def test_pipeline_with_cross_pair_generates_triangle(self):
        """Full pipeline with cross pairs produces at least one triangle with USDT.

        This is the test category that would have caught the original bug where
        cross pairs like ETH/BTC were filtered out unconditionally, preventing
        any triangle from forming even though the graph contained the necessary
        edges to form a cycle including USDT.

        **Conversion math verification**:
        - BTC/USDT: price = $67,000, volume = $10M
        - ETH/USDT: price = $3,000, volume = $8M
        - ETH/BTC: quoteVolume = 50,000 BTC (quoted in BTC, not USDT)
          → USDT-equivalent = 50,000 BTC × $67,000/BTC = $3.35B (passes $1M threshold)
        - The filter accepts all three; generator produces BTC-ETH-USDT triangle.
        """
        raw_tickers = [
            # BTC/USDT: volume = $10M, price = $67,000
            _make_ticker("BTC/USDT", 10_000_000),
            # ETH/USDT: volume = $8M, price = $3,000
            _make_ticker("ETH/USDT", 8_000_000),
            # ETH/BTC: quoteVolume = 50,000 BTC
            _make_ticker("ETH/BTC", None),
        ]
        # Add price (last trade price) for USDT pairs
        raw_tickers[0]["last"] = 67_000  # BTC/USDT price
        raw_tickers[1]["last"] = 3_000   # ETH/USDT price

        # For ETH/BTC, add quoteVolume (in BTC, not USDT)
        raw_tickers[2]["quoteVolume"] = 50_000  # 50,000 BTC volume in 24h
        raw_tickers[2]["last"] = 0.0447  # ETH/BTC last price (20 ETH ≈ 1 BTC, so 1 BTC ≈ 0.045 ETH)

        # Run the pipeline
        filtered_pairs = filter_pairs_by_volume(raw_tickers, self.THRESHOLD)
        triangles = generate_triangles(filtered_pairs)

        # Verify results
        assert len(filtered_pairs) == 3, (
            f"Filter should pass all three pairs with sufficient volume; "
            f"got {len(filtered_pairs)} pairs: {[(p.base, p.quote, p.volume_usdt) for p in filtered_pairs]}"
        )
        # ETH/BTC USDT-equivalent: 50k BTC × $67k/BTC = $3.35B
        # Symbol is slash-delimited for cross pairs (see Issue B fix).
        eth_btc_pair = [p for p in filtered_pairs if p.symbol == "ETH/BTC"][0]
        expected_usdt_volume = Decimal("50000") * Decimal("67000")
        assert eth_btc_pair.volume_usdt == expected_usdt_volume, (
            f"ETH/BTC should be converted to USDT-equivalent; "
            f"got {eth_btc_pair.volume_usdt} vs expected {expected_usdt_volume}"
        )

        assert len(triangles) >= 1, (
            f"Pipeline should generate at least one triangle; "
            f"got {len(triangles)}"
        )
        # Verify all triangles contain USDT
        for t in triangles:
            assert "USDT" in (t.asset_a, t.asset_b, t.asset_c), (
                f"Triangle {t} must contain USDT"
            )

    def test_pipeline_excludes_cross_pair_below_volume(self):
        """Cross pair below volume threshold is excluded from graph.

        **Conversion math verification**:
        - BTC/USDT: price = $67,000
        - ETH/USDT: price = $3,000
        - ETH/BTC: quoteVolume = 0.5 BTC (very low)
          → USDT-equivalent = 0.5 BTC × $67,000/BTC = $33,500 (below $1M threshold)
        - Filter excludes ETH/BTC; no triangle is formed (requires ETH/BTC edge).
        """
        raw_tickers = [
            _make_ticker("BTC/USDT", 10_000_000),
            _make_ticker("ETH/USDT", 8_000_000),
            _make_ticker("ETH/BTC", None),
        ]
        # Add prices for USDT pairs
        raw_tickers[0]["last"] = 67_000  # BTC/USDT price
        raw_tickers[1]["last"] = 3_000   # ETH/USDT price

        # ETH/BTC has very low quoteVolume: 0.5 BTC = $33,500 (below threshold)
        raw_tickers[2]["quoteVolume"] = Decimal("0.5")
        raw_tickers[2]["last"] = 0.0447

        filtered_pairs = filter_pairs_by_volume(raw_tickers, self.THRESHOLD)
        triangles = generate_triangles(filtered_pairs)

        # Only BTC/USDT and ETH/USDT should pass; ETH/BTC excluded
        assert len(filtered_pairs) == 2, (
            f"Filter should exclude low-volume cross pair; "
            f"got {len(filtered_pairs)} pairs"
        )
        # Triangle BTC-ETH-USDT requires ETH/BTC edge, which is absent
        assert len(triangles) == 0, (
            "Without the ETH/BTC edge, no triangle can form"
        )

    def test_pipeline_excludes_cross_pair_with_missing_quote_price(self):
        """Non-USDT pair whose quote asset lacks a USDT price is excluded.

        Scenario: DOGE/EXOTIC pair, where EXOTIC is some asset without a
        USDT listing in the batch. Expected: DOGE/EXOTIC is excluded because
        we cannot determine EXOTIC's USDT price (not converted with a
        fallback or heuristic, just filtered out).
        """
        raw_tickers = [
            # Base pairs for price lookup
            _make_ticker("BTC/USDT", 10_000_000),
            _make_ticker("ETH/USDT", 8_000_000),
            # Cross pair with no price lookup available (EXOTIC has no USDT pair)
            _make_ticker("DOGE/EXOTIC", 5_000_000),
        ]
        raw_tickers[0]["last"] = 67_000  # BTC/USDT price
        raw_tickers[1]["last"] = 3_000   # ETH/USDT price

        # DOGE/EXOTIC has volume in EXOTIC, but EXOTIC has no USDT pair in batch
        raw_tickers[2]["quoteVolume"] = 100_000_000  # 100M EXOTIC units
        raw_tickers[2]["last"] = 0.00001  # DOGE/EXOTIC price

        filtered_pairs = filter_pairs_by_volume(raw_tickers, self.THRESHOLD)

        # Only BTC/USDT and ETH/USDT should pass. DOGE/EXOTIC excluded.
        assert len(filtered_pairs) == 2, (
            f"Filter should exclude pair with missing quote-asset USDT price; "
            f"got {len(filtered_pairs)} pairs: {[(p.base, p.quote, p.volume_usdt) for p in filtered_pairs]}"
        )
        symbols = {p.symbol for p in filtered_pairs}
        assert symbols == {"BTCUSDT", "ETHUSDT"}, (
            f"Only USDT pairs should be in result; got {symbols}"
        )

    def test_pipeline_usdt_constraint_applied_at_output(self):
        """Even if graph contains cycles, non-USDT triangles are rejected.

        Scenario: BTC-ETH-BNB forms a complete cycle. However, none of these
        pairs have a USDT quote asset in the batch, so none survive the price
        lookup step. The filter returns empty and generator produces no
        triangles (correct — no executable cycle without USDT legs).
        """
        raw_tickers = [
            _make_ticker("BTC/ETH", 5_000_000),
            _make_ticker("ETH/BNB", 3_000_000),
            _make_ticker("BNB/BTC", 2_000_000),
        ]
        # Manually add quoteVolume for all pairs
        raw_tickers[0]["quoteVolume"] = 100_000  # 100k ETH
        raw_tickers[1]["quoteVolume"] = 500_000  # 500k BNB
        raw_tickers[2]["quoteVolume"] = 50_000   # 50k BTC
        raw_tickers[0]["last"] = 0.075           # ETH/BTC price
        raw_tickers[1]["last"] = 0.05            # BNB/ETH price
        raw_tickers[2]["last"] = 13.3            # BTC/BNB price

        # No USDT pairs in this batch, so price lookup is empty.
        filtered_pairs = filter_pairs_by_volume(raw_tickers, self.THRESHOLD)

        # All non-USDT pairs excluded due to missing quote-asset USDT prices.
        assert len(filtered_pairs) == 0, (
            "All pairs should be excluded due to missing quote-asset USDT prices"
        )

        triangles = generate_triangles(filtered_pairs)
        # The cycle BTC-ETH-BNB cannot form without edges.
        assert len(triangles) == 0, (
            "No triangles can form without pairs in the filtered set"
        )
