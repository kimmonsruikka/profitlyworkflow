import uuid
from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class Underwriter(Base):
    """Nasdaq Capital Market equivalent of promoter_entities.

    Tracks placement-agents / underwriters behind small-cap IPOs and
    follow-on offerings. manipulation_flagged identifies entities named
    in regulatory filings or investigative reporting.
    """

    __tablename__ = "underwriters"
    __table_args__ = (
        Index("idx_underwriters_name", "name"),
        Index("idx_underwriters_flagged", "manipulation_flagged"),
    )

    underwriter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str | None] = mapped_column(String(50))
    first_seen_edgar: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ncm_listing_count: Mapped[int] = mapped_column(Integer, default=0)
    manipulation_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_source: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Underwriter {self.name!r} flagged={self.manipulation_flagged} "
            f"ncm={self.ncm_listing_count}>"
        )
