"""Prefect flow that runs the float updater weekly.

Schedule: every Sunday at 06:00 ET.

Deploy with (Prefect 3):
    prefect deploy flows/float_update_flow.py:float_update_flow \\
        --name weekly-float-update \\
        --pool default-agent-pool \\
        --cron "0 6 * * 0" \\
        --timezone America/New_York
"""

from __future__ import annotations

from prefect import flow, get_run_logger, task

from data.db import get_session
from ingestion.market_data.float_updater import (
    FloatUpdateReport,
    update_floats_for_universe,
)
from ingestion.market_data.polygon_client import PolygonClient


@task(name="run-float-update")
async def _run_float_update() -> FloatUpdateReport:
    polygon = PolygonClient()
    async with get_session() as session:
        return await update_floats_for_universe(session, polygon)


@flow(name="weekly-float-update")
async def float_update_flow() -> dict:
    """Refresh float data for every active ticker.

    Returns the report as a dict so the Prefect UI shows the counts.
    """
    logger = get_run_logger()
    report = await _run_float_update()
    for line in report.summary_lines():
        logger.info(line)
    return {
        "total": report.total,
        "updated": report.updated,
        "deactivated_oversized": report.deactivated_oversized,
        "deactivated_not_found": report.deactivated_not_found,
        "errors": report.errors,
    }


if __name__ == "__main__":
    import asyncio

    asyncio.run(float_update_flow())
