from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.ticker import Ticker
from data.repositories.schemas import TickerSchema


class TickerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_ticker(self, ticker: str) -> TickerSchema | None:
        row = await self.session.get(Ticker, ticker)
        return TickerSchema.model_validate(row) if row else None

    async def get_active_universe(self, float_max: int) -> list[TickerSchema]:
        stmt = (
            select(Ticker)
            .where(Ticker.active.is_(True))
            .where(Ticker.float_shares.isnot(None))
            .where(Ticker.float_shares <= float_max)
            .order_by(Ticker.ticker)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [TickerSchema.model_validate(r) for r in rows]

    async def upsert_ticker(self, ticker_data: dict) -> TickerSchema:
        stmt = (
            pg_insert(Ticker)
            .values(**ticker_data)
            .on_conflict_do_update(
                index_elements=[Ticker.ticker],
                set_={k: v for k, v in ticker_data.items() if k != "ticker"},
            )
            .returning(Ticker)
        )
        row = (await self.session.execute(stmt)).scalar_one()
        return TickerSchema.model_validate(row)

    async def update_float(self, ticker: str, float_shares: int) -> TickerSchema | None:
        row = await self.session.get(Ticker, ticker)
        if row is None:
            return None
        row.float_shares = float_shares
        await self.session.flush()
        return TickerSchema.model_validate(row)
