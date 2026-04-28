from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.account_state import AccountState
from data.models.position import Position
from data.models.trade import Trade
from data.repositories.schemas import (
    DailyPnL,
    Expectancy,
    PositionSchema,
    TradeSchema,
)


def _utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


class TradeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_trade(self, trade_data: dict) -> TradeSchema:
        trade = Trade(**trade_data)
        self.session.add(trade)
        await self.session.flush()
        return TradeSchema.model_validate(trade)

    async def close_trade(
        self, trade_id: uuid.UUID, exit_price: float, exit_reason: str
    ) -> TradeSchema | None:
        trade = await self.session.get(Trade, trade_id)
        if trade is None:
            return None

        exit_time = datetime.utcnow()
        trade.exit_price = Decimal(str(exit_price))
        trade.exit_time = exit_time
        trade.exit_reason = exit_reason

        entry = Decimal(trade.entry_price)
        shares = Decimal(trade.shares)
        pnl = (Decimal(str(exit_price)) - entry) * shares
        trade.pnl_dollars = pnl
        trade.hold_minutes = int((exit_time - trade.entry_time).total_seconds() // 60)

        await self.session.flush()
        return TradeSchema.model_validate(trade)

    async def get_open_positions(self) -> list[PositionSchema]:
        stmt = (
            select(Position)
            .where(Position.status == "open")
            .order_by(Position.entry_time.desc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [PositionSchema.model_validate(r) for r in rows]

    async def get_trades_today(self) -> list[TradeSchema]:
        start = _utc_midnight(date.today())
        end = start + timedelta(days=1)
        stmt = (
            select(Trade)
            .where(Trade.entry_time >= start, Trade.entry_time < end)
            .order_by(Trade.entry_time)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [TradeSchema.model_validate(r) for r in rows]

    async def get_daily_pnl(self) -> DailyPnL:
        today = date.today()
        start = _utc_midnight(today)
        end = start + timedelta(days=1)
        stmt = select(
            func.coalesce(func.sum(Trade.pnl_dollars), 0).label("realized"),
            func.count().label("trade_count"),
        ).where(
            Trade.exit_time.isnot(None),
            Trade.exit_time >= start,
            Trade.exit_time < end,
        )
        row = (await self.session.execute(stmt)).one()
        return DailyPnL(
            date=today,
            realized=Decimal(row.realized or 0),
            trade_count=row.trade_count or 0,
        )

    async def get_expectancy(self, strategy: str, days: int = 90) -> Expectancy:
        cutoff = datetime.utcnow() - timedelta(days=days)
        stmt = select(Trade.pnl_r).where(
            Trade.strategy == strategy,
            Trade.entry_time >= cutoff,
            Trade.pnl_r.isnot(None),
        )
        rs = [float(r) for (r,) in (await self.session.execute(stmt)).all()]
        n = len(rs)
        if n == 0:
            return Expectancy(
                strategy=strategy,
                sample_size=0,
                win_rate=0.0,
                avg_win_r=0.0,
                avg_loss_r=0.0,
                expectancy_r=0.0,
            )

        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        win_rate = len(wins) / n
        loss_rate = len(losses) / n
        avg_win = (sum(wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(losses) / len(losses)) if losses else 0.0
        expectancy = win_rate * avg_win + loss_rate * avg_loss
        return Expectancy(
            strategy=strategy,
            sample_size=n,
            win_rate=win_rate,
            avg_win_r=avg_win,
            avg_loss_r=avg_loss,
            expectancy_r=expectancy,
        )

    async def get_consecutive_losses(self) -> int:
        """Count back from the latest closed trade until a non-loss is seen."""
        stmt = (
            select(Trade.pnl_r)
            .where(Trade.exit_time.isnot(None), Trade.pnl_r.isnot(None))
            .order_by(desc(Trade.exit_time))
        )
        rs = [float(r) for (r,) in (await self.session.execute(stmt)).all()]
        count = 0
        for r in rs:
            if r < 0:
                count += 1
            else:
                break
        return count

    async def update_account_state(self, day: date, data: dict) -> None:
        stmt = (
            pg_insert(AccountState)
            .values(date=day, **data)
            .on_conflict_do_update(
                index_elements=[AccountState.date],
                set_=data,
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()
