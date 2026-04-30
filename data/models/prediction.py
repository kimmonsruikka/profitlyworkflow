import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class Prediction(Base):
    """One row per signal evaluation. Immutable.

    Outcomes table closes these out via outcome_id. Confidence is a
    calibrated probability in [0.0, 1.0] (NUMERIC(5,4) — handles 0.0000–9.9999
    so we can detect bad-shape values during dev).
    """

    __tablename__ = "predictions"
    __table_args__ = (
        Index("idx_predictions_ticker_time", "ticker", "created_at"),
        Index(
            "idx_predictions_unresolved",
            "created_at",
            postgresql_where=text("outcome_id IS NULL"),
        ),
    )

    prediction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(40), nullable=False)
    feature_vector: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    scorer_version: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    predicted_window_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_target_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    alert_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    user_decision: Mapped[str | None] = mapped_column(String(20))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    trade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    outcome_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    def __repr__(self) -> str:
        return (
            f"<Prediction {self.ticker} {self.signal_type} "
            f"conf={self.confidence} scorer={self.scorer_version}>"
        )
