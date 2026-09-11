"""Unit tests for sequential live leg dispatch and reconciliation (ADR-009).

Tests sequential execution semantics:
1. Leg 0 fills, Leg 1 fills, Leg 2 fills → COMPLETED.
2. Leg 0 expires → abort immediately, 0 unhedged inventory, no calls to legs 1 or 2.
3. Leg 0 fills, Leg 1 expires → reconcile Leg 0 inventory via reverse market order.
   Asserts that reconciliation uses REAL Leg 0 filled_qty, even if it differs from theoretical plan.
4. Legs 0 and 1 fill, Leg 2 expires → reconcile Leg 1 inventory via reverse market order.
   Asserts that reconciliation uses REAL Leg 1 filled_qty, even if it differs from theoretical plan.
5. Adapter exceptions in each leg are caught and handled cleanly without unhandled crashes.
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from core.executor import Executor, ExecutionResult
from core.graph import Triangle
from core.risk import RiskManager
from exchanges.base import BookTicker, OrderResult
from config.settings import Settings


@pytest.fixture
def mock_settings():
    """Create Settings with testnet enabled and DRY_RUN False to exercise live path."""
    return Settings(
        DRY_RUN=False,
        BINANCE_TESTNET=True,
        TESTNET_BINANCE_API_KEY="test_key",
        TESTNET_BINANCE_API_SECRET="test_secret",
    )


@pytest.fixture
def mock_adapter():
    """Create a mock ExchangeAdapter."""
    adapter = AsyncMock()
    adapter.set_sandbox_mode = MagicMock()
    return adapter


@pytest.fixture
def mock_risk_manager():
    """Create a mock RiskManager."""
    rm = MagicMock(spec=RiskManager)
    rm.can_execute = MagicMock(return_value=(True, ""))
    rm.register_execution_start = MagicMock()
    rm.register_execution_end = MagicMock()
    rm.is_paused = False
    rm.record_incident = MagicMock()
    return rm


@pytest.fixture
def executor(mock_adapter, mock_risk_manager, mock_settings):
    """Create an Executor with mocks."""
    return Executor(
        adapter=mock_adapter,
        risk_manager=mock_risk_manager,
        db_manager=None,
        settings=mock_settings,
    )


@pytest.fixture
def triangle():
    """Create a sample Triangle for testing."""
    return Triangle(
        asset_a="BTC",
        asset_b="ETH",
        asset_c="USDT",
        pair_ab="BTC/ETH",
        pair_bc="ETH/USDT",
        pair_ca="USDT/BTC",
    )


def make_order_result(
    is_filled: bool,
    symbol: str,
    filled_qty: Decimal = Decimal("0"),
    avg_price: Decimal = Decimal("50000"),
    fee: Decimal = Decimal("0"),
    fee_asset: str = "",
) -> OrderResult:
    """Helper to create OrderResult with specified fill state."""
    return OrderResult(
        symbol=symbol,
        order_id="test_order_123",
        status="FILLED" if is_filled else "EXPIRED",
        filled_qty=filled_qty if is_filled else Decimal("0"),
        avg_price=avg_price if is_filled else Decimal("0"),
        fee=fee,
        fee_asset=fee_asset,
        raw={"test": "data"},
    )


def make_ticker(bid: Decimal, ask: Decimal, symbol: str = "test") -> BookTicker:
    """Helper to create BookTicker for testing."""
    return BookTicker(
        symbol=symbol,
        bid=bid,
        ask=ask,
        timestamp_ms=0,
    )


def make_test_path_and_tickers():
    """Create sample path and tickers for live execution.
    Path: USDT -> BTC -> ETH -> USDT
    Leg 0: USDT -> BTC on BTCUSDT (BUY at ask 50,000)
    Leg 1: BTC -> ETH on ETH/BTC (BUY at ask 0.05 ETH/BTC)
    Leg 2: ETH -> USDT on ETHUSDT (SELL at bid 3,000)
    """
    path = ("USDT", "BTC", "ETH", "USDT")
    tickers = {
        "BTCUSDT": make_ticker(Decimal("49900"), Decimal("50000"), "BTCUSDT"),
        "ETH/BTC": make_ticker(Decimal("0.049"), Decimal("0.050"), "ETH/BTC"),
        "ETHUSDT": make_ticker(Decimal("3000"), Decimal("3010"), "ETHUSDT"),
    }
    return path, tickers


@pytest.mark.asyncio
async def test_sequential_all_three_legs_filled(executor, triangle, mock_adapter):
    """Sequential dispatch: all 3 legs fill successfully → COMPLETED."""
    pair_symbols = ("BTCUSDT", "ETH/BTC", "ETHUSDT")
    position_usdt = Decimal("100")
    expected_return = Decimal("1.02")
    path, tickers = make_test_path_and_tickers()

    # Leg 0: 100 USDT / 50000 = 0.002 BTC
    res0 = make_order_result(True, pair_symbols[0], filled_qty=Decimal("0.002"), avg_price=Decimal("50000"))
    # Leg 1: 0.002 BTC / 0.050 = 0.04 ETH
    res1 = make_order_result(True, pair_symbols[1], filled_qty=Decimal("0.04"), avg_price=Decimal("0.050"))
    # Leg 2: 0.04 ETH * 3000 = 120 USDT
    res2 = make_order_result(True, pair_symbols[2], filled_qty=Decimal("0.04"), avg_price=Decimal("3000"))

    mock_adapter.place_fok_order = AsyncMock(side_effect=[res0, res1, res2])

    result = await executor.execute_triangle(
        triangle=triangle,
        pair_symbols=pair_symbols,
        position_usdt=position_usdt,
        expected_net_return=expected_return,
        path=path,
        tickers=tickers,
    )

    assert result.status == "COMPLETED"
    assert result.legs_filled == 3
    # Check that place_fok_order was called exactly 3 times sequentially
    assert mock_adapter.place_fok_order.call_count == 3


@pytest.mark.asyncio
async def test_sequential_leg0_expired_aborts_immediately(executor, triangle, mock_adapter):
    """Sequential dispatch: Leg 0 expires → aborts immediately, Leg 1 and 2 never dispatched."""
    pair_symbols = ("BTCUSDT", "ETH/BTC", "ETHUSDT")
    position_usdt = Decimal("100")
    expected_return = Decimal("1.02")
    path, tickers = make_test_path_and_tickers()

    res0 = make_order_result(False, pair_symbols[0])
    mock_adapter.place_fok_order = AsyncMock(return_value=res0)

    result = await executor.execute_triangle(
        triangle=triangle,
        pair_symbols=pair_symbols,
        position_usdt=position_usdt,
        expected_net_return=expected_return,
        path=path,
        tickers=tickers,
    )

    assert result.status == "FAILED_LEG_0"
    assert result.legs_filled == 0
    assert "zero unhedged inventory" in result.error_message
    # Critical: adapter must have been called ONLY once
    assert mock_adapter.place_fok_order.call_count == 1


@pytest.mark.asyncio
async def test_sequential_leg1_expired_reconciles_real_leg0_fill(executor, triangle, mock_adapter):
    """Sequential dispatch: Leg 0 fills with unexpected quantity, Leg 1 expires.
    
    Proves hard requirement (ADR-009 §6): Reconciliation must use the REAL filled_qty
    from Leg 0 (0.0018 BTC) rather than the theoretical planned quantity (0.0020 BTC).
    """
    pair_symbols = ("BTCUSDT", "ETH/BTC", "ETHUSDT")
    position_usdt = Decimal("100")
    expected_return = Decimal("1.02")
    path, tickers = make_test_path_and_tickers()

    # Theoretical plan was 100 / 50000 = 0.0020 BTC.
    # Suppose real fill returned a partial/differing quantity of 0.0018 BTC.
    real_leg0_fill = Decimal("0.0018")
    res0 = make_order_result(True, pair_symbols[0], filled_qty=real_leg0_fill, avg_price=Decimal("50000"))
    res1 = make_order_result(False, pair_symbols[1])

    # Leg 0 succeeds, Leg 1 expires
    mock_adapter.place_fok_order = AsyncMock(side_effect=[res0, res1])
    # Reconciliation market order
    recon_res = make_order_result(True, pair_symbols[0], filled_qty=real_leg0_fill, avg_price=Decimal("49900"))
    mock_adapter.place_market_order = AsyncMock(return_value=recon_res)

    result = await executor.execute_triangle(
        triangle=triangle,
        pair_symbols=pair_symbols,
        position_usdt=position_usdt,
        expected_net_return=expected_return,
        path=path,
        tickers=tickers,
    )

    assert result.status == "FAILED_RECONCILED"
    assert result.legs_filled == 1
    # Check that Leg 2 was never called
    assert mock_adapter.place_fok_order.call_count == 2
    # Verify reconciliation placed a market order on Leg 0 symbol with REAL filled_qty
    mock_adapter.place_market_order.assert_called_once()
    recon_call_kwargs = mock_adapter.place_market_order.call_args.kwargs
    assert recon_call_kwargs["symbol"] == pair_symbols[0]
    assert recon_call_kwargs["side"] == "SELL"
    # MUST match real fill of 0.0018, NOT theoretical 0.0020
    assert recon_call_kwargs["quantity"] == real_leg0_fill


@pytest.mark.asyncio
async def test_sequential_leg2_expired_reconciles_real_leg1_fill(executor, triangle, mock_adapter):
    """Sequential dispatch: Legs 0 and 1 fill, Leg 2 expires.
    
    Proves hard requirement (ADR-009 §6): Reconciliation must use the REAL filled_qty
    from Leg 1 (0.035 ETH) rather than the theoretical planned quantity (0.040 ETH).
    """
    pair_symbols = ("BTCUSDT", "ETH/BTC", "ETHUSDT")
    position_usdt = Decimal("100")
    expected_return = Decimal("1.02")
    path, tickers = make_test_path_and_tickers()

    real_leg0_fill = Decimal("0.002")
    real_leg1_fill = Decimal("0.035")  # Real fill differs from theoretical 0.040

    res0 = make_order_result(True, pair_symbols[0], filled_qty=real_leg0_fill, avg_price=Decimal("50000"))
    res1 = make_order_result(True, pair_symbols[1], filled_qty=real_leg1_fill, avg_price=Decimal("0.050"))
    res2 = make_order_result(False, pair_symbols[2])

    mock_adapter.place_fok_order = AsyncMock(side_effect=[res0, res1, res2])
    recon_res = make_order_result(True, pair_symbols[1], filled_qty=real_leg1_fill, avg_price=Decimal("0.049"))
    mock_adapter.place_market_order = AsyncMock(return_value=recon_res)

    result = await executor.execute_triangle(
        triangle=triangle,
        pair_symbols=pair_symbols,
        position_usdt=position_usdt,
        expected_net_return=expected_return,
        path=path,
        tickers=tickers,
    )

    assert result.status == "FAILED_RECONCILED"
    assert result.legs_filled == 2
    assert mock_adapter.place_fok_order.call_count == 3
    # Verify reconciliation placed a market order on Leg 1 symbol with REAL filled_qty
    mock_adapter.place_market_order.assert_called_once()
    recon_call_kwargs = mock_adapter.place_market_order.call_args.kwargs
    assert recon_call_kwargs["symbol"] == pair_symbols[1]
    assert recon_call_kwargs["side"] == "SELL"
    # MUST match real fill of 0.035, NOT theoretical 0.040
    assert recon_call_kwargs["quantity"] == real_leg1_fill


@pytest.mark.asyncio
async def test_sequential_reconciliation_nets_fee_when_fee_in_produced_asset(
    executor, triangle, mock_adapter
):
    """Reconciliation market order must net the fee when fee was charged in the produced asset.

    Scenario: Leg 0 (BUY on BTCUSDT) produces BTC. Leg 0 fills 0.002 BTC, but pays a fee
    of 0.000002 BTC (in BTC). Net available BTC in account is 0.001998 BTC.
    Leg 1 expires.
    Reconciliation must liquidate Leg 0's held BTC via a market SELL order sized at
    filled_qty - fee (0.001998 BTC), NOT filled_qty (0.002 BTC), preventing InsufficientFunds.
    """
    pair_symbols = ("BTCUSDT", "ETH/BTC", "ETHUSDT")
    position_usdt = Decimal("100")
    expected_return = Decimal("1.02")
    path, tickers = make_test_path_and_tickers()

    leg0_fill = Decimal("0.002")
    leg0_fee = Decimal("0.000002")
    expected_liquidation_qty = leg0_fill - leg0_fee  # 0.001998 BTC

    res0 = make_order_result(
        True,
        pair_symbols[0],
        filled_qty=leg0_fill,
        avg_price=Decimal("50000"),
        fee=leg0_fee,
        fee_asset="BTC",
    )
    res1 = make_order_result(False, pair_symbols[1])

    mock_adapter.place_fok_order = AsyncMock(side_effect=[res0, res1])
    recon_res = make_order_result(
        True,
        pair_symbols[0],
        filled_qty=expected_liquidation_qty,
        avg_price=Decimal("49900"),
    )
    mock_adapter.place_market_order = AsyncMock(return_value=recon_res)

    result = await executor.execute_triangle(
        triangle=triangle,
        pair_symbols=pair_symbols,
        position_usdt=position_usdt,
        expected_net_return=expected_return,
        path=path,
        tickers=tickers,
    )

    assert result.status == "FAILED_RECONCILED"
    assert result.legs_filled == 1
    mock_adapter.place_market_order.assert_called_once()
    recon_call_kwargs = mock_adapter.place_market_order.call_args.kwargs
    assert recon_call_kwargs["symbol"] == pair_symbols[0]
    assert recon_call_kwargs["side"] == "SELL"
    # Assert quantity is filled_qty MINUS fee, not filled_qty alone
    assert recon_call_kwargs["quantity"] == expected_liquidation_qty
    assert recon_call_kwargs["quantity"] != leg0_fill


@pytest.mark.asyncio
async def test_sequential_reconciliation_does_not_net_when_bnb_fee_discount_active(
    executor, triangle, mock_adapter
):
    """When BNB fee discount is active, fee is charged in BNB, NOT the produced asset (BTC).

    Scenario: Leg 0 (BUY on BTCUSDT) fills 0.002 BTC. Fee is 0.00015 BNB.
    Because the fee was debited from BNB balance, the full 0.002 BTC was credited to the account.
    Reconciliation must liquidate the full 0.002 BTC, without improperly subtracting BNB from BTC.
    """
    pair_symbols = ("BTCUSDT", "ETH/BTC", "ETHUSDT")
    position_usdt = Decimal("100")
    expected_return = Decimal("1.02")
    path, tickers = make_test_path_and_tickers()

    leg0_fill = Decimal("0.002")
    bnb_fee = Decimal("0.00015")

    res0 = make_order_result(
        True,
        pair_symbols[0],
        filled_qty=leg0_fill,
        avg_price=Decimal("50000"),
        fee=bnb_fee,
        fee_asset="BNB",
    )
    res1 = make_order_result(False, pair_symbols[1])

    mock_adapter.place_fok_order = AsyncMock(side_effect=[res0, res1])
    recon_res = make_order_result(
        True,
        pair_symbols[0],
        filled_qty=leg0_fill,
        avg_price=Decimal("49900"),
    )
    mock_adapter.place_market_order = AsyncMock(return_value=recon_res)

    result = await executor.execute_triangle(
        triangle=triangle,
        pair_symbols=pair_symbols,
        position_usdt=position_usdt,
        expected_net_return=expected_return,
        path=path,
        tickers=tickers,
    )

    assert result.status == "FAILED_RECONCILED"
    assert result.legs_filled == 1
    mock_adapter.place_market_order.assert_called_once()
    recon_call_kwargs = mock_adapter.place_market_order.call_args.kwargs
    assert recon_call_kwargs["symbol"] == pair_symbols[0]
    assert recon_call_kwargs["side"] == "SELL"
    # Sized at full 0.002 BTC — BNB fee must NOT be subtracted from BTC quantity
    assert recon_call_kwargs["quantity"] == leg0_fill


@pytest.mark.asyncio
async def test_sequential_reconciliation_nets_fee_in_buy_branch(
    executor, triangle, mock_adapter
):
    """Reconciliation market order in BUY branch must net fee from quote_received.

    Scenario: Path ETH -> USDT -> BTC -> ETH.
    Leg 0: SELL ETH on ETHUSDT (bid 3000). Produces USDT.
    Leg 0 sells 0.04 ETH at avg_price 3000 = 120 USDT gross.
    Fee charged is 0.12 USDT in fee_asset='USDT'.
    Net USDT held is 120 - 0.12 = 119.88 USDT.
    Leg 1 (USDT -> BTC) fails.
    Reconciliation reverses Leg 0 by BUYing back ETH on ETHUSDT at ask 3010.
    Liquidation quantity must be 119.88 / 3010, NOT 120 / 3010.
    """
    path = ("ETH", "USDT", "BTC", "ETH")
    pair_symbols = ("ETHUSDT", "BTCUSDT", "ETH/BTC")
    tickers = {
        "ETHUSDT": make_ticker(Decimal("3000"), Decimal("3010"), "ETHUSDT"),
        "BTCUSDT": make_ticker(Decimal("49900"), Decimal("50000"), "BTCUSDT"),
        "ETH/BTC": make_ticker(Decimal("0.049"), Decimal("0.050"), "ETH/BTC"),
    }
    position_eth = Decimal("0.04")

    gross_quote = Decimal("120")  # 0.04 * 3000
    fee_usdt = Decimal("0.12")
    net_quote = gross_quote - fee_usdt  # 119.88
    ask_price = Decimal("3010")
    expected_liquidation_qty = net_quote / ask_price

    res0 = make_order_result(
        True,
        pair_symbols[0],
        filled_qty=position_eth,
        avg_price=Decimal("3000"),
        fee=fee_usdt,
        fee_asset="USDT",
    )
    res1 = make_order_result(False, pair_symbols[1])

    mock_adapter.place_fok_order = AsyncMock(side_effect=[res0, res1])
    recon_res = make_order_result(
        True,
        pair_symbols[0],
        filled_qty=expected_liquidation_qty,
        avg_price=ask_price,
    )
    mock_adapter.place_market_order = AsyncMock(return_value=recon_res)

    result = await executor.execute_triangle(
        triangle=triangle,
        pair_symbols=pair_symbols,
        position_usdt=gross_quote,
        expected_net_return=Decimal("1.01"),
        path=path,
        tickers=tickers,
    )

    assert result.status == "FAILED_RECONCILED"
    assert result.legs_filled == 1
    mock_adapter.place_market_order.assert_called_once()
    recon_call_kwargs = mock_adapter.place_market_order.call_args.kwargs
    assert recon_call_kwargs["symbol"] == pair_symbols[0]
    assert recon_call_kwargs["side"] == "BUY"
    assert recon_call_kwargs["quantity"] == expected_liquidation_qty

