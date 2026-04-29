"""Order staging, submission, and lifecycle management."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable, Protocol

from loguru import logger

from data.repositories.schemas import SignalSchema
from execution.broker.base import BrokerClient, OrderResult, Side
from execution.paper_trade import PaperTradeEngine


@dataclass(frozen=True)
class StagedOrder:
    ticker: str
    qty: int
    side: Side
    entry_limit: Decimal
    stop_price: Decimal
    take_profit: Decimal
    signal_id: uuid.UUID | None
    risk_dollars: Decimal | None


class RiskGate(Protocol):
    async def __call__(self, staged: StagedOrder) -> tuple[bool, str | None]:
        """Return (allow, rejection_reason)."""


async def _allow_all(_: StagedOrder) -> tuple[bool, str | None]:
    return True, None


def _midpoint(low: Decimal | None, high: Decimal | None) -> Decimal:
    if low is None and high is None:
        raise ValueError("staged order needs at least one entry price bound")
    if low is None:
        return Decimal(high)  # type: ignore[arg-type]
    if high is None:
        return Decimal(low)
    return (Decimal(low) + Decimal(high)) / Decimal(2)


class OrderManager:
    def __init__(
        self,
        broker: BrokerClient | PaperTradeEngine,
        risk_gate: RiskGate | None = None,
    ) -> None:
        self.broker = broker
        self.risk_gate: Callable[[StagedOrder], Awaitable[tuple[bool, str | None]]] = (
            risk_gate or _allow_all
        )

    def stage_order(self, signal: SignalSchema) -> StagedOrder:
        if signal.share_count is None or signal.share_count <= 0:
            raise ValueError(f"signal {signal.signal_id} has no share_count")
        if signal.stop_price is None or signal.target1_price is None:
            raise ValueError(f"signal {signal.signal_id} missing stop/target")

        entry_limit = _midpoint(signal.entry_price_low, signal.entry_price_high)
        return StagedOrder(
            ticker=signal.ticker,
            qty=int(signal.share_count),
            side="buy",
            entry_limit=entry_limit,
            stop_price=Decimal(signal.stop_price),
            take_profit=Decimal(signal.target1_price),
            signal_id=signal.signal_id,
            risk_dollars=Decimal(signal.risk_dollars) if signal.risk_dollars else None,
        )

    async def submit_order(self, staged: StagedOrder) -> OrderResult:
        allowed, reason = await self.risk_gate(staged)
        if not allowed:
            logger.warning(
                "risk gate rejected order {ticker} qty={qty}: {reason}",
                ticker=staged.ticker,
                qty=staged.qty,
                reason=reason,
            )
            raise PermissionError(f"risk gate blocked order: {reason}")

        return await self.broker.submit_bracket_order(
            ticker=staged.ticker,
            qty=staged.qty,
            side=staged.side,
            entry_limit=staged.entry_limit,
            stop_price=staged.stop_price,
            take_profit=staged.take_profit,
        )

    async def monitor_position(self, position_id: uuid.UUID) -> None:
        """Lifecycle hook called by Celery — implementation pending position
        repository wiring. Logs intent so missed cycles are visible."""
        logger.debug("monitor_position called for {}", position_id)
        # TODO: load position, fetch current price, evaluate stop/target/time-stop
        # TODO: emit emergency_exit on S3 shelf detection (per system rules)

    async def emergency_exit(self, ticker: str, reason: str) -> OrderResult:
        logger.warning("emergency_exit {ticker}: {reason}", ticker=ticker, reason=reason)
        return await self.broker.close_position(ticker)
