import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class GateDecision(Base):
    __tablename__ = "gate_decisions"
    __table_args__ = (
        Index("idx_gate_decisions_signal", "signal_id"),
        Index("idx_gate_decisions_timestamp", "timestamp"),
    )

    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.signal_id")
    )
    rule_triggered: Mapped[str] = mapped_column(String(64), nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    message: Mapped[str | None] = mapped_column(String(500))
    timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<GateDecision rule={self.rule_triggered} approved={self.approved} "
            f"signal={self.signal_id}>"
        )
