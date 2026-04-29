from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import case, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.price_data import PriceData
from data.repositories.schemas import PriceBar


_INTERVAL_TO_PG = {
    "1m": "1 minute",
    "5m": "5 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
    "1h": "1 hour",
    "1d": "1 day",
}


def _utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


class PriceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_price_batch(self, records: list[dict]) -> int:
        """Insert with PK conflict ignored — duplicates from overlapping feeds are fine."""
        if not records:
            return 0
        stmt = pg_insert(PriceData).values(records).on_conflict_do_nothing(
            index_elements=[PriceData.ticker, PriceData.timestamp]
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    async def get_ohlcv(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> list[PriceBar]:
        """Aggregate raw bars into the requested interval using time_bucket."""
        pg_interval = _INTERVAL_TO_PG.get(interval)
        if pg_interval is None:
            raise ValueError(f"unsupported interval: {interval}")

        bucket = func.time_bucket(text(f"INTERVAL '{pg_interval}'"), PriceData.timestamp)
        stmt = (
            select(
                literal_column(f"'{ticker}'").label("ticker"),
                bucket.label("timestamp"),
                func.first(PriceData.open, PriceData.timestamp).label("open"),
                func.max(PriceData.high).label("high"),
                func.min(PriceData.low).label("low"),
                func.last(PriceData.close, PriceData.timestamp).label("close"),
                func.sum(PriceData.volume).label("volume"),
            )
            .where(PriceData.ticker == ticker)
            .where(PriceData.timestamp >= start)
            .where(PriceData.timestamp < end)
            .group_by(bucket)
            .order_by(bucket)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            PriceBar(
                ticker=ticker,
                timestamp=r.timestamp,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
            )
            for r in rows
        ]

    async def get_vwap(self, ticker: str, day: date) -> Decimal | None:
        """Volume-weighted average price for the trading day."""
        start = _utc_midnight(day)
        end = start + timedelta(days=1)
        stmt = select(
            func.sum(PriceData.vwap * PriceData.volume).label("num"),
            func.sum(PriceData.volume).label("den"),
        ).where(
            PriceData.ticker == ticker,
            PriceData.timestamp >= start,
            PriceData.timestamp < end,
            PriceData.vwap.isnot(None),
            PriceData.volume.isnot(None),
        )
        row = (await self.session.execute(stmt)).one()
        if not row.den:
            return None
        return Decimal(row.num) / Decimal(row.den)

    async def get_average_volume(
        self, ticker: str, days: int, interval_minutes: int = 15
    ) -> Decimal | None:
        """Average per-bucket volume over the lookback window."""
        end = datetime.utcnow().replace(tzinfo=timezone.utc)
        start = end - timedelta(days=days)
        bucket = func.time_bucket(
            text(f"INTERVAL '{interval_minutes} minutes'"), PriceData.timestamp
        )
        bucket_volumes = (
            select(func.sum(PriceData.volume).label("vol"))
            .where(
                PriceData.ticker == ticker,
                PriceData.timestamp >= start,
                PriceData.timestamp < end,
            )
            .group_by(bucket)
            .subquery()
        )
        stmt = select(func.avg(bucket_volumes.c.vol))
        result = (await self.session.execute(stmt)).scalar_one_or_none()
        return Decimal(result) if result is not None else None

    async def get_historical_spread(
        self, ticker: str, days: int = 90
    ) -> dict[str, Decimal | int]:
        """Summary stats on spread_pct for the lookback window."""
        end = datetime.utcnow().replace(tzinfo=timezone.utc)
        start = end - timedelta(days=days)
        stmt = select(
            func.count(PriceData.spread_pct).label("samples"),
            func.avg(PriceData.spread_pct).label("avg"),
            func.percentile_cont(0.5)
            .within_group(PriceData.spread_pct)
            .label("median"),
            func.percentile_cont(0.95)
            .within_group(PriceData.spread_pct)
            .label("p95"),
            func.max(PriceData.spread_pct).label("max"),
        ).where(
            PriceData.ticker == ticker,
            PriceData.timestamp >= start,
            PriceData.timestamp < end,
            PriceData.spread_pct.isnot(None),
        )
        row = (await self.session.execute(stmt)).one()
        return {
            "samples": row.samples or 0,
            "avg": row.avg,
            "median": row.median,
            "p95": row.p95,
            "max": row.max,
        }
