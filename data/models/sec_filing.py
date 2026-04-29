import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Index, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class SecFiling(Base):
    __tablename__ = "sec_filings"
    __table_args__ = (
        Index("idx_sec_filings_filed_processed", "filed_at", "processed"),
        Index("idx_sec_filings_ticker_form", "ticker", "form_type"),
    )

    filing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    ticker: Mapped[str | None] = mapped_column(String(10))
    cik: Mapped[str | None] = mapped_column(String(20))
    filed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    form_type: Mapped[str] = mapped_column(String(20), nullable=False)
    accession_number: Mapped[str | None] = mapped_column(String(50), unique=True)
    item_numbers: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    ir_firm_mentioned: Mapped[str | None] = mapped_column(Text)
    compensation_disclosed: Mapped[bool] = mapped_column(Boolean, default=False)
    compensation_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    s3_effective: Mapped[bool] = mapped_column(Boolean, default=False)
    form4_insider_buy: Mapped[bool] = mapped_column(Boolean, default=False)
    full_text: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<SecFiling {self.form_type} ticker={self.ticker} "
            f"filed_at={self.filed_at} processed={self.processed}>"
        )
