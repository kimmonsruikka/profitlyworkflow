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

import asyncio
import os
import time
import uuid
from typing import Any

from prefect import flow, get_run_logger, task

from config import constants
from data.db import get_session
from data.repositories.flow_run_log_repo import FlowRunLogRepository
from ingestion.market_data.float_updater import (
    FloatUpdateReport,
    update_floats_for_universe,
)
from ingestion.market_data.polygon_client import PolygonClient


FLOW_NAME = "weekly-float-update"


# Cancellation signal types — KeyboardInterrupt covers Ctrl+C, CancelledError
# covers Prefect / asyncio cancellation. TerminationSignal exists in some
# Prefect versions and is a CancelledError subclass; we add it to the tuple
# only if importable so a Prefect upgrade renaming it doesn't break import.
_CANCELLATION_EXCS: tuple[type[BaseException], ...]
_cancellation_excs: list[type[BaseException]] = [
    KeyboardInterrupt,
    asyncio.CancelledError,
]
try:
    from prefect.exceptions import TerminationSignal  # type: ignore[import-not-found]
    _cancellation_excs.append(TerminationSignal)
except ImportError:
    pass
_CANCELLATION_EXCS = tuple(_cancellation_excs)


def _make_flow_progress_logger(prefect_logger):
    """Per-ticker progress callback for the flow.

    Logs every FLOAT_UPDATE_FLOW_PROGRESS_INTERVAL tickers (and on the
    last row) so journalctl shows incremental progress during the
    ~17h sweep. Without this, the flow looked silent for hours and
    operators concluded it was wedged — see PR #26 investigation.
    """
    started = time.monotonic()
    interval = constants.FLOAT_UPDATE_FLOW_PROGRESS_INTERVAL

    def progress(visited: int, total: int, last_ticker: str) -> None:
        if visited % interval != 0 and visited != total:
            return
        elapsed = time.monotonic() - started
        rate = visited / elapsed if elapsed else 0.0
        remaining = (total - visited) / rate if rate else 0
        prefect_logger.info(
            "float-sweep [%d/%d] last=%s elapsed=%.1fm est_remaining=%.1fm",
            visited, total, last_ticker, elapsed / 60, remaining / 60,
        )

    return progress


@task(name="run-float-update")
async def _run_float_update(progress_callback=None) -> FloatUpdateReport:
    polygon = PolygonClient()
    async with get_session() as session:
        return await update_floats_for_universe(
            session, polygon, progress_callback=progress_callback,
        )


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


def _log_flow_finish_sync(
    flow_run_id: uuid.UUID,
    *,
    status: str,
    error_message: str | None,
) -> None:
    """Cancellation-survivable counterpart to _log_flow_finish.

    Awaiting an async DB call inside an except handler that's already
    propagating CancelledError is a known asyncio footgun — the new
    awaits get cancelled too, the row never gets written, and the
    flow_run_log entry stays at status='running' forever. This sync
    path uses psycopg2 directly with no event-loop dependency.

    Deliberately minimal: one connect, one UPDATE, one commit, one
    close. No pooling, no retries, no transaction abstraction. The
    point is that this MUST run to completion under cancellation.
    """
    import psycopg2  # local import — only loaded when actually needed

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE flow_run_log SET status=%s, completed_at=NOW(), "
                "error_message=%s WHERE flow_run_id=%s",
                (status, error_message, str(flow_run_id)),
            )
        conn.commit()
    finally:
        conn.close()


@flow(name=FLOW_NAME)
async def float_update_flow() -> dict:
    """Refresh float data for every active ticker.

    Returns the report as a dict so the Prefect UI shows the counts.
    """
    logger = get_run_logger()
    flow_run_id = await _log_flow_start()
    progress_cb = _make_flow_progress_logger(logger)

    try:
        report = await _run_float_update(progress_callback=progress_cb)
    except _CANCELLATION_EXCS as exc:
        # Cancellation path — async DB calls would themselves be cancelled
        # mid-await, leaving the row at status='running'. Use the sync
        # helper which doesn't depend on the event loop.
        _log_flow_finish_sync(
            flow_run_id,
            status="cancelled",
            error_message=f"{type(exc).__name__}: cancelled mid-run",
        )
        raise
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
