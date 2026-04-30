import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class Outcome(Base):
    """One per resolved prediction. UNIQUE(prediction_id) enforces 1:1.

    All percentage fields are signed (negative = adverse / loss). The
    outcome_label is set by the resolution flow per OUTCOME_LABEL_RULES
    in config/constants.py.
    """

    __tablename__ = "outcomes"
    __table_args__ = (
        UniqueConstraint("prediction_id", name="uq_outcomes_prediction_id"),
        Index("idx_outcomes_label", "outcome_label"),
    )

    outcome_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    prediction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("predictions.prediction_id"),
        nullable=False,
    )
    resolved_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    window_close_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    max_favorable_excursion_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    max_adverse_excursion_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    realized_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    paper_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    hit_target: Mapped[bool | None] = mapped_column(Boolean)
    hit_stop: Mapped[bool | None] = mapped_column(Boolean)
    outcome_label: Mapped[str] = mapped_column(String(20), nullable=False)
    price_data_source: Mapped[str] = mapped_column(String(40), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<Outcome {self.outcome_label} pred={self.prediction_id} "
            f"realized={self.realized_return_pct}>"
        )
