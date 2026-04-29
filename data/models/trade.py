import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("idx_trades_ticker_entry", "ticker", "entry_time"),
        Index("idx_trades_strategy_entry", "strategy", "entry_time"),
    )

    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.signal_id"), index=True
    )
    strategy: Mapped[str] = mapped_column(String(10), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    exit_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    shares: Mapped[int] = mapped_column(Integer, nullable=False)
    pnl_dollars: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    pnl_r: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    hold_minutes: Mapped[int | None] = mapped_column(Integer)
    exit_reason: Mapped[str | None] = mapped_column(String(50))
    mae_dollars: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    mfe_dollars: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    liquidity_score_entry: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    liquidity_score_exit: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    slippage_cents_entry: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    slippage_cents_exit: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    overnight_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    broker: Mapped[str | None] = mapped_column(String(20))
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Trade {self.strategy} {self.ticker} shares={self.shares} "
            f"pnl_r={self.pnl_r} exit={self.exit_reason}>"
        )
