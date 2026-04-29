"""Broker abstraction. Concrete implementations live next to this file."""

from __future__ import annotations

import abc
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


Side = Literal["buy", "sell"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AccountInfo(_Schema):
    balance: Decimal
    buying_power: Decimal
    portfolio_value: Decimal
    cash: Decimal | None = None
    pattern_day_trader: bool | None = None


class Position(_Schema):
    ticker: str
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    unrealized_pnl_pct: Decimal | None = None
    side: Side = "buy"


class OrderResult(_Schema):
    order_id: str
    client_order_id: str | None = None
    ticker: str
    qty: int
    side: Side
    order_type: str
    status: str
    filled_qty: int = 0
    filled_avg_price: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    raw: dict | None = None


class MarketClock(_Schema):
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None
    timestamp: datetime | None = None


class BrokerClient(abc.ABC):
    """Implementations must be safe to call concurrently."""

    @abc.abstractmethod
    async def get_account(self) -> AccountInfo: ...

    @abc.abstractmethod
    async def get_position(self, ticker: str) -> Position | None: ...

    @abc.abstractmethod
    async def get_all_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    async def submit_bracket_order(
        self,
        ticker: str,
        qty: int,
        side: Side,
        entry_limit: Decimal | float,
        stop_price: Decimal | float,
        take_profit: Decimal | float,
    ) -> OrderResult: ...

    @abc.abstractmethod
    async def submit_limit_order(
        self, ticker: str, qty: int, side: Side, limit_price: Decimal | float
    ) -> OrderResult: ...

    @abc.abstractmethod
    async def submit_market_order(
        self, ticker: str, qty: int, side: Side
    ) -> OrderResult: ...

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abc.abstractmethod
    async def get_order(self, order_id: str) -> OrderResult: ...

    @abc.abstractmethod
    async def get_open_orders(self) -> list[OrderResult]: ...

    @abc.abstractmethod
    async def close_position(self, ticker: str) -> OrderResult: ...

    @abc.abstractmethod
    async def is_market_open(self) -> bool: ...

    @abc.abstractmethod
    async def get_clock(self) -> MarketClock: ...
