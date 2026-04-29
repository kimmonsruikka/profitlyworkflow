import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import DATE, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class PromoterCampaign(Base):
    __tablename__ = "promoter_campaigns"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promoter_entities.entity_id"), index=True
    )
    ticker: Mapped[str | None] = mapped_column(
        String(10), ForeignKey("tickers.ticker"), index=True
    )
    launch_date: Mapped[date | None] = mapped_column(DATE)
    end_date: Mapped[date | None] = mapped_column(DATE)
    compensation_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    compensation_type: Mapped[str | None] = mapped_column(String(50))
    source_filing: Mapped[str | None] = mapped_column(Text)
    day1_move_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    peak_move_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    days_to_peak: Mapped[int | None] = mapped_column(Integer)
    decay_speed: Mapped[str | None] = mapped_column(String(20))
    campaign_result: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<PromoterCampaign ticker={self.ticker} launch={self.launch_date} "
            f"result={self.campaign_result}>"
        )
