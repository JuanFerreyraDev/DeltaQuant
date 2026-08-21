"""Triangle graph construction for DeltaQuant.

This module implements two responsibilities from the technical plan (§8, Fase 1
and §3 "Flujo de evaluación"):

1. **Volume filter** (step 1 of the evaluation flow): given a list of raw ccxt
   ticker dicts, discard any pair whose 24-hour quote-asset volume is below the
   ``MIN_VOLUME_USDT`` threshold defined in ``Settings``.  Both USDT-quoted
   pairs and cross pairs (e.g. ``ETH/BTC``) are accepted.  For USDT-quoted
   pairs the raw ``quoteVolume`` is used directly; for cross pairs the volume
   is normalized to a USDT-equivalent by multiplying ``quoteVolume`` by the
   quote asset's USDT price sourced from the same ticker batch (see ADR-003).

2. **Triangle generation** (step 2): from the filtered symbol set, enumerate
   all closed triangles of the form ``A → B → C → A`` where each directed
   edge corresponds to a tradeable pair in the filtered set.  The result
   contains no duplicates: ``(BTC, ETH, USDT)`` and ``(ETH, BTC, USDT)``
   are the same triangle and appear exactly once.

Terminology used throughout this module:
    - *Symbol*: exchange-native string, e.g. ``"BTCUSDT"``.
    - *Base / Quote*: as defined by the exchange — for ``"BTCUSDT"``,
      base = ``"BTC"``, quote = ``"USDT"``.
    - *Triangle*: an ordered frozen triple ``(asset_a, asset_b, asset_c)``
      representing the cycle ``A → B → C → A``, where canonical ordering
      ensures uniqueness.

Design note:
    This module is exchange-agnostic.  It receives a plain list of dicts (the
    ccxt ticker format) and returns ``Triangle`` objects.  The caller
    (``main.py`` or an integration layer) is responsible for fetching the
    tickers via ``BinanceAdapter.fetch_tickers_24h()`` and passing them here.
    The graph module never imports ``BinanceAdapter`` directly — this keeps
    the engine layer decoupled from any specific exchange.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import NamedTuple


# ── Data model ────────────────────────────────────────────────────────────────


class TradingPair(NamedTuple):
    """A single tradeable market, split into its component assets.

    Attributes:
        symbol: Exchange-native symbol string, e.g. ``"BTCUSDT"``.
        base: Base asset ticker, e.g. ``"BTC"``.
        quote: Quote asset ticker, e.g. ``"USDT"``.
        volume_usdt: 24-hour volume expressed in USDT.  For USDT-quoted pairs
            this is the raw ``quoteVolume``; for non-USDT pairs it is the
            USDT-equivalent computed as ``quoteVolume × quote-asset USDT price``
            (see ADR-003).  Non-USDT pairs are excluded only when the quote
            asset's USDT price is absent from the ticker batch — not as a
            blanket rule.
    """

    symbol: str
    base: str
    quote: str
    volume_usdt: Decimal


class Triangle(NamedTuple):
    """A canonical, duplicate-free representation of one arbitrage triangle.

    Stores the three asset tickers that form the cycle ``A → B → C → A``
    and the three trading pair symbols needed to traverse it.  Asset names
    are stored in canonical order (lexicographically sorted) so that the same
    economic triangle always maps to the same ``Triangle`` object regardless
    of the traversal direction it was discovered in.

    Attributes:
        asset_a: First asset in canonical order.
        asset_b: Second asset in canonical order.
        asset_c: Third asset in canonical order.
        pair_ab: Symbol for the ``A ↔ B`` leg, e.g. ``"BTCUSDT"``.
        pair_bc: Symbol for the ``B ↔ C`` leg, e.g. ``"ETHBTC"``.
        pair_ca: Symbol for the ``C ↔ A`` leg, e.g. ``"ETHUSDT"``.
    """

    asset_a: str
    asset_b: str
    asset_c: str
    pair_ab: str
    pair_bc: str
    pair_ca: str


# ── Volume filter ─────────────────────────────────────────────────────────────


def parse_symbol(symbol: str) -> tuple[str, str] | None:
    """Split a ccxt unified symbol string into (base, quote) components.

    ccxt uses a forward-slash notation (``"BTC/USDT"``), while Binance's
    native REST API returns concatenated strings (``"BTCUSDT"``).  This
    function handles both formats.

    Args:
        symbol: A symbol string in either ``"BASE/QUOTE"`` or ``"BASEQUOTE"``
            format.  The function only handles two-asset pairs (exactly one
            ``/`` in unified format, or a known-USDT suffix for concatenated
            format).

    Returns:
        A ``(base, quote)`` tuple of uppercase strings, e.g.
        ``("BTC", "USDT")``, or ``None`` if the symbol cannot be parsed.
    """
    if "/" in symbol:
        parts = symbol.split("/")
        if len(parts) == 2:
            return parts[0].upper(), parts[1].upper()
        return None
    # Native Binance format — no delimiter.  We can only reliably split these
    # for USDT-quoted pairs and known non-USDT pairs (e.g. "ETHBTC").
    # In Phase 1, we only parse USDT-quoted pairs from native format;
    # non-USDT pairs must arrive in slash-delimited unified format from ccxt.
    if symbol.endswith("USDT") and len(symbol) > 4:
        return symbol[:-4].upper(), "USDT"
    return None


def _build_quote_asset_prices(raw_tickers: list[dict]) -> dict[str, Decimal]:
    """Build a map of quote asset → its price in USDT from USDT-quoted tickers.

    Scans the raw ticker list for all USDT-quoted pairs. For each such pair,
    extracts the base asset ticker and its current price (from the ``last``
    field, the most recent trade price available in the ticker). This map is
    used to compute USDT-equivalent volumes for non-USDT-quoted pairs.

    **Price field choice**: ``last`` (most recent trade price) is used instead of
    ``close`` (24h close) because it reflects current market price at the time
    tickers were fetched, and is consistent with how ccxt.binance exposes
    real-time pricing. For a stable USDT price lookup, any trade price within
    the 24h window is sufficient (USDT is a stablecoin).

    Args:
        raw_tickers: List of ccxt ticker dicts from ``fetch_tickers``.

    Returns:
        Dict mapping asset ticker (e.g. ``"BTC"``, ``"ETH"``) to its current
        USDT price as ``Decimal``. Assets without an associated USDT ticker
        in the batch are absent from the map.
    """
    prices: dict[str, Decimal] = {}

    for ticker in raw_tickers:
        raw_symbol: str = ticker.get("symbol", "")
        parsed = parse_symbol(raw_symbol)
        if parsed is None:
            continue

        base, quote = parsed
        if quote != "USDT":
            continue

        # Extract price from the ``last`` field (most recent trade price).
        # This is the price used for volume conversion of non-USDT pairs.
        last_price = ticker.get("last")
        if last_price is None:
            continue

        try:
            prices[base] = Decimal(str(last_price))
        except Exception:
            # If conversion fails, skip this price entry.
            continue

    return prices


def filter_pairs_by_volume(
    raw_tickers: list[dict],
    min_volume_usdt: Decimal,
) -> list[TradingPair]:
    """Filter ccxt ticker dicts to tradeable pairs above the volume threshold.

    Iterates over the raw ticker list returned by ``BinanceAdapter.fetch_tickers_24h()``,
    filters by 24-hour volume, and discards any pair whose volume is strictly
    below ``min_volume_usdt``.

    **Volume comparison for USDT-quoted vs. cross pairs:**

    - For USDT-quoted pairs (e.g. ``BTC/USDT``): compares ``quoteVolume``
      (24h volume already denominated in USDT) directly against the threshold.

    - For non-USDT pairs (e.g. ``ETH/BTC``): computes USDT-equivalent volume as:

        ``quoteVolume × price_of_quote_asset_in_USDT``

      The quote asset's USDT price is sourced from a USDT-quoted pair
      (``quote_asset/USDT``) in the same raw_tickers batch. If no such pair
      exists (e.g. an exotic quote asset with no direct USDT listing), the
      pair is excluded — we do not fall back to unconverted fallbacks or
      heuristics.

    This approach is cleaner than using baseVolume because:
    - quoteVolume is already in a single denomination (the quote asset), so one
      price lookup converts to USDT.
    - baseVolume would require the base asset's USDT price for a full conversion,
      an unnecessary second lookup.

    The function is intentionally permissive about missing/malformed data for
    USDT pairs (treated as zero volume, not an error). For non-USDT pairs, it
    requires both a price lookup and a quoteVolume value; missing either results
    in the pair being excluded (not an error).

    Args:
        raw_tickers: List of ccxt ticker dicts from ``fetch_tickers_24h()``.
            Each dict is expected to have at minimum ``"symbol"`` (unified,
            e.g. ``"BTC/USDT"``) and ``"quoteVolume"`` (float or string).
            Non-USDT pairs must also have a ``"last"`` field (recent trade
            price). Extra keys are ignored.
        min_volume_usdt: Minimum 24-hour USDT-equivalent volume threshold.
            Pairs must be *strictly above* this threshold.

    Returns:
        List of ``TradingPair`` objects for pairs that passed the filter,
        sorted by ``volume_usdt`` descending (highest liquidity first).
        Returns an empty list if no pair passes.
    """
    # Build a map of asset → USDT price for all USDT-quoted pairs.
    # This map is used to convert non-USDT volumes to USDT equivalents.
    quote_asset_prices = _build_quote_asset_prices(raw_tickers)

    result: list[TradingPair] = []

    for ticker in raw_tickers:
        raw_symbol: str = ticker.get("symbol", "")
        parsed = parse_symbol(raw_symbol)
        if parsed is None:
            continue

        base, quote = parsed

        # Determine volume based on quote asset.
        if quote == "USDT":
            # USDT-quoted pair: volume is already in USDT.
            raw_vol = ticker.get("quoteVolume")
        else:
            # Non-USDT pair: convert quoteVolume to USDT-equivalent.
            raw_quote_vol = ticker.get("quoteVolume")
            if raw_quote_vol is None:
                continue

            # Look up the quote asset's USDT price.
            if quote not in quote_asset_prices:
                # No price available for this quote asset → exclude the pair.
                continue

            quote_price_usdt = quote_asset_prices[quote]

            try:
                quote_volume_decimal = Decimal(str(raw_quote_vol))
            except Exception:
                continue

            # Compute USDT-equivalent volume.
            raw_vol = quote_volume_decimal * quote_price_usdt

        if raw_vol is None:
            continue

        try:
            # Always convert via Decimal(str(...)) regardless of the input type.
            # Technical plan §2 hard rule: no raw floats on monetary fields.
            volume = Decimal(str(raw_vol))
        except Exception:
            continue

        if volume <= min_volume_usdt:
            continue

        # Derive the native symbol: concatenated for USDT pairs (unambiguous),
        # slash-delimited for cross pairs (unambiguous and parseable).
        if quote == "USDT":
            native_symbol = f"{base}{quote}"
        else:
            native_symbol = f"{base}/{quote}"

        result.append(
            TradingPair(
                symbol=native_symbol,
                base=base,
                quote=quote,
                volume_usdt=volume,
            )
        )

    result.sort(key=lambda p: p.volume_usdt, reverse=True)
    return result


# ── Triangle generation ───────────────────────────────────────────────────────


def _build_pair_index(pairs: list[TradingPair]) -> dict[frozenset[str], str]:
    """Build a lookup from asset-pair frozenset to native symbol.

    This index lets the triangle generator check in O(1) whether a pair
    connecting two assets exists in the filtered set.

    Example:
        ``{frozenset({"BTC", "USDT"}): "BTCUSDT", ...}``

    Args:
        pairs: Filtered list of ``TradingPair`` objects.

    Returns:
        Dict mapping each ``frozenset({base, quote})`` to the corresponding
        native symbol string.
    """
    return {frozenset({p.base, p.quote}): p.symbol for p in pairs}


def _canonical_triangle(
    a: str,
    b: str,
    c: str,
    pair_index: dict[frozenset[str], str],
) -> Triangle | None:
    """Attempt to build a canonical ``Triangle`` from three asset tickers.

    Checks that all three edges (A-B, B-C, C-A) exist in ``pair_index``.
    If any edge is missing the triplet cannot form a complete arbitrage
    triangle and ``None`` is returned.

    Canonical ordering: the three asset names are sorted lexicographically
    and assigned to ``asset_a``, ``asset_b``, ``asset_c`` so that the same
    triplet always produces the same ``Triangle`` regardless of which order
    the assets were discovered.

    Args:
        a: First asset ticker.
        b: Second asset ticker.
        c: Third asset ticker.
        pair_index: Lookup built by ``_build_pair_index``.

    Returns:
        A ``Triangle`` with canonical asset ordering, or ``None`` if any of
        the three required pairs is missing from the filtered set.
    """
    assets = tuple(sorted([a, b, c]))
    ca, cb, cc = assets

    sym_ab = pair_index.get(frozenset({ca, cb}))
    sym_bc = pair_index.get(frozenset({cb, cc}))
    sym_ca = pair_index.get(frozenset({cc, ca}))

    if sym_ab is None or sym_bc is None or sym_ca is None:
        return None

    return Triangle(
        asset_a=ca,
        asset_b=cb,
        asset_c=cc,
        pair_ab=sym_ab,
        pair_bc=sym_bc,
        pair_ca=sym_ca,
    )


def generate_triangles(pairs: list[TradingPair]) -> list[Triangle]:
    """Generate all unique arbitrage triangles from the filtered pair list.

    An arbitrage triangle is a closed cycle of three assets ``A → B → C → A``
    where a tradeable pair exists for each of the three edges.

    **USDT constraint**: All triangles returned must include USDT as one of the
    three assets. This reflects the bot's operational constraint: capital is
    held exclusively in USDT (technical plan §1), so a triangle with no USDT
    leg (e.g. ``BTC-ETH-BNB``) cannot be executed — there is no way to enter
    or exit the cycle without first converting to/from USDT.

    **Duplicate elimination**: two traversals of the same economic triangle
    (e.g. ``BTC→ETH→USDT`` and ``ETH→BTC→USDT``) are collapsed into a single
    ``Triangle`` by canonicalising the asset order (lexicographic sort) and
    using a ``set`` of already-seen triplets during iteration.

    Algorithm:
        1. Build a per-asset neighbour list from the pair index.
        2. For every base asset ``A`` that appears in any pair:
             For every neighbour ``B`` of ``A`` (connected by any pair):
               For every neighbour ``C`` of ``B``:
                 If the edge ``C → A`` also exists:
                   Attempt to build a canonical Triangle and add to result set
                   (after checking the USDT constraint).
        3. The set guarantees no duplicates; convert to a sorted list for
           deterministic output.

    Args:
        pairs: Filtered list of ``TradingPair`` objects from
            ``filter_pairs_by_volume``.  May be empty, in which case an
            empty list is returned immediately.  May contain non-USDT pairs
            (e.g. ``ETH/BTC``), which are now accepted by the volume filter.

    Returns:
        List of unique ``Triangle`` objects where each triangle contains USDT.
        Sorted by ``(asset_a, asset_b, asset_c)`` for deterministic ordering.
        Returns an empty list if fewer than three distinct assets exist in
        ``pairs``, if no complete triangle can be formed, or if all candidate
        triangles violate the USDT constraint.
    """
    if len(pairs) < 3:
        return []

    pair_index = _build_pair_index(pairs)

    # Build adjacency list: asset → set of directly connected assets.
    adjacency: dict[str, set[str]] = defaultdict(set)
    for key in pair_index:
        assets = list(key)
        adjacency[assets[0]].add(assets[1])
        adjacency[assets[1]].add(assets[0])

    seen: set[tuple[str, str, str]] = set()
    triangles: list[Triangle] = []

    all_assets = list(adjacency.keys())

    for a in all_assets:
        neighbours_a = adjacency[a]
        for b in neighbours_a:
            if b == a:
                continue
            neighbours_b = adjacency[b]
            for c in neighbours_b:
                if c == a or c == b:
                    continue
                # Check the closing edge C → A exists.
                if a not in adjacency[c]:
                    continue

                canonical_key = tuple(sorted([a, b, c]))
                if canonical_key in seen:
                    continue

                triangle = _canonical_triangle(a, b, c, pair_index)
                if triangle is None:
                    continue

                # **USDT constraint**: at least one of the three assets must be USDT.
                # The bot cannot trade in a cycle with no USDT leg (no entry/exit point).
                if "USDT" not in (triangle.asset_a, triangle.asset_b, triangle.asset_c):
                    continue

                seen.add(canonical_key)
                triangles.append(triangle)

    triangles.sort(key=lambda t: (t.asset_a, t.asset_b, t.asset_c))
    return triangles
