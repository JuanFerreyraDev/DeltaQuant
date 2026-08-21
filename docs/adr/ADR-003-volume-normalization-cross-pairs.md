# ADR-003: Volume Normalization for Cross-Asset Pairs

## Status
Accepted

## Context

The first step of the arbitrage flow (`core/graph.py`, `filter_pairs_by_volume`)
must decide whether a trading pair has sufficient 24-hour volume to be included
in the graph. For USDT-quoted pairs, this is straightforward: `quoteVolume` is
already expressed in USDT.

For cross-asset pairs (e.g. `ETH/BTC`, where the quote asset is not USDT),
`quoteVolume` is denominated in the quote asset (BTC, in this example), not
USDT. A naive comparison of raw `quoteVolume` against a USDT-denominated
threshold is incorrect: it conflates two different units. For example:

- `ETH/BTC` with `quoteVolume = 50,000 BTC` is economically ~50,000 × $67,000 = $3.35B
- But the comparison `50,000 < 1_000_000 (threshold)` would reject it incorrectly.

Three approaches were considered:

1. **Use baseVolume as a proxy (rejected)**: Compare the base asset's volume
   against a USDT threshold. This still mixes units and introduces asymmetry
   (base asset price matters, not quote asset price). Required a second price
   lookup for correctness.

2. **Ship unconverted quoteVolume with an ADR (rejected)**: Document why the
   conversion isn't done, accept the resulting behavior. Leaves a time bomb in
   the code for Phase 2.

3. **Convert using the quote asset's USDT price (chosen)**: The quote asset's
   price in USDT can be sourced from a USDT-quoted pair in the same ticker batch
   (e.g. `BTC/USDT` for `ETH/BTC`). No additional API call required.

## Decision

For a non-USDT-quoted pair, compute its USDT-equivalent 24-hour volume as:

```
quoteVolume (in quote asset units) × price_of_quote_asset_in_USDT
```

The price is obtained from a USDT-quoted ticker in the same raw ticker batch
passed to `filter_pairs_by_volume`.

If no such ticker exists (the quote asset has no direct USDT listing in the
data), the pair is **excluded** — not rejected with an error, just filtered out,
as if its volume were below threshold. This is semantically appropriate: an
unmeasurable pair is not a usable pair.

### Implementation: _build_quote_asset_prices()

Before processing individual pairs, scan the ticker batch for all USDT-quoted
pairs and build a map:

```python
asset → Decimal(price_in_USDT)
```

For each asset with a USDT quote, extract its price from the ticker's `last`
field (the most recent trade price available). This map is then used to convert
non-USDT pairs.

### Why quoteVolume × quote_asset_USDT_price, not baseVolume × base_asset_USDT_price?

- **Simpler**: `quoteVolume` needs one price lookup (quote asset).
- **Correct units**: `quoteVolume` is already in a single denomination (the
  quote asset). One multiplication converts it to USDT.
- **Fewer dependencies**: The base asset's USDT price may or may not be in the
  batch (for  exotic base assets), but the quote asset's price (used for the
  pair itself) is more likely to be listed directly.
- **Aligns with market structure**: On Binance, newer or exotic assets often
  trade *against* established assets (BTC, USDT, BNB, ETH) as the quote, not
  as the base.

## Consequences

**Positive:**
- Non-USDT pairs are now correctly normalized to USDT-equivalent volume.
- No additional API calls required (uses data already fetched).
- Consistent behavior: pairs with insufficient *convertible* volume are filtered
  out, just like USDT pairs with insufficient volume.
- Executable in Phase 1 without waiting for Phase 2.

**Negative / Limitations:**
- Exotic quote assets without a direct USDT listing in the batch will have their
  cross pairs excluded. Example: if a ticker batch contains `RARE/BTC` but no
  `RARE/USDT` or `RARE/BNB` (as a price source), then `RARE/BTC` is excluded.
  In practice, Binance's listing conventions (most pairs quote against USDT or
  BNB) make this rare. If it becomes an issue, Phase 2 can add a fallback
  (e.g. indirect pricing via `RARE/BTC` × `BTC/USDT`).
- Relies on `last` price field being accurate for a 24-hour snapshot. In normal
  operation this is reliable; during extreme market gaps, a single price point
  may not reflect true value, but this is accepted (tick-level staleness checks
  are the evaluator's responsibility in Phase 2).

## Alternatives Considered

**1. Reject all non-USDT pairs (use only USDT in Phase 1) — rejected**
Would work but defeats the purpose: cross pairs are essential for triangular
arbitrage. A triangle requires at least one edge between two non-USDT assets
(e.g. `ETH-BTC-USDT` requires `ETH/BTC`). Filtering them out unconditionally
returns to the Phase 1 bug (zero triangles, always).

**2. Use BNB as an alternative quote asset (rejected)**
Some Binance pairs quote against BNB instead of USDT. Extending the filter to
accept both would complicate the logic and introduce a second asset-specific
decision (why BNB but not others?). Better deferred to Phase 2 if real data
shows it's necessary.

**3. Synthetic price: baseVolume × base_asset_USDT_price (rejected)**
Requires two price lookups (base asset, not just quote asset). More complex, and
If the base asset lacks a USDT listing, we're back to the fallback problem.

## Testing

- `test_pipeline_with_cross_pair_generates_triangle`: Verifies ETH/BTC is
  accepted when converted via `BTC/USDT` price. Includes arithmetic check:
  50,000 BTC × $67,000/BTC = $3.35B (passes $1M threshold).
- `test_pipeline_excludes_cross_pair_below_volume`: Verifies 0.5 BTC volume is
  converted to $33,500 and rejected (below threshold).
- `test_pipeline_excludes_cross_pair_with_missing_quote_price`: Verifies
  DOGE/BTC is excluded (no DOGE/USDT in batch) — not crashed, not
  unconverted, just filtered out.

## References

- **ADR-001**: ExchangeAdapter interface. Establishes that graph.py is
  exchange-agnostic and works with raw ticker dicts.
- **Technical Plan §3 "Flujo de evaluación"**: Shows the volume filter as the
  first step; doesn't yet specify cross-pair handling, but this ADR closes that gap.
- **Fix Phase 1**: "Graph triangle generation" bug. This ADR resolves the
  volume normalization approach within that fix.
