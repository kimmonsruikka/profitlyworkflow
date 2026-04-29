from datetime import datetime

from sqlalchemy import BigInteger, Boolean, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class Ticker(Base):
    __tablename__ = "tickers"

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    company_name: Mapped[str | None] = mapped_column(Text)
    float_shares: Mapped[int | None] = mapped_column(BigInteger)
    exchange: Mapped[str | None] = mapped_column(String(20))
    sector: Mapped[str | None] = mapped_column(String(100))
    first_seen: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Ticker {self.ticker} float={self.float_shares} active={self.active}>"
