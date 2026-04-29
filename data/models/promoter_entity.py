import uuid
from datetime import datetime

from sqlalchemy import Boolean, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class PromoterEntity(Base):
    __tablename__ = "promoter_entities"

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    first_seen_edgar: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    sec_enforcement_case: Mapped[bool] = mapped_column(Boolean, default=False)
    enforcement_case_url: Mapped[str | None] = mapped_column(Text)
    current_status: Mapped[str | None] = mapped_column(String(50), default="active")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<PromoterEntity {self.name!r} type={self.type} status={self.current_status}>"
