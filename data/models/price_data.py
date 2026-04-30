from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Numeric, String
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class PriceData(Base):
    """TimescaleDB hypertable. Composite PK on (ticker, granularity, timestamp).

    `granularity` ('1m' / '5m' / '15m' / '1h' / '1d') was added in
    migration 0007 so the same ticker can have multiple granularities
    cached. Existing rows pre-0007 backfilled to '1m'.
    """

    __tablename__ = "price_data"

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    granularity: Mapped[str] = mapped_column(String(10), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), primary_key=True
    )
    open: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    high: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    low: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    close: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    spread_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    liquidity_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    def __repr__(self) -> str:
        return (
            f"<PriceData {self.ticker} {self.granularity} {self.timestamp} "
            f"close={self.close} vol={self.volume}>"
        )
