"""Tests for BinanceAdapter live order placement and safety gates.

These tests are designed to fail against plausible wrong implementations:
    - live order placement when BINANCE_TESTNET is disabled
    - market order placement that does not validate against a fresh ticker
    - precision conversion that does not snap to the market's real step/tick sizes
    - response mapping that loses Binance status / fill / fee data
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from exchanges.binance_adapter import BinanceAdapter, _order_result_from_raw


@pytest.fixture
def testnet_settings() -> Settings:
    return Settings(
        BINANCE_API_KEY="real_key_abc123",
        BINANCE_API_SECRET="real_secret_xyz789",
        BINANCE_TESTNET=True,
        TESTNET_BINANCE_API_KEY="testnet_key_abc123",
        TESTNET_BINANCE_API_SECRET="testnet_secret_xyz789",
        TELEGRAM_BOT_TOKEN="111:AAA",
        TELEGRAM_CHAT_ID="123",
        DRY_RUN=False,
    )


@pytest.fixture
def market_rules() -> dict:
    return {
        "BTC/USDT": {
            "symbol": "BTC/USDT",
            "precision": {"amount": 5, "price": 2},
            "info": {
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.00001000",
                        "maxQty": "9000.00000000",
                        "stepSize": "0.00001000",
                    },
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01000000",
                        "maxPrice": "1000000.00000000",
                        "tickSize": "0.01000000",
                    },
                    {
                        "filterType": "MIN_NOTIONAL",
                        "minNotional": "5.00000000",
                    },
                ]
            },
        },
        "ETH/BTC": {
            "symbol": "ETH/BTC",
            "precision": {"amount": 4, "price": 6},
            "info": {
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.00100000",
                        "maxQty": "100000.00000000",
                        "stepSize": "0.00100000",
                    },
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.00000100",
                        "maxPrice": "1000.00000000",
                        "tickSize": "0.00000100",
                    },
                    {
                        "filterType": "MIN_NOTIONAL",
                        "minNotional": "0.00050000",
                    },
                ]
            },
        },
    }


@pytest.fixture
def adapter(testnet_settings, market_rules) -> BinanceAdapter:
    client = MagicMock()
    client.create_order = MagicMock()
    client.fetch_ticker = MagicMock()
    client.markets = market_rules
    client.load_markets = MagicMock(return_value=market_rules)
    return BinanceAdapter(client, testnet_settings)


class TestBinanceAdapterLiveOrders:
    def test_order_result_from_real_expired_fok_payload_maps_zero_fill(self):
        """Regression: real Binance EXPIRED FOK payload must map to filled_qty=0.

        This fixture uses the literal shape captured during Scenario 2:
        info.executedQty="0.00000000", status=EXPIRED, and empty fills array.
        """
        raw = {
            "info": {
                "symbol": "BTCUSDT",
                "orderId": 13281780,
                "orderListId": -1,
                "clientOrderId": "x-TKT5PX2F1dcf8bedb375da97083ab0",
                "transactTime": 1788824513594,
                "price": "78262.88000000",
                "origQty": "0.00007000",
                "executedQty": "0.00000000",
                "origQuoteOrderQty": "0.00000000",
                "cummulativeQuoteQty": "0.00000000",
                "status": "EXPIRED",
                "expiryReason": "UNFILLED_FOK_ORDER_EXPIRED",
                "timeInForce": "FOK",
                "type": "LIMIT",
                "side": "BUY",
                "workingTime": 1788824513594,
                "fills": [],
                "selfTradePreventionMode": "EXPIRE_MAKER",
            },
            "id": "13281780",
            "clientOrderId": "x-TKT5PX2F1dcf8bedb375da97083ab0",
            "timestamp": 1788824513594,
            "datetime": "2026-09-07T23:41:53.594Z",
            "lastTradeTimestamp": None,
            "lastUpdateTimestamp": 1788824513594,
            "symbol": "BTC/USDT",
            "type": "limit",
            "timeInForce": "FOK",
            "postOnly": False,
            "reduceOnly": None,
            "side": "buy",
            "price": 78262.88,
            "triggerPrice": None,
            "amount": 7e-05,
            "cost": 0.0,
            "average": None,
            "filled": 0.0,
            "remaining": 7e-05,
            "status": "expired",
            "fee": None,
            "trades": [],
            "fees": [],
            "stopPrice": None,
            "takeProfitPrice": None,
            "stopLossPrice": None,
        }

        result = _order_result_from_raw("BTCUSDT", raw)

        assert result.status == "EXPIRED"
        assert result.filled_qty == Decimal("0")
        assert result.is_filled is False
        assert result.avg_price == Decimal("0")

    @pytest.mark.asyncio
    async def test_place_fok_order_rejects_when_testnet_disabled(self, market_rules):
        settings = Settings(
            BINANCE_API_KEY="real_key_abc123",
            BINANCE_API_SECRET="real_secret_xyz789",
            BINANCE_TESTNET=True,
            TESTNET_BINANCE_API_KEY="testnet_key_abc123",
            TESTNET_BINANCE_API_SECRET="testnet_secret_xyz789",
            TELEGRAM_BOT_TOKEN="111:AAA",
            TELEGRAM_CHAT_ID="123",
            DRY_RUN=False,
        )
        client = MagicMock()
        client.create_order = MagicMock()
        client.fetch_ticker = MagicMock()
        client.markets = market_rules
        client.load_markets = MagicMock(return_value=market_rules)
        adapter = BinanceAdapter(client, settings)
        adapter._settings = SimpleNamespace(DRY_RUN=False, BINANCE_TESTNET=False)
        adapter._markets_cache = market_rules

        with pytest.raises(PermissionError):
            await adapter.place_fok_order("BTCUSDT", "BUY", Decimal("0.1"), Decimal("65000"))

        client.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_fok_order_normalizes_precision_and_maps_response(self, adapter):
        adapter._markets_cache = adapter._client.markets
        adapter._client.create_order.return_value = {
            "id": "12345",
            "status": "FILLED",
            "filled": "0.00010",
            "average": "65000.01",
            "fee": {"cost": "0.00000010", "currency": "BNB"},
            "info": {"status": "FILLED"},
        }

        result = await adapter.place_fok_order(
            "BTCUSDT",
            "BUY",
            Decimal("0.000105"),
            Decimal("65000.019"),
        )

        adapter._client.create_order.assert_called_once_with(
            "BTC/USDT",
            "limit",
            "buy",
            "0.00010000",
            "65000.01000000",
            {"timeInForce": "FOK"},
        )
        assert result.symbol == "BTCUSDT"
        assert result.order_id == "12345"
        assert result.status == "FILLED"
        assert result.filled_qty == Decimal("0.00010")
        assert result.avg_price == Decimal("65000.01")
        assert result.fee == Decimal("0.00000010")
        assert result.fee_asset == "BNB"
        assert result.is_filled is True

    @pytest.mark.asyncio
    async def test_place_market_order_rejects_when_testnet_disabled(self, market_rules):
        settings = Settings(
            BINANCE_API_KEY="real_key_abc123",
            BINANCE_API_SECRET="real_secret_xyz789",
            BINANCE_TESTNET=True,
            TESTNET_BINANCE_API_KEY="testnet_key_abc123",
            TESTNET_BINANCE_API_SECRET="testnet_secret_xyz789",
            TELEGRAM_BOT_TOKEN="111:AAA",
            TELEGRAM_CHAT_ID="123",
            DRY_RUN=False,
        )
        client = MagicMock()
        client.create_order = MagicMock()
        client.fetch_ticker = MagicMock()
        client.markets = market_rules
        client.load_markets = MagicMock(return_value=market_rules)
        adapter = BinanceAdapter(client, settings)
        adapter._settings = SimpleNamespace(DRY_RUN=False, BINANCE_TESTNET=False)
        adapter._markets_cache = market_rules

        with pytest.raises(PermissionError):
            await adapter.place_market_order("BTCUSDT", "SELL", Decimal("0.1"))

        client.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_market_order_validates_notional_and_maps_response(self, adapter):
        adapter._markets_cache = adapter._client.markets
        adapter._client.fetch_ticker.return_value = {
            "bid": "65000.00",
            "ask": "65000.10",
            "last": "65000.05",
        }
        adapter._client.create_order.return_value = {
            "id": "67890",
            "status": "FILLED",
            "filled": "0.00010",
            "average": "65000.00",
            "fee": {"cost": "0.00000012", "currency": "BNB"},
            "info": {"status": "FILLED"},
        }

        result = await adapter.place_market_order(
            "BTCUSDT",
            "SELL",
            Decimal("0.00010"),
        )

        adapter._client.fetch_ticker.assert_called_once_with("BTC/USDT")
        adapter._client.create_order.assert_called_once_with(
            "BTC/USDT",
            "market",
            "sell",
            "0.00010000",
        )
        assert result.symbol == "BTCUSDT"
        assert result.order_id == "67890"
        assert result.status == "FILLED"
        assert result.filled_qty == Decimal("0.00010")
        assert result.avg_price == Decimal("65000.00")
        assert result.fee == Decimal("0.00000012")
        assert result.is_filled is True

    @pytest.mark.asyncio
    async def test_place_market_order_rejects_below_min_notional(self, adapter):
        adapter._markets_cache = adapter._client.markets
        adapter._client.fetch_ticker.return_value = {
            "bid": "65000.00",
            "ask": "65000.10",
            "last": "65000.05",
        }

        with pytest.raises(ValueError):
            await adapter.place_market_order(
                "BTCUSDT",
                "BUY",
                Decimal("0.00001"),
            )

        adapter._client.create_order.assert_not_called()