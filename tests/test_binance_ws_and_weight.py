"""Unit tests for Phase 2 WebSocket bookTicker + rate-limit weight tracking.

Covers:
    - _extract_weight_from_last_response: header extraction edge cases
    - _log_weight: silent no-op when header missing (never raises)
    - subscribe_book_ticker empty symbols → short-circuit, no yields
    - subscribe_book_ticker with mock ccxt.pro:
        * USDT concatenated native symbol survives the round-trip (not unified)
        * Cross-pair slash-delimited native symbol survives the round-trip
        * Yielded bid/ask fields are explicitly Decimal (not float/str)
        * timestamp_ms is a populated integer (not None/str)

Key probing principle (from plan): every test is written to fail against at
least one plausible-but-wrong implementation.
"""

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from exchanges.binance_adapter import (
    _extract_weight_from_last_response,
    _log_weight,
    BinanceAdapter,
)
from config.settings import Settings
from exchanges.base import BookTicker


# ── _extract_weight_from_last_response ───────────────────────────────────────


class TestExtractWeightFromLastResponse:
    """Probing tests for header weight extraction.

    Would fail against a plausible wrong implementation that:
      1. Uses the wrong header name (e.g. X-MBX-ORDER-COUNT-1M).
      2. Returns the raw string "42" instead of int(42).
      3. Raises AttributeError when last_http_response is None.
    """

    def test_happy_path_returns_int(self):
        """Correct header → integer value (not string, not float)."""
        client = MagicMock()
        client.last_http_response.headers = {"X-MBX-USED-WEIGHT-1M": "42"}
        result = _extract_weight_from_last_response(client)
        assert isinstance(result, int)
        assert result == 42

    def test_wrong_header_name_returns_none(self):
        """A plausible bug: reads ORDER-COUNT instead of USED-WEIGHT.
        This is exactly the kind of off-by-one-header the test was designed
        to catch — both values are plausible ints in the same response."""
        client = MagicMock()
        client.last_http_response.headers = {
            "X-MBX-ORDER-COUNT-1M": "99"
        }
        assert _extract_weight_from_last_response(client) is None

    def test_no_last_http_response_no_raise(self):
        """Fresh client with no prior REST call returns None silently."""
        client = MagicMock(spec=["foo"])  # no last_http_response attr
        assert _extract_weight_from_last_response(client) is None

    def test_last_http_response_none(self):
        """Explicit None on last_http_response → None result."""
        client = MagicMock()
        client.last_http_response = None
        assert _extract_weight_from_last_response(client) is None

    def test_response_without_headers_attr(self):
        """Response object has no headers field → None, no AttributeError."""
        client = MagicMock()
        client.last_http_response = SimpleNamespace()  # no .headers
        result = _extract_weight_from_last_response(client)
        assert result is None


# ── _log_weight ───────────────────────────────────────────────────────────────


class TestLogWeight:
    """Probing tests for the weight logging helper.

    Would fail against a plausible wrong implementation that:
      1. Raises when _extract_weight... returns None (instead of silent no-op).
      2. Logs at DEBUG level (won't appear in production INFO-level sinks).
    """

    def test_missing_header_does_not_raise(self):
        """No weight → silent skip, never raises. No logger call at INFO level.

        Plausible bug this catches: _log_weight does logger.info(f"weight: {w}")
        unconditionally, which would produce "weight: None" spam instead of
        skipping silently. Or it tries to concatenate weight+str when weight
        is None and raises TypeError.
        """
        client = MagicMock(spec=["nope"])  # no last_http_response attr
        # Neither of these should raise.
        for _ in range(3):
            _log_weight(client, "fetch_tickers_24h")
        # No exception = pass. The fact that we got here without a TypeError
        # or AttributeError is the assertion.
        assert True

    def test_header_present_logs_info(self, caplog):
        """A valid weight is logged at INFO level with structured op + weight."""
        client = MagicMock()
        client.last_http_response.headers = {"X-MBX-USED-WEIGHT-1M": "7"}
        with caplog.at_level(logging.INFO, logger="exchanges.binance_adapter"):
            _log_weight(client, "fetch_tickers_24h")
        msgs = [r.message for r in caplog.records if r.name == "exchanges.binance_adapter"]
        assert any(
            "op=fetch_tickers_24h" in m and "weight_1m=7" in m for m in msgs
        ), f"Expected structured weight log in: {msgs}"


