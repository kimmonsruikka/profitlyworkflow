"""Tests for the weekly-float-update Prefect flow.

Pin the contract that:
  - flow_run_log row is opened on flow start (status='running')
  - flow_run_log row is patched to 'completed' + summary on success
  - flow_run_log row is patched to 'failed' + error_message on failure
  - the summary dict contains the FloatUpdateReport metric keys
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def flow_module(monkeypatch):
    """Import the flow with all its DB / Polygon side-effects stubbed.

    Returns the module so each test can drive its own mocked report or
    error path.
    """
    from flows import float_update_flow as m

    started: list[str] = []
    finished: list[dict] = []

    async def fake_log_start():
        flow_run_id = uuid.uuid4()
        started.append("running")
        # Stash the id on the module so finish-side can correlate.
        m._test_flow_run_id = flow_run_id  # type: ignore[attr-defined]
        return flow_run_id

    async def fake_log_finish(flow_run_id, *, status, summary, error_message):
        finished.append({
            "flow_run_id": flow_run_id,
            "status": status,
            "summary": summary,
            "error_message": error_message,
        })

    monkeypatch.setattr(m, "_log_flow_start", fake_log_start)
    monkeypatch.setattr(m, "_log_flow_finish", fake_log_finish)

    # Calling Flow.fn() outside Prefect's runtime means get_run_logger()
    # raises MissingContextError. Stub it with a noop logger so the
    # body's logger.info() calls are harmless.
    class _NoopLogger:
        def info(self, *_a, **_k): pass
        def warning(self, *_a, **_k): pass
        def error(self, *_a, **_k): pass

    monkeypatch.setattr(m, "get_run_logger", lambda: _NoopLogger())

    return m, started, finished


@pytest.mark.asyncio
async def test_flow_writes_completed_log_row_on_success(flow_module, monkeypatch):
    m, started, finished = flow_module

    from ingestion.market_data.float_updater import FloatUpdateReport

    sample_report = FloatUpdateReport(
        total=42, updated=40, deactivated_oversized=1,
        deactivated_not_found=1, errors=0,
    )

    async def fake_run_float_update():
        return sample_report

    # The Prefect @task wraps _run_float_update — call its underlying fn
    # directly via monkeypatch on the module attribute.
    monkeypatch.setattr(m, "_run_float_update", fake_run_float_update)

    # Call the underlying coroutine directly via .fn so we don't spin
    # up Prefect's runtime (which pulls in cryptography and is heavier
    # than this unit test needs).
    summary = await m.float_update_flow.fn()

    assert started == ["running"]
    assert len(finished) == 1
    fin = finished[0]
    assert fin["status"] == "completed"
    assert fin["error_message"] is None
    assert fin["summary"] == {
        "total": 42,
        "updated": 40,
        "deactivated_oversized": 1,
        "deactivated_not_found": 1,
        "errors": 0,
    }
    assert summary == fin["summary"]


@pytest.mark.asyncio
async def test_flow_writes_failed_log_row_on_exception(flow_module, monkeypatch):
    m, started, finished = flow_module

    async def fake_run_float_update():
        raise RuntimeError("polygon down")

    monkeypatch.setattr(m, "_run_float_update", fake_run_float_update)

    with pytest.raises(RuntimeError, match="polygon down"):
        await m.float_update_flow.fn()

    assert started == ["running"]
    assert len(finished) == 1
    fin = finished[0]
    assert fin["status"] == "failed"
    assert "polygon down" in (fin["error_message"] or "")
    assert fin["summary"] is None


@pytest.mark.asyncio
async def test_flow_run_log_repository_start_and_finish(monkeypatch):
    """End-to-end on the repository: start() inserts a running row,
    finish() patches completed_at + status + summary."""
    from data.repositories.flow_run_log_repo import FlowRunLogRepository

    added: list = []
    executed: list = []

    session = MagicMock()
    session.add = MagicMock(side_effect=lambda r: added.append(r))
    session.flush = AsyncMock()

    async def fake_execute(stmt):
        executed.append(str(stmt))
        return MagicMock()

    session.execute = AsyncMock(side_effect=fake_execute)

    repo = FlowRunLogRepository(session)

    # start() — adds a row, flushes, returns its id
    row_id = await repo.start("test-flow")
    assert len(added) == 1
    assert added[0].flow_name == "test-flow"
    assert added[0].status == "running"
    session.flush.assert_awaited_once()
    assert row_id == added[0].flow_run_id

    # finish() — emits an UPDATE
    await repo.finish(
        row_id,
        status="completed",
        summary={"total": 10, "updated": 9},
        error_message=None,
    )
    assert len(executed) == 1
    assert "update" in executed[0].lower()
