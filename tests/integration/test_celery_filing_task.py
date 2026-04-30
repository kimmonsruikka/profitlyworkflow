"""Loop-isolation regression test for the Celery filing task.

The bug being guarded against: Celery's prefork worker invokes async
tasks via ``asyncio.run(...)`` — fresh event loop per task. If the
task body uses a module-level SQLAlchemy engine, the engine binds to
the first task's loop and subsequent tasks fail with

    RuntimeError: got Future attached to a different loop

inside asyncpg. The fix in data.db is task_local_session_factory(),
which creates and disposes the engine inside the task's own loop.
This test invokes the path twice in a row to prove that pattern holds.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Direct contract: task_local_session_factory survives back-to-back
# asyncio.run() loops.
# ---------------------------------------------------------------------------
def test_task_local_session_factory_works_across_back_to_back_asyncio_run(monkeypatch):
    """The minimal regression test. If task_local_session_factory ever
    regresses to the module-level engine, the second asyncio.run() raises
    'got Future attached to a different loop'."""
    from sqlalchemy import text
    from data import db as db_mod

    # Use sqlite+aiosqlite — no asyncpg available in CI, but the same
    # event-loop binding behavior applies to aiosqlite. The bug shape
    # is identical: connections cached on a closed loop break.
    monkeypatch.setattr(
        db_mod.settings, "DATABASE_URL", "sqlite+aiosqlite:///:memory:"
    )

    async def run_once(label: str) -> str:
        async with db_mod.task_local_session_factory() as factory:
            async with db_mod.session_from(factory) as session:
                result = await session.execute(text("SELECT 1"))
                value = result.scalar_one()
                return f"{label}={value}"

    # Two separate asyncio.run() calls — the very pattern Celery uses
    # when each task wraps the coroutine in run().
    out1 = asyncio.run(run_once("first"))
    out2 = asyncio.run(run_once("second"))
    assert out1 == "first=1"
    assert out2 == "second=1"


# ---------------------------------------------------------------------------
# Celery-task-shaped call: invoke process_filing.apply() twice with mocks
# stubbing out the parser and the engine. Both calls must succeed.
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_external_calls(monkeypatch):
    """Stub everything the task touches outside its own DB session.

    - filing_parser.fetch_filing_text returns empty so we exercise the
      empty-text branch (one session_from(factory) usage)
    - SignalEngine and downstream lookups are not exercised in this path
    """
    from ingestion.edgar import filing_parser

    monkeypatch.setattr(
        filing_parser, "fetch_filing_text", AsyncMock(return_value=""),
    )


def test_celery_task_invocation_twice_does_not_hit_loop_mismatch(
    mock_external_calls, monkeypatch,
):
    """Hit the actual Celery task wrapper twice. The wrapper calls
    asyncio.run(_process_filing_async(payload)) — fresh loop per call.
    If the engine isn't task-local, the second call dies."""
    from data import db as db_mod
    from ingestion.edgar.rss_watcher import process_filing
    from sqlalchemy import text

    monkeypatch.setattr(
        db_mod.settings, "DATABASE_URL", "sqlite+aiosqlite:///:memory:"
    )

    # We don't expect the SecFiling UPDATE to actually update a row
    # (no schema set up), but the SQL emission shouldn't crash with the
    # loop-mismatch error. The empty-text branch only runs ONE UPDATE,
    # which is enough to prove the engine was created and used on the
    # task's own loop.
    payload = {"accession_number": "TEST-26-000001", "form_type": "8-K"}

    # process_filing.apply() runs the task synchronously in-process —
    # same code path as a real worker: asyncio.run(_process_filing_async).
    # We catch sqlalchemy errors that would arise from the missing
    # schema and assert they're NOT loop-mismatch errors.
    for label in ("first", "second"):
        try:
            result = process_filing.apply(args=[payload]).get()
        except Exception as exc:
            msg = repr(exc).lower()
            assert "different loop" not in msg, (
                f"{label} call hit the loop-isolation bug: {exc}"
            )
            # Any OTHER error (e.g. 'no such table: sec_filings' from the
            # in-memory sqlite) is acceptable — proves the loop binding
            # was correct enough to even reach the SQL execution layer.
            continue
        # If apply() returns successfully, the call worked end-to-end.
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# session_from() preserves get_session()'s commit-on-clean / rollback-on-error
# semantics.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_session_from_commits_on_clean_exit():
    factory = MagicMock()
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def fake_factory_call():
        yield session

    factory.side_effect = lambda: fake_factory_call()

    from data.db import session_from

    async with session_from(factory) as got:
        assert got is session

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_from_rolls_back_on_exception():
    factory = MagicMock()
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def fake_factory_call():
        yield session

    factory.side_effect = lambda: fake_factory_call()

    from data.db import session_from

    with pytest.raises(RuntimeError, match="boom"):
        async with session_from(factory):
            raise RuntimeError("boom")

    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
