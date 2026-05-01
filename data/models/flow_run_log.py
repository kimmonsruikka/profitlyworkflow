"""Operational history for Prefect flows.

One row per flow run. Started_at is set on flow entry, completed_at +
status + summary on exit. summary is JSONB so each flow can record
its own metric shape without a per-flow schema migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class FlowRunLog(Base):
    __tablename__ = "flow_run_log"

    flow_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    flow_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return (
            f"<FlowRunLog {self.flow_name} {self.status} "
            f"started={self.started_at}>"
        )
