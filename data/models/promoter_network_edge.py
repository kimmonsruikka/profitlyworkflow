import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class PromoterNetworkEdge(Base):
    __tablename__ = "promoter_network_edges"

    edge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    entity_a: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promoter_entities.entity_id"), index=True
    )
    entity_b: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promoter_entities.entity_id"), index=True
    )
    co_appearance_count: Mapped[int] = mapped_column(Integer, default=1)
    first_co_appearance: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_co_appearance: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    filing_references: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )

    def __repr__(self) -> str:
        return (
            f"<PromoterNetworkEdge {self.entity_a}↔{self.entity_b} "
            f"count={self.co_appearance_count}>"
        )
