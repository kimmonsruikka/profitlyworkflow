"""Prefect flow that runs the float updater weekly.

Schedule: every Sunday at 06:00 ET. Sunday morning means the sweep
finishes Sunday evening, so float data is fresh for Monday market
open. Polygon throttling (5 req/min on the Starter plan) is handled
inside PolygonClient — no explicit sleep here.

Each run writes a row to flow_run_log so the operator can answer
'did the flow run last week?' and 'how long did it take?' from SQL
without bouncing through the Prefect UI.

Deploy with (Prefect 3):
    prefect deploy flows/float_update_flow.py:float_update_flow \\
        --name weekly-float-update \\
        --pool default-agent-pool \\
        --cron "0 6 * * 0" \\
        --timezone America/New_York
"""

from __future__ import annotations

import uuid
from typing import Any

from prefect import flow, get_run_logger, task

from data.db import get_session
from data.repositories.flow_run_log_repo import FlowRunLogRepository
from ingestion.market_data.float_updater import (
    FloatUpdateReport,
    update_floats_for_universe,
)
from ingestion.market_data.polygon_client import PolygonClient


FLOW_NAME = "weekly-float-update"


@task(name="run-float-update")
async def _run_float_update() -> FloatUpdateReport:
    polygon = PolygonClient()
    async with get_session() as session:
        return await update_floats_for_universe(session, polygon)


async def _log_flow_start() -> uuid.UUID:
    async with get_session() as session:
        repo = FlowRunLogRepository(session)
        return await repo.start(FLOW_NAME)


async def _log_flow_finish(
    flow_run_id: uuid.UUID,
    *,
    status: str,
    summary: dict[str, Any] | None,
    error_message: str | None,
) -> None:
    async with get_session() as session:
        repo = FlowRunLogRepository(session)
        await repo.finish(
            flow_run_id,
            status=status,
            summary=summary,
            error_message=error_message,
        )


@flow(name=FLOW_NAME)
async def float_update_flow() -> dict:
    """Refresh float data for every active ticker.

    Returns the report as a dict so the Prefect UI shows the counts.
    """
    # TODO(stale-flow-run): if this flow receives SIGTERM (Ctrl+C, agent
    # restart, OOM), the flow_run_log row stays at status='running'
    # forever — _log_flow_finish never runs. Wrap the body in
    # try/except/finally that catches TerminationSignal / KeyboardInterrupt /
    # asyncio.CancelledError and patches the row to status='cancelled'.
    # Tracked separately; small follow-up PR.
    logger = get_run_logger()
    flow_run_id = await _log_flow_start()

    try:
        report = await _run_float_update()
    except Exception as exc:
        await _log_flow_finish(
            flow_run_id,
            status="failed",
            summary=None,
            error_message=repr(exc),
        )
        raise

    for line in report.summary_lines():
        logger.info(line)

    summary = report.as_dict()
    await _log_flow_finish(
        flow_run_id,
        status="completed",
        summary=summary,
        error_message=None,
    )
    return summary


if __name__ == "__main__":
    import asyncio

    asyncio.run(float_update_flow())
