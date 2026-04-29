from datetime import date
from decimal import Decimal

from sqlalchemy import Integer, Numeric
from sqlalchemy.dialects.postgresql import DATE
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class AccountState(Base):
    __tablename__ = "account_state"

    date: Mapped[date] = mapped_column(DATE, primary_key=True)
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    closing_balance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    daily_pnl: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    daily_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    pdt_count_rolling: Mapped[int] = mapped_column(Integer, default=0)
    trades_today: Mapped[int] = mapped_column(Integer, default=0)
    signals_generated: Mapped[int] = mapped_column(Integer, default=0)
    signals_executed: Mapped[int] = mapped_column(Integer, default=0)
    signals_declined: Mapped[int] = mapped_column(Integer, default=0)
    signals_expired: Mapped[int] = mapped_column(Integer, default=0)
    s2_positions_open: Mapped[int] = mapped_column(Integer, default=0)
    total_exposure_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))

    def __repr__(self) -> str:
        return (
            f"<AccountState {self.date} pnl={self.daily_pnl} "
            f"s2_open={self.s2_positions_open}>"
        )
