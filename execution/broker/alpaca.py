"""Alpaca implementation of BrokerClient.

Wraps the synchronous alpaca-py TradingClient in asyncio.to_thread() to
preserve the async interface.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from loguru import logger

from config.settings import Settings, settings as default_settings
from execution.broker.base import (
    AccountInfo,
    BrokerClient,
    MarketClock,
    OrderResult,
    Position,
    Side,
)


def _to_int(value: Any) -> int:
    return int(float(value)) if value is not None else 0


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class AlpacaBroker(BrokerClient):
    def __init__(self, settings: Settings | None = None) -> None:
        from alpaca.trading.client import TradingClient

        self._settings = settings or default_settings
        is_paper = "paper" in self._settings.ALPACA_BASE_URL.lower()
        self._client = TradingClient(
            api_key=self._settings.ALPACA_API_KEY,
            secret_key=self._settings.ALPACA_SECRET_KEY,
            paper=is_paper,
            url_override=self._settings.ALPACA_BASE_URL or None,
        )
        logger.info(
            "AlpacaBroker initialized (paper={paper}, base_url={url})",
            paper=is_paper,
            url=self._settings.ALPACA_BASE_URL,
        )

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def get_account(self) -> AccountInfo:
        try:
            acct = await self._run(self._client.get_account)
        except Exception:
            logger.exception("alpaca.get_account failed")
            raise
        return AccountInfo(
            balance=_to_decimal(acct.equity) or Decimal(0),
            buying_power=_to_decimal(acct.buying_power) or Decimal(0),
            portfolio_value=_to_decimal(acct.portfolio_value) or Decimal(0),
            cash=_to_decimal(acct.cash),
            pattern_day_trader=getattr(acct, "pattern_day_trader", None),
        )

    async def get_position(self, ticker: str) -> Position | None:
        try:
            pos = await self._run(self._client.get_open_position, ticker)
        except Exception as exc:
            if "position does not exist" in str(exc).lower() or "404" in str(exc):
                return None
            logger.exception("alpaca.get_position({} ) failed", ticker)
            raise
        return self._position_from_alpaca(pos)

    async def get_all_positions(self) -> list[Position]:
        try:
            rows = await self._run(self._client.get_all_positions)
        except Exception:
            logger.exception("alpaca.get_all_positions failed")
            raise
        return [self._position_from_alpaca(p) for p in rows]

    async def submit_bracket_order(
        self,
        ticker: str,
        qty: int,
        side: Side,
        entry_limit: Decimal | float,
        stop_price: Decimal | float,
        take_profit: Decimal | float,
    ) -> OrderResult:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        request = LimitOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            limit_price=float(entry_limit),
            stop_loss=StopLossRequest(stop_price=float(stop_price)),
            take_profit=TakeProfitRequest(limit_price=float(take_profit)),
        )
        return await self._submit("bracket", request)

    async def submit_limit_order(
        self, ticker: str, qty: int, side: Side, limit_price: Decimal | float
    ) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        request = LimitOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
        )
        return await self._submit("limit", request)

    async def submit_market_order(
        self, ticker: str, qty: int, side: Side
    ) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return await self._submit("market", request)

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self._run(self._client.cancel_order_by_id, order_id)
            return True
        except Exception:
            logger.exception("alpaca.cancel_order({}) failed", order_id)
            return False

    async def get_order(self, order_id: str) -> OrderResult:
        try:
            order = await self._run(self._client.get_order_by_id, order_id)
        except Exception:
            logger.exception("alpaca.get_order({}) failed", order_id)
            raise
        return self._order_from_alpaca(order)

    async def get_open_orders(self) -> list[OrderResult]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        try:
            orders = await self._run(self._client.get_orders, request)
        except Exception:
            logger.exception("alpaca.get_open_orders failed")
            raise
        return [self._order_from_alpaca(o) for o in orders]

    async def close_position(self, ticker: str) -> OrderResult:
        try:
            order = await self._run(self._client.close_position, ticker)
        except Exception:
            logger.exception("alpaca.close_position({}) failed", ticker)
            raise
        return self._order_from_alpaca(order)

    async def is_market_open(self) -> bool:
        clock = await self.get_clock()
        return clock.is_open

    async def get_clock(self) -> MarketClock:
        try:
            clock = await self._run(self._client.get_clock)
        except Exception:
            logger.exception("alpaca.get_clock failed")
            raise
        return MarketClock(
            is_open=bool(clock.is_open),
            next_open=getattr(clock, "next_open", None),
            next_close=getattr(clock, "next_close", None),
            timestamp=getattr(clock, "timestamp", None),
        )

    async def _submit(self, kind: str, request) -> OrderResult:
        try:
            order = await self._run(self._client.submit_order, request)
        except Exception:
            logger.exception("alpaca.submit_order({}) failed", kind)
            raise
        result = self._order_from_alpaca(order)
        logger.info(
            "submitted {kind} order {ticker} qty={qty} side={side} id={id}",
            kind=kind,
            ticker=result.ticker,
            qty=result.qty,
            side=result.side,
            id=result.order_id,
        )
        return result

    @staticmethod
    def _position_from_alpaca(pos: Any) -> Position:
        qty = _to_int(getattr(pos, "qty", 0))
        return Position(
            ticker=pos.symbol,
            qty=qty,
            avg_entry_price=_to_decimal(pos.avg_entry_price) or Decimal(0),
            current_price=_to_decimal(getattr(pos, "current_price", None)),
            market_value=_to_decimal(getattr(pos, "market_value", None)),
            unrealized_pnl=_to_decimal(getattr(pos, "unrealized_pl", None)),
            unrealized_pnl_pct=_to_decimal(getattr(pos, "unrealized_plpc", None)),
            side="sell" if qty < 0 else "buy",
        )

    @staticmethod
    def _order_from_alpaca(order: Any) -> OrderResult:
        side_raw = getattr(order, "side", "buy")
        side = "sell" if str(side_raw).lower().endswith("sell") else "buy"
        return OrderResult(
            order_id=str(order.id),
            client_order_id=getattr(order, "client_order_id", None),
            ticker=getattr(order, "symbol", ""),
            qty=_to_int(getattr(order, "qty", 0)),
            side=side,  # type: ignore[arg-type]
            order_type=str(getattr(order, "order_type", "")),
            status=str(getattr(order, "status", "")),
            filled_qty=_to_int(getattr(order, "filled_qty", 0)),
            filled_avg_price=_to_decimal(getattr(order, "filled_avg_price", None)),
            limit_price=_to_decimal(getattr(order, "limit_price", None)),
            stop_price=_to_decimal(getattr(order, "stop_price", None)),
            submitted_at=getattr(order, "submitted_at", None),
            filled_at=getattr(order, "filled_at", None),
        )
