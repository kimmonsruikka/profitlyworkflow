import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("idx_positions_status_strategy", "status", "strategy"),
        Index("idx_positions_ticker", "ticker"),
    )

    position_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    strategy: Mapped[str] = mapped_column(String(10), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    shares: Mapped[int] = mapped_column(Integer, nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    target1_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    target2_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    unrealized_pnl_r: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    days_held: Mapped[int] = mapped_column(Integer, default=0)
    thesis_category: Mapped[str | None] = mapped_column(String(5))
    thesis_intact: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.signal_id"), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Position {self.strategy} {self.ticker} shares={self.shares} "
            f"status={self.status} unrealized_r={self.unrealized_pnl_r}>"
        )
