import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("idx_signals_ticker_generated", "ticker", "generated_at"),
        Index("idx_signals_outcome", "outcome"),
        Index("idx_signals_strategy_generated", "strategy", "generated_at"),
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    strategy: Mapped[str] = mapped_column(String(10), nullable=False)
    s2_category: Mapped[str | None] = mapped_column(String(5))
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    catalyst_type: Mapped[str | None] = mapped_column(String(50))
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    liquidity_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    promoter_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promoter_entities.entity_id"), index=True
    )
    entry_price_low: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    entry_price_high: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    target1_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    target2_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    risk_dollars: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    share_count: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(30))
    decline_reason: Mapped[str | None] = mapped_column(String(100))
    paper_entry_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    alert_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    response_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    response_time_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Signal {self.strategy} {self.ticker} conf={self.confidence_score} "
            f"outcome={self.outcome}>"
        )