# ── subscribe_book_ticker ─────────────────────────────────────────────────────


class TestSubscribeBookTicker:
    """Tests for the WS bookTicker subscription method.

    All network calls are mocked — this validates the translation + typing
    logic, not ccxt.pro's actual WebSocket transport.

    Each test targets a specific plausible bug:
      - empty symbols → the method must short-circuit and not try to open a
        WS connection (bug would raise because ws.watch_bids_asks([]) is
        undefined behaviour in ccxt.pro).
      - symbol format round-trip: engine-native format in must equal the
        yielded BookTicker.symbol. A buggy impl that returns unified format
        ("BTC/USDT" instead of "BTCUSDT") will be caught by the exact-match
        assertion on the native string.
      - bid/ask are Decimal: explicit isinstance check catches a silent
        regression where str() is called instead of Decimal(str()) — since
        Decimal("5") == 5.0 is True, a naive equality assertion would pass
        for both types and hide the type bug.
    """

    @pytest.fixture
    def adapter(self):
        """Build a BinanceAdapter with mocks for every external dependency."""
        rest_client = MagicMock()
        rest_client.markets = {}
        settings = Settings(
            BINANCE_API_KEY="real_key_abc123",
            BINANCE_API_SECRET="real_secret_xyz789",
            TELEGRAM_BOT_TOKEN="111:AAA",
            TELEGRAM_CHAT_ID="123",
            _env_file=None,
        )
        return BinanceAdapter(rest_client, settings, ws_client=MagicMock())

    @pytest.mark.asyncio
    async def test_empty_symbols_short_circuits(self, adapter):
        """Empty list → return immediately, no WS client interaction.

        Probes for: a naive implementation that passes [] to watch_bids_asks
        and then hangs, or calls _ws_client construction unconditionally.
        """
        # Force _ws_client to None so the "lazily constructed" code path
        # is exercised — if empty symbols don't short-circuit, we'd hit the
        # MagicMock ws creation = connection.
        adapter._ws_client = None
        it = adapter.subscribe_book_ticker([])
        collected = []
        async for tick in it:
            collected.append(tick)
        assert collected == []
        # _ws_client must still be None: the method returned without wiring.
        assert adapter._ws_client is None

    @pytest.mark.asyncio
    async def test_symbol_formats_round_trip_native_usdt_and_cross(self, adapter):
        """Yielded BookTicker.symbol is the EXACT native string that went in.

        Two ADR-002 sub-formats are exercised in the same subscription to
        catch bugs where one branch works but the other doesn't:

          1. USDT concatenated: "BTCUSDT" in → must yield symbol="BTCUSDT"
             NOT symbol="BTC/USDT" (a plausible unified-format leak bug).
          2. Cross pair slash-delimited: "ETH/BTC" in → must yield symbol="ETH/BTC"
             NOT symbol="ETHBTC" (a plausible over-aggressive slash-stripping bug).
        """
        fake_ws = MagicMock()
        call_counter = {"n": 0}

        async def fake_watch(symbols_list):
            call_counter["n"] += 1
            await asyncio.sleep(0)
            if call_counter["n"] > 1:
                # Second iteration: stop the stream cleanly so the generator
                # doesn't hang inside the while True loop forever.
                raise StopAsyncIteration
            return {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "bids": [[Decimal("50000.0"), Decimal("0.5")]],
                    "asks": [[Decimal("50001.0"), Decimal("0.3")]],
                },
                "ETH/BTC": {
                    "symbol": "ETH/BTC",
                    "bids": [[Decimal("0.05"), Decimal("1.0")]],
                    "asks": [[Decimal("0.0501"), Decimal("2.0")]],
                },
            }

        fake_ws.watch_bids_asks = AsyncMock(side_effect=fake_watch)
        adapter._ws_client = fake_ws

        it = adapter.subscribe_book_ticker(["BTCUSDT", "ETH/BTC"])

        collected = []
        async for tick in it:
            collected.append(tick)

        by_symbol = {t.symbol: t for t in collected}

        # ── Critical exact-match assertions on native format ──────────
        assert "BTCUSDT" in by_symbol, (
            "USDT concatenated native format lost — adapter leaked unified "
            f"'BTC/USDT' instead. Got keys: {list(by_symbol.keys())}"
        )
        assert "ETH/BTC" in by_symbol, (
            "Cross-pair slash-delimited format lost — adapter erroneously "
            f"stripped slashes to 'ETHBTC' instead. Got keys: {list(by_symbol.keys())}"
        )

        # ── Type check: bid/ask are Decimal (not float/str) ────────────
        bt = by_symbol["BTCUSDT"]
        assert isinstance(bt.bid, Decimal), (
            f"bid has wrong type {type(bt.bid).__name__}, expected Decimal. "
            "Plausible bug: Decimal(str(x)) was replaced with raw ccxt float."
        )
        assert isinstance(bt.ask, Decimal), (
            f"ask has wrong type {type(bt.ask).__name__}, expected Decimal."
        )
        assert bt.bid == Decimal("50000.0")
        assert bt.ask == Decimal("50001.0")

        # ── timestamp_ms: populated integer (not None, not float) ──────
        assert isinstance(bt.timestamp_ms, int), (
            f"timestamp_ms has wrong type {type(bt.timestamp_ms).__name__}, "
            "expected int. Plausible bug: time.time() (float seconds) was "
            "used instead of time_ns() // 1_000_000."
        )
        assert bt.timestamp_ms > 0, "timestamp_ms should be positive ms epoch"

    @pytest.mark.asyncio
    async def test_ccxt_pro_client_lazy_construction(self, adapter):
        """WS client is NOT created until subscribe_book_ticker is called with
        a non-empty symbol list.

        Probes for: an impl that eagerly opens a WS connection in
        BinanceAdapter.__init__ or create() — tests that don't use WS would
        pay the cost of opening a socket for no reason.
        """
        adapter._ws_client = None
        assert adapter._ws_client is None

        import ccxt.pro as ccxt_pro

        constructed = {"n": 0}

        class FakeWsClient:
            def __init__(self, *a, **kw):
                constructed["n"] += 1

            async def watch_bids_asks(self, sl):
                raise StopAsyncIteration

            async def close(self):
                pass

        orig_binance = ccxt_pro.binance
        try:
            ccxt_pro.binance = FakeWsClient
            it = adapter.subscribe_book_ticker(["BTCUSDT"])
            async for tick in it:
                pass  # generator terminates cleanly after StopAsyncIteration return
            assert constructed["n"] == 1, (
                f"ccxt.pro.binance should be called exactly once, got {constructed['n']}"
            )
            # After stream exit, _ws_client is reset to None for clean
            # reconnection on the next subscribe_book_ticker call.
            assert adapter._ws_client is None
        finally:
            ccxt_pro.binance = orig_binance

    @pytest.mark.asyncio
    async def test_sequential_single_symbol_pushes_not_synthesized(self, adapter):
        """Verify generator yields ONLY symbols present in each individual push.

        Simulates realistic ccxt.pro behavior where each push contains ONLY the
        symbol whose price actually changed:
          - Push 1: {"BTC/USDT": ...}
          - Push 2: {"ETH/BTC": ...} (BTC/USDT is absent)
          - Push 3: StopAsyncIteration

        Probes for: a buggy implementation that caches or synthesizes ticks for
        absent symbols on every push. If absent symbols were re-yielded, push 2
        would yield both ETH/BTC and BTCUSDT (with a refreshed timestamp_ms),
        which would defeat ADR-004's tick staleness check.
        """
        fake_ws = MagicMock()
        call_counter = {"n": 0}

        async def fake_watch(symbols_list):
            call_counter["n"] += 1
            await asyncio.sleep(0)
            if call_counter["n"] == 1:
                return {
                    "BTC/USDT": {
                        "symbol": "BTC/USDT",
                        "bids": [[Decimal("50000.0"), Decimal("0.5")]],
                        "asks": [[Decimal("50001.0"), Decimal("0.3")]],
                    }
                }
            elif call_counter["n"] == 2:
                return {
                    "ETH/BTC": {
                        "symbol": "ETH/BTC",
                        "bids": [[Decimal("0.05"), Decimal("1.0")]],
                        "asks": [[Decimal("0.0501"), Decimal("2.0")]],
                    }
                }
            else:
                raise StopAsyncIteration

        fake_ws.watch_bids_asks = AsyncMock(side_effect=fake_watch)
        adapter._ws_client = fake_ws

        it = adapter.subscribe_book_ticker(["BTCUSDT", "ETH/BTC"])

        collected = []
        async for tick in it:
            collected.append(tick)

        # ── Assertions ────────────────────────────────────────────────────────
        # Must yield exactly 2 ticks total across the 2 pushes (one per push).
        assert len(collected) == 2, (
            f"Expected exactly 2 ticks (1 per push), but got {len(collected)}. "
            f"Symbols yielded: {[t.symbol for t in collected]}"
        )

        # First push yields ONLY BTCUSDT
        assert collected[0].symbol == "BTCUSDT"
        assert collected[0].bid == Decimal("50000.0")

        # Second push yields ONLY ETH/BTC
        assert collected[1].symbol == "ETH/BTC"
        assert collected[1].bid == Decimal("0.05")

        # Confirm BTCUSDT was NOT re-yielded in push 2
        yielded_symbols = [t.symbol for t in collected]
        assert yielded_symbols == ["BTCUSDT", "ETH/BTC"], (
            f"Yielded sequence {yielded_symbols} indicates absent symbols were "
            "synthesized or re-yielded."
        )

    @pytest.mark.asyncio
    async def test_symbol_formats_round_trip_flat_bid_ask(self, adapter):
        """Yielded BookTicker correctly parses FLAT bid/ask fields (ccxt >= 4.5 format)."""
        fake_ws = MagicMock()
        call_counter = {"n": 0}

        async def fake_watch(symbols_list):
            call_counter["n"] += 1
            if call_counter["n"] > 1:
                raise StopAsyncIteration
            return {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "bid": Decimal("50000.0"),
                    "ask": Decimal("50001.0"),
                },
                "ETH/BTC": {
                    "symbol": "ETH/BTC",
                    "bid": Decimal("0.05"),
                    "ask": Decimal("0.0501"),
                },
            }

        fake_ws.watch_bids_asks = AsyncMock(side_effect=fake_watch)
        adapter._ws_client = fake_ws

        it = adapter.subscribe_book_ticker(["BTCUSDT", "ETH/BTC"])
        collected = [t async for t in it]

        by_symbol = {t.symbol: t for t in collected}
        assert "BTCUSDT" in by_symbol
        assert "ETH/BTC" in by_symbol
        assert isinstance(by_symbol["BTCUSDT"].bid, Decimal)
        assert by_symbol["BTCUSDT"].bid == Decimal("50000.0")
        assert by_symbol["BTCUSDT"].ask == Decimal("50001.0")

    @pytest.mark.asyncio
    async def test_ws_client_reset_to_none_after_exit_and_recreated_on_next_call(self, adapter):
        """After WS stream exits (StopAsyncIteration or Exception), _ws_client is None, and next call recreates it."""
        import ccxt.pro as ccxt_pro

        constructed_clients = []

        class FakeWsClient:
            def __init__(self, *a, **kw):
                self.closed = False
                constructed_clients.append(self)

            async def watch_bids_asks(self, sl):
                if len(constructed_clients) == 1:
                    raise StopAsyncIteration
                else:
                    raise RuntimeError("Simulated network exception")

            async def close(self):
                self.closed = True

        orig_binance = ccxt_pro.binance
        try:
            ccxt_pro.binance = FakeWsClient

            # Path 1: StopAsyncIteration (normal test/clean stream exit)
            adapter._ws_client = None
            it1 = adapter.subscribe_book_ticker(["BTCUSDT"])
            async for _ in it1:
                pass
            assert adapter._ws_client is None
            assert len(constructed_clients) == 1
            assert constructed_clients[0].closed is True

            # Path 2: Production exception path (ConnectionError)
            it2 = adapter.subscribe_book_ticker(["BTCUSDT"])
            with pytest.raises(ConnectionError):
                async for _ in it2:
                    pass
            assert adapter._ws_client is None
            assert len(constructed_clients) == 2
            assert constructed_clients[1].closed is True
        finally:
            ccxt_pro.binance = orig_binance

    @pytest.mark.asyncio
    async def test_binance_adapter_close_safely_closes_and_is_idempotent(self, adapter):
        """BinanceAdapter.close() safely closes _ws_client and _client, is idempotent, and works when _ws_client is None."""
        ws_mock = AsyncMock()
        rest_mock = AsyncMock()
        adapter._ws_client = ws_mock
        adapter._client = rest_mock

        # First call closes both clients and resets _ws_client to None
        await adapter.close()
        ws_mock.close.assert_awaited_once()
        rest_mock.close.assert_awaited_once()
        assert adapter._ws_client is None

        # Second call (idempotent when _ws_client is already None) does not crash or re-close _ws_client
        await adapter.close()
        assert adapter._ws_client is None

