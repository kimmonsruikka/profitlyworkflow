from __future__ import annotations

import sys
import types
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import Settings


def _stub_alpaca_module() -> MagicMock:
    """Insert a fake alpaca-py SDK so AlpacaBroker imports cleanly without the
    real package installed in CI."""
    if "alpaca" in sys.modules:
        return sys.modules["alpaca.trading.client"].TradingClient  # type: ignore[attr-defined]

    alpaca = types.ModuleType("alpaca")
    trading = types.ModuleType("alpaca.trading")
    client_mod = types.ModuleType("alpaca.trading.client")
    enums_mod = types.ModuleType("alpaca.trading.enums")
    requests_mod = types.ModuleType("alpaca.trading.requests")

    trading_client = MagicMock(name="TradingClient")
    client_mod.TradingClient = trading_client

    class _E:
        BUY = "buy"
        SELL = "sell"
        DAY = "day"
        BRACKET = "bracket"
        OPEN = "open"

    enums_mod.OrderSide = _E
    enums_mod.TimeInForce = _E
    enums_mod.OrderClass = _E
    enums_mod.QueryOrderStatus = _E

    class _Req:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    requests_mod.LimitOrderRequest = _Req
    requests_mod.MarketOrderRequest = _Req
    requests_mod.StopLossRequest = _Req
    requests_mod.TakeProfitRequest = _Req
    requests_mod.GetOrdersRequest = _Req

    sys.modules["alpaca"] = alpaca
    sys.modules["alpaca.trading"] = trading
    sys.modules["alpaca.trading.client"] = client_mod
    sys.modules["alpaca.trading.enums"] = enums_mod
    sys.modules["alpaca.trading.requests"] = requests_mod
    return trading_client


@pytest.fixture
def alpaca_stub() -> MagicMock:
    return _stub_alpaca_module()


def test_get_broker_paper_mode_returns_paper_url(alpaca_stub: MagicMock) -> None:
    from execution.broker import PAPER_URL, get_broker

    s = Settings(ENVIRONMENT="development", BROKER_MODE="paper")
    broker = get_broker(settings=s)
    assert broker._settings.ALPACA_BASE_URL == PAPER_URL  # type: ignore[attr-defined]
    assert alpaca_stub.called


def test_is_live_trading_gate_blocks_live_in_staging(alpaca_stub: MagicMock) -> None:
    from execution.broker import PAPER_URL, get_broker

    # BROKER_MODE=live but ENVIRONMENT=staging → must NOT route to live URL
    s = Settings(ENVIRONMENT="staging", BROKER_MODE="live")
    broker = get_broker(settings=s)
    assert broker._settings.ALPACA_BASE_URL == PAPER_URL  # type: ignore[attr-defined]


def test_is_live_trading_gate_allows_production_live(alpaca_stub: MagicMock) -> None:
    from execution.broker import LIVE_URL, get_broker

    s = Settings(ENVIRONMENT="production", BROKER_MODE="live")
    broker = get_broker(settings=s)
    assert broker._settings.ALPACA_BASE_URL == LIVE_URL  # type: ignore[attr-defined]


def test_stage_order_builds_correct_parameters() -> None:
    from data.repositories.schemas import SignalSchema
    from execution.order_manager import OrderManager

    signal = SignalSchema(
        signal_id=uuid.uuid4(),
        strategy="S1",
        ticker="ABCD",
        generated_at=__import__("datetime").datetime.utcnow(),
        confidence_score=Decimal("82"),
        liquidity_score=Decimal("74"),
        entry_price_low=Decimal("4.14"),
        entry_price_high=Decimal("4.28"),
        stop_price=Decimal("3.95"),
        target1_price=Decimal("4.83"),
        target2_price=Decimal("5.52"),
        risk_dollars=Decimal("350"),
        share_count=1820,
    )
    mock_broker = MagicMock()
    mock_broker.submit_bracket_order = AsyncMock()
    manager = OrderManager(broker=mock_broker)

    staged = manager.stage_order(signal)

    assert staged.ticker == "ABCD"
    assert staged.qty == 1820
    assert staged.side == "buy"
    assert staged.entry_limit == Decimal("4.21")  # midpoint of 4.14 and 4.28
    assert staged.stop_price == Decimal("3.95")
    assert staged.take_profit == Decimal("4.83")
    assert staged.risk_dollars == Decimal("350")
    assert staged.signal_id == signal.signal_id


@pytest.mark.asyncio
async def test_submit_order_blocked_by_risk_gate() -> None:
    from execution.order_manager import OrderManager, StagedOrder

    mock_broker = MagicMock()
    mock_broker.submit_bracket_order = AsyncMock()

    async def deny(_):
        return False, "daily loss limit"

    manager = OrderManager(broker=mock_broker, risk_gate=deny)
    staged = StagedOrder(
        ticker="ABCD",
        qty=100,
        side="buy",
        entry_limit=Decimal("4.21"),
        stop_price=Decimal("3.95"),
        take_profit=Decimal("4.83"),
        signal_id=uuid.uuid4(),
        risk_dollars=Decimal("350"),
    )
    with pytest.raises(PermissionError, match="daily loss limit"):
        await manager.submit_order(staged)
    mock_broker.submit_bracket_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_order_passes_through_when_gate_allows() -> None:
    from execution.broker.base import OrderResult
    from execution.order_manager import OrderManager, StagedOrder

    mock_broker = MagicMock()
    expected = OrderResult(
        order_id="abc",
        ticker="ABCD",
        qty=100,
        side="buy",
        order_type="bracket",
        status="accepted",
    )
    mock_broker.submit_bracket_order = AsyncMock(return_value=expected)
    manager = OrderManager(broker=mock_broker)

    staged = StagedOrder(
        ticker="ABCD",
        qty=100,
        side="buy",
        entry_limit=Decimal("4.21"),
        stop_price=Decimal("3.95"),
        take_profit=Decimal("4.83"),
        signal_id=None,
        risk_dollars=None,
    )
    result = await manager.submit_order(staged)
    assert result is expected
    mock_broker.submit_bracket_order.assert_awaited_once_with(
        ticker="ABCD",
        qty=100,
        side="buy",
        entry_limit=Decimal("4.21"),
        stop_price=Decimal("3.95"),
        take_profit=Decimal("4.83"),
    )
