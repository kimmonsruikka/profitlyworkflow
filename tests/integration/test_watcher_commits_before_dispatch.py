"""Regression test for Bug B: watcher must commit the INSERT before
dispatching the Celery task.

If the INSERT and the `process_filing.delay(...)` happen inside the
same uncommitted transaction, the worker (which runs in its own
process and starts its own READ COMMITTED transaction) cannot see the
row, the UPDATE returns rowcount=0, and `processed=false` silently
stays. This test pins the ordering: every dispatch must be preceded
by a commit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_persist_and_queue_commits_before_dispatch(monkeypatch):
    """Every process_filing.delay() call must be preceded by a session
    commit on the per-filing insert session."""
    from ingestion.edgar import rss_watcher as m

    events: list[tuple[str, str]] = []  # (event_type, accession)

    # Outer session — only used for the read-only existing/ticker lookups,
    # both of which we monkeypatch away.
    outer_session = MagicMock()
    outer_session.execute = AsyncMock()
    outer_session.flush = AsyncMock()

    # Per-insert session: every execute() and commit() is recorded so we
    # can assert the ordering relative to delay() calls.
    def make_insert_session():
        sess = MagicMock()

        async def fake_execute(stmt):
            try:
                acc = stmt.compile().params.get("accession_number")
            except Exception:
                acc = None
            events.append(("execute", acc))
            result = MagicMock()
            result.rowcount = 1
            return result

        async def fake_commit():
            events.append(("commit", None))

        async def fake_rollback():
            events.append(("rollback", None))

        sess.execute = AsyncMock(side_effect=fake_execute)
        sess.commit = AsyncMock(side_effect=fake_commit)
        sess.rollback = AsyncMock(side_effect=fake_rollback)
        return sess

    @asynccontextmanager
    async def fake_get_session():
        # Mirrors data.db.get_session — commit on clean exit, rollback on
        # error. We exercise the commit path here.
        sess = make_insert_session()
        try:
            yield sess
            await sess.commit()
        except Exception:
            await sess.rollback()
            raise

    monkeypatch.setattr(m, "get_session", fake_get_session)
    monkeypatch.setattr(
        m, "_existing_accessions", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        m, "_ticker_for_ciks", AsyncMock(return_value={})
    )

    def fake_delay(payload):
        events.append(("delay", payload["accession_number"]))

    monkeypatch.setattr(m.process_filing, "delay", fake_delay)

    filings = [
        {
            "accession_number": "0000111111-26-000001",
            "form_type": "8-K",
            "cik": "0000111111",
            "filed_at": datetime(2026, 4, 29, tzinfo=timezone.utc),
            "company_name": "FOO INC",
            "link": "https://example/1",
        },
        {
            "accession_number": "0000222222-26-000002",
            "form_type": "S-3",
            "cik": "0000222222",
            "filed_at": datetime(2026, 4, 29, tzinfo=timezone.utc),
            "company_name": "BAR CORP",
            "link": "https://example/2",
        },
    ]

    queued = await m.persist_and_queue(outer_session, filings)
    assert queued == 2

    # Filings are processed serially, so each filing's three events
    # (execute, commit, delay) must appear in that exact order with no
    # delay slipping in between execute and commit. We check by walking
    # the global event list per accession.
    accessions = [f["accession_number"] for f in filings]
    expected = []
    for acc in accessions:
        expected.extend([("execute", acc), ("commit", None), ("delay", acc)])
    assert events == expected, (
        f"event ordering wrong\nexpected: {expected}\ngot:      {events}"
    )


@pytest.mark.asyncio
async def test_persist_and_queue_skips_dispatch_when_insert_session_raises(
    monkeypatch,
):
    """If the insert session raises, the celery task must NOT be dispatched
    (otherwise the worker tries to UPDATE a row that was never inserted)."""
    from ingestion.edgar import rss_watcher as m

    delays: list = []

    @asynccontextmanager
    async def failing_get_session():
        sess = MagicMock()
        sess.execute = AsyncMock(side_effect=RuntimeError("db down"))
        sess.commit = AsyncMock()
        sess.rollback = AsyncMock()
        try:
            yield sess
            await sess.commit()
        except Exception:
            await sess.rollback()
            raise

    monkeypatch.setattr(m, "get_session", failing_get_session)
    monkeypatch.setattr(m, "_existing_accessions", AsyncMock(return_value=set()))
    monkeypatch.setattr(m, "_ticker_for_ciks", AsyncMock(return_value={}))
    monkeypatch.setattr(m.process_filing, "delay", lambda p: delays.append(p))

    outer_session = MagicMock()
    outer_session.execute = AsyncMock()
    outer_session.flush = AsyncMock()

    filings = [
        {
            "accession_number": "0000111111-26-000001",
            "form_type": "8-K",
            "cik": "0000111111",
            "filed_at": datetime(2026, 4, 29, tzinfo=timezone.utc),
            "company_name": "FOO INC",
            "link": "https://example/1",
        },
    ]

    with pytest.raises(RuntimeError, match="db down"):
        await m.persist_and_queue(outer_session, filings)

    assert delays == [], "delay() must not be called when the insert fails"
