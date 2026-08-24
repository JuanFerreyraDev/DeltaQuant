"""Real-time triangular arbitrage opportunity evaluator for DeltaQuant.

Calculates tick-level gross and net returns for triangular arbitrage cycles using
exact ``Decimal`` arithmetic.  Incorporates taker fee rates (supporting nominal and
BNB-discounted rates via ``exchanges.fees``), evaluates executable spreads, and
verifies tick data freshness against a maximum staleness threshold.

Design rationale:
    This module operates 100 % in RAM within the hot path.  It performs no I/O,
    network requests, or database queries during evaluation.  All price inputs
    are supplied as ``BookTicker`` snapshots and fee rates as ``TradingFees`` or
    ``Decimal`` taker rates.

Symbol format contract (ADR-002):
    Symbols must conform to ADR-002: concatenated for USDT pairs (e.g. ``"BTCUSDT"``),
    slash-delimited for cross pairs (e.g. ``"ETH/BTC"``).

Google-style docstrings per ``docs/planning/plan_doc_DQ.md`` §2.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from exchanges.base import BookTicker, TradingFees
from core.graph import Triangle, parse_symbol


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvaluationResult:
    """Result of evaluating a single directional path of an arbitrage triangle.

    Attributes:
        triangle: The canonical ``Triangle`` object being evaluated.
        path: Four-tuple of asset tickers representing the asset conversion path,
            e.g. ``("USDT", "BTC", "ETH", "USDT")``.
        pair_symbols: Three-tuple of market symbols corresponding to leg 1, 2, and 3.
        gross_return: Cumulative gross price multiplier across the 3 legs.
            Strictly of type ``Decimal``.
        net_return: Cumulative net multiplier incorporating taker fee deductions
            across all 3 legs.  Strictly of type ``Decimal``.
        total_fee_rate: Effective total fee rate deducted, computed as
            ``Decimal("1.0") - (net_return / gross_return)`` when gross_return > 0.
            Strictly of type ``Decimal``.
        is_profitable: ``True`` when ``net_return > Decimal("1.0") + safety_margin``
            and ``is_stale`` is ``False``.
        is_stale: ``True`` when any ticker age exceeds ``max_tick_age_ms``.
        max_age_ms: Maximum ticker timestamp age in milliseconds observed across legs.
    """

    triangle: Triangle
    path: tuple[str, str, str, str]
    pair_symbols: tuple[str, str, str]
    gross_return: Decimal
    net_return: Decimal
    total_fee_rate: Decimal
    is_profitable: bool
    is_stale: bool = False
    max_age_ms: int = 0


# ── Helper functions ──────────────────────────────────────────────────────────


def _find_pair_for_assets(triangle: Triangle, asset_x: str, asset_y: str) -> str:
    """Find the symbol in ``triangle`` that connects asset X and asset Y.

    Args:
        triangle: Canonical ``Triangle`` object containing three symbols.
        asset_x: Ticker symbol of the source asset (e.g. ``"USDT"``).
        asset_y: Ticker symbol of the target asset (e.g. ``"BTC"``).

    Returns:
        The matching pair symbol string from ``triangle``.

    Raises:
        ValueError: If no pair in ``triangle`` connects ``asset_x`` and ``asset_y``.
    """
    target = {asset_x, asset_y}
    for pair in (triangle.pair_ab, triangle.pair_bc, triangle.pair_ca):
        parsed = parse_symbol(pair)
        if parsed is not None and set(parsed) == target:
            return pair
    raise ValueError(
        f"No pair in triangle {triangle} connects assets {asset_x} and {asset_y}"
    )


def evaluate_leg(
    asset_x: str,
    asset_y: str,
    pair_symbol: str,
    ticker: BookTicker,
    taker_fee: Decimal,
) -> tuple[Decimal, Decimal]:
    """Calculate gross and net conversion rates for a single trade leg X -> Y.

    Args:
        asset_x: Ticker of the asset being converted from (e.g. ``"USDT"``).
        asset_y: Ticker of the asset being converted to (e.g. ``"BTC"``).
        pair_symbol: Symbol of the market connecting X and Y (e.g. ``"BTCUSDT"``).
        ticker: Real-time ``BookTicker`` snapshot for ``pair_symbol``.
        taker_fee: Taker fee rate as a ``Decimal`` (e.g. ``Decimal("0.00075")``).

    Returns:
        Tuple ``(gross_rate, net_rate)`` both of type ``Decimal``.
        If bid/ask is invalid (<= 0), returns ``(Decimal("0"), Decimal("0"))``.

    Raises:
        TypeError: If ``taker_fee``, ``ticker.bid``, or ``ticker.ask`` is not ``Decimal``.
        ValueError: If ``pair_symbol`` cannot be parsed or does not connect X and Y.
    """
    if not isinstance(taker_fee, Decimal):
        raise TypeError(
            f"taker_fee must be Decimal, got {type(taker_fee).__name__}: {taker_fee!r}"
        )
    if not isinstance(ticker.bid, Decimal):
        raise TypeError(
            f"ticker.bid must be Decimal, got {type(ticker.bid).__name__}: {ticker.bid!r}"
        )
    if not isinstance(ticker.ask, Decimal):
        raise TypeError(
            f"ticker.ask must be Decimal, got {type(ticker.ask).__name__}: {ticker.ask!r}"
        )

    parsed = parse_symbol(pair_symbol)
    if parsed is None:
        raise ValueError(f"Could not parse symbol {pair_symbol!r}")

    base, quote = parsed

    if asset_x == quote and asset_y == base:
        # BUY base using quote -> pay ask price.
        if ticker.ask <= Decimal("0"):
            return Decimal("0"), Decimal("0")
        gross_rate = Decimal("1") / ticker.ask
        net_rate = (Decimal("1") - taker_fee) / ticker.ask
        return gross_rate, net_rate

    elif asset_x == base and asset_y == quote:
        # SELL base for quote -> receive bid price.
        if ticker.bid <= Decimal("0"):
            return Decimal("0"), Decimal("0")
        gross_rate = ticker.bid
        net_rate = ticker.bid * (Decimal("1") - taker_fee)
        return gross_rate, net_rate

    else:
        raise ValueError(
            f"Pair {pair_symbol} ({base}/{quote}) does not connect {asset_x} -> {asset_y}"
        )


# ── Core evaluation ───────────────────────────────────────────────────────────


def evaluate_path(
    triangle: Triangle,
    path: tuple[str, str, str, str],
    tickers: Mapping[str, BookTicker],
    fee_rates: Mapping[str, Decimal | TradingFees],
    safety_margin: Decimal,
    current_time_ms: int | None = None,
    max_tick_age_ms: int | None = None,
) -> EvaluationResult:
    """Evaluate returns and staleness for a specific 4-asset directional path.

    Args:
        triangle: Canonical ``Triangle`` object.
        path: Four-tuple of asset tickers, e.g. ``("USDT", "BTC", "ETH", "USDT")``.
            The first and last asset must be identical (closed cycle).
        tickers: Map of symbol -> ``BookTicker`` snapshots.
        fee_rates: Map of symbol -> ``Decimal`` taker fee rate or ``TradingFees``.
        safety_margin: Required net return margin over ``1.0``, as a ``Decimal``
            (e.g. ``Decimal("0.0010")`` for 0.1 %).
        current_time_ms: Optional current Unix timestamp in ms for staleness check.
        max_tick_age_ms: Maximum allowed tick age in ms (from ``Settings.MAX_TICK_AGE_MS``).

    Returns:
        ``EvaluationResult`` object with exact ``Decimal`` return metrics and staleness flags.

    Raises:
        ValueError: If ``path[0] != path[3]`` or tickers/fee_rates are missing for a leg.
        TypeError: If ``safety_margin`` is not ``Decimal``.
    """
    if not isinstance(safety_margin, Decimal):
        raise TypeError(
            f"safety_margin must be Decimal, got {type(safety_margin).__name__}: {safety_margin!r}"
        )
    if path[0] != path[3]:
        raise ValueError(f"Path must start and end with the same asset: {path}")

    a1, a2, a3, a4 = path
    pair1 = _find_pair_for_assets(triangle, a1, a2)
    pair2 = _find_pair_for_assets(triangle, a2, a3)
    pair3 = _find_pair_for_assets(triangle, a3, a4)
    pair_symbols = (pair1, pair2, pair3)

    gross_factors: list[Decimal] = []
    net_factors: list[Decimal] = []
    max_age_ms = 0
    is_stale = False

    for pair, x, y in zip(pair_symbols, (a1, a2, a3), (a2, a3, a4)):
        if pair not in tickers:
            raise ValueError(f"Missing ticker for symbol {pair}")
        if pair not in fee_rates:
            raise ValueError(f"Missing fee rate for symbol {pair}")

        ticker = tickers[pair]
        fee_entry = fee_rates[pair]
        taker_fee = fee_entry.taker if isinstance(fee_entry, TradingFees) else fee_entry

        # Calculate leg rates
        g, n = evaluate_leg(x, y, pair, ticker, taker_fee)
        gross_factors.append(g)
        net_factors.append(n)

        # Staleness check
        if current_time_ms is not None and max_tick_age_ms is not None:
            age = current_time_ms - ticker.timestamp_ms
            if age > max_age_ms:
                max_age_ms = age
            if age > max_tick_age_ms or age < 0:
                is_stale = True

    gross_return = gross_factors[0] * gross_factors[1] * gross_factors[2]
    net_return = net_factors[0] * net_factors[1] * net_factors[2]

    if gross_return > Decimal("0"):
        total_fee_rate = Decimal("1.0") - (net_return / gross_return)
    else:
        total_fee_rate = Decimal("0")

    is_profitable = (net_return > (Decimal("1.0") + safety_margin)) and not is_stale

    return EvaluationResult(
        triangle=triangle,
        path=path,
        pair_symbols=pair_symbols,
        gross_return=gross_return,
        net_return=net_return,
        total_fee_rate=total_fee_rate,
        is_profitable=is_profitable,
        is_stale=is_stale,
        max_age_ms=max_age_ms,
    )


def evaluate_triangle(
    triangle: Triangle,
    tickers: Mapping[str, BookTicker],
    fee_rates: Mapping[str, Decimal | TradingFees],
    safety_margin: Decimal,
    current_time_ms: int | None = None,
    max_tick_age_ms: int | None = None,
) -> list[EvaluationResult]:
    """Evaluate both directional paths of a triangle starting from USDT.

    Args:
        triangle: Canonical ``Triangle`` object (must contain ``"USDT"``).
        tickers: Map of symbol -> ``BookTicker`` snapshots.
        fee_rates: Map of symbol -> ``Decimal`` taker fee rate or ``TradingFees``.
        safety_margin: Margin required above ``1.0`` (e.g. ``Decimal("0.0010")``).
        current_time_ms: Optional timestamp in ms for staleness checks.
        max_tick_age_ms: Optional staleness threshold in ms.

    Returns:
        List of 2 ``EvaluationResult`` objects (one per directional path),
        sorted by ``net_return`` descending.

    Raises:
        ValueError: If ``"USDT"`` is not in ``triangle``.
    """
    assets = (triangle.asset_a, triangle.asset_b, triangle.asset_c)
    if "USDT" not in assets:
        raise ValueError(f"Triangle {triangle} does not contain USDT")

    non_usdt = [a for a in assets if a != "USDT"]

    # Two paths starting and ending in USDT
    b, c = non_usdt[0], non_usdt[1]
    path1 = ("USDT", b, c, "USDT")
    path2 = ("USDT", c, b, "USDT")

    res1 = evaluate_path(
        triangle, path1, tickers, fee_rates, safety_margin, current_time_ms, max_tick_age_ms
    )
    res2 = evaluate_path(
        triangle, path2, tickers, fee_rates, safety_margin, current_time_ms, max_tick_age_ms
    )

    results = [res1, res2]
    results.sort(key=lambda r: r.net_return, reverse=True)
    return results
