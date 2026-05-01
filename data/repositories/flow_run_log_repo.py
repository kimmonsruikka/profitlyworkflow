"""Repository for the flow_run_log operational-history table.

Each Prefect flow opens a row at start (status='running'), then
patches it on completion (status='completed' / 'failed' + summary +
error_message). Designed to survive flow crashes — if the flow dies
between start and completion, the 'running' row stays as evidence
the flow was attempted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.flow_run_log import FlowRunLog


class FlowRunLogRepository:
    """Thin CRUD over flow_run_log."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(self, flow_name: str) -> uuid.UUID:
        """Insert a 'running' row and return its flow_run_id."""
        row = FlowRunLog(
            flow_name=flow_name,
            started_at=datetime.now(timezone.utc),
            status="running",
        )
        self._session.add(row)
        await self._session.flush()
        return row.flow_run_id

    async def finish(
        self,
        flow_run_id: uuid.UUID,
        *,
        status: str,
        summary: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Patch the row with completion data. status is 'completed' or 'failed'."""
        await self._session.execute(
            update(FlowRunLog)
            .where(FlowRunLog.flow_run_id == flow_run_id)
            .values(
                completed_at=datetime.now(timezone.utc),
                status=status,
                summary=summary,
                error_message=error_message,
            )
        )
