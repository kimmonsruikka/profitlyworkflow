from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.signal import Signal
from data.models.trade import Trade
from data.repositories.schemas import (
    CatalystWinRate,
    SignalSchema,
)


PAPER_BUCKETS = {"user_declined", "expired", "system_paper"}


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_signal(self, signal_data: dict) -> SignalSchema:
        signal = Signal(**signal_data)
        self.session.add(signal)
        await self.session.flush()
        return SignalSchema.model_validate(signal)

    async def update_signal_outcome(
        self,
        signal_id: uuid.UUID,
        outcome: str,
        decline_reason: str | None = None,
    ) -> SignalSchema | None:
        signal = await self.session.get(Signal, signal_id)
        if signal is None:
            return None
        signal.outcome = outcome
        if decline_reason is not None:
            signal.decline_reason = decline_reason
        signal.response_at = datetime.utcnow()
        if signal.alert_sent_at is not None:
            signal.response_time_seconds = int(
                (signal.response_at - signal.alert_sent_at).total_seconds()
            )
        await self.session.flush()
        return SignalSchema.model_validate(signal)

    async def get_recent_signals(
        self, strategy: str, hours: int = 24
    ) -> list[SignalSchema]:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        stmt = (
            select(Signal)
            .where(Signal.strategy == strategy)
            .where(Signal.generated_at >= cutoff)
            .order_by(Signal.generated_at.desc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [SignalSchema.model_validate(r) for r in rows]

    async def get_paper_trades_by_bucket(
        self, bucket: str, days: int = 30
    ) -> list[SignalSchema]:
        if bucket not in PAPER_BUCKETS:
            raise ValueError(f"unknown paper-trade bucket: {bucket}")
        cutoff = datetime.utcnow() - timedelta(days=days)
        stmt = (
            select(Signal)
            .where(Signal.outcome == bucket)
            .where(Signal.generated_at >= cutoff)
            .order_by(Signal.generated_at.desc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [SignalSchema.model_validate(r) for r in rows]

    async def get_win_rate_by_catalyst_type(
        self, strategy: str, days: int = 90
    ) -> list[CatalystWinRate]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        winner = case((Trade.pnl_r > 0, 1), else_=0)

        stmt = (
            select(
                Signal.catalyst_type.label("catalyst_type"),
                func.count(Trade.trade_id).label("sample_size"),
                func.sum(winner).label("winners"),
            )
            .join(Trade, Trade.signal_id == Signal.signal_id)
            .where(Signal.strategy == strategy)
            .where(Signal.generated_at >= cutoff)
            .where(Trade.pnl_r.isnot(None))
            .where(Signal.catalyst_type.isnot(None))
            .group_by(Signal.catalyst_type)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            CatalystWinRate(
                catalyst_type=r.catalyst_type,
                sample_size=r.sample_size or 0,
                win_rate=(float(r.winners or 0) / r.sample_size) if r.sample_size else 0.0,
            )
            for r in rows
        ]

    async def get_signals_pending_response(self) -> list[SignalSchema]:
        stmt = (
            select(Signal)
            .where(Signal.alert_sent_at.isnot(None))
            .where(Signal.outcome.is_(None))
            .order_by(Signal.alert_sent_at.desc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [SignalSchema.model_validate(r) for r in rows]
