"""Paper-trade engine.

Mirrors the BrokerClient interface so calling code can swap freely between
real and simulated execution. Records outcomes directly on the signal row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.signal import Signal
from execution.broker.base import (
    AccountInfo,
    MarketClock,
    OrderResult,
    Position,
    Side,
)


PaperBucket = Literal["user_declined", "expired", "system_paper"]


class PaperTradeEngine:
    """Simulated execution. Records to signals table; never calls a broker."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._fills: list[OrderResult] = []
        self._positions: dict[str, Position] = {}
        self._pnl: Decimal = Decimal(0)

    async def record_outcome(
        self,
        signal_id: uuid.UUID,
        bucket: PaperBucket,
        fill_price: Decimal | float,
        decline_reason: str | None = None,
    ) -> Signal | None:
        """Persist outcome on the signal row.

        - USER_DECLINED → fill at the alert's last valid entry price (caller
          passes signal.entry_price_low or midpoint).
        - SIGNAL_EXPIRED → fill at the price at expiry (caller passes the
          live mark observed when the timer ran out).
        - SYSTEM_PAPER → Phase 1, no broker present; fill at signal entry.
        """
        signal = await self.session.get(Signal, signal_id)
        if signal is None:
            logger.warning("paper_trade.record_outcome: signal {} not found", signal_id)
            return None

        signal.outcome = bucket
        signal.paper_entry_price = Decimal(str(fill_price))
        signal.response_at = datetime.now(timezone.utc)
        if decline_reason is not None:
            signal.decline_reason = decline_reason
        if signal.alert_sent_at is not None:
            signal.response_time_seconds = int(
                (signal.response_at - signal.alert_sent_at).total_seconds()
            )
        await self.session.flush()
        logger.info(
            "paper_trade recorded {bucket} signal={sid} fill={price}",
            bucket=bucket,
            sid=signal_id,
            price=signal.paper_entry_price,
        )
        return signal

    @property
    def paper_pnl(self) -> Decimal:
        return self._pnl

    # ------------------------------------------------------------------
    # BrokerClient-compatible surface (no real orders ever leave this box)
    # ------------------------------------------------------------------
    async def submit_market_order(
        self, ticker: str, qty: int, side: Side, fill_price: Decimal | float = Decimal(0)
    ) -> OrderResult:
        return self._record_fill(ticker, qty, side, "market", Decimal(str(fill_price)))

    async def submit_limit_order(
        self, ticker: str, qty: int, side: Side, limit_price: Decimal | float
    ) -> OrderResult:
        return self._record_fill(ticker, qty, side, "limit", Decimal(str(limit_price)))

    async def submit_bracket_order(
        self,
        ticker: str,
        qty: int,
        side: Side,
        entry_limit: Decimal | float,
        stop_price: Decimal | float,
        take_profit: Decimal | float,
    ) -> OrderResult:
        result = self._record_fill(ticker, qty, side, "bracket", Decimal(str(entry_limit)))
        result.stop_price = Decimal(str(stop_price))
        return result

    async def get_account(self) -> AccountInfo:
        return AccountInfo(
            balance=Decimal(0),
            buying_power=Decimal(0),
            portfolio_value=self._pnl,
        )

    async def get_position(self, ticker: str) -> Position | None:
        return self._positions.get(ticker)

    async def get_all_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def get_open_orders(self) -> list[OrderResult]:
        return []

    async def close_position(self, ticker: str) -> OrderResult:
        pos = self._positions.pop(ticker, None)
        qty = pos.qty if pos else 0
        return self._record_fill(ticker, qty, "sell", "market", Decimal(0))

    async def is_market_open(self) -> bool:
        return True

    async def get_clock(self) -> MarketClock:
        return MarketClock(is_open=True, timestamp=datetime.now(timezone.utc))

    def _record_fill(
        self, ticker: str, qty: int, side: Side, kind: str, price: Decimal
    ) -> OrderResult:
        order = OrderResult(
            order_id=f"paper-{uuid.uuid4()}",
            ticker=ticker,
            qty=qty,
            side=side,
            order_type=kind,
            status="filled",
            filled_qty=qty,
            filled_avg_price=price,
            limit_price=price if kind != "market" else None,
            submitted_at=datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc),
        )
        self._fills.append(order)
        existing = self._positions.get(ticker)
        if side == "buy":
            new_qty = (existing.qty if existing else 0) + qty
            self._positions[ticker] = Position(
                ticker=ticker,
                qty=new_qty,
                avg_entry_price=price,
                side="buy",
            )
        else:
            if existing and existing.qty <= qty:
                self._pnl += (price - existing.avg_entry_price) * Decimal(existing.qty)
                self._positions.pop(ticker, None)
        return order
