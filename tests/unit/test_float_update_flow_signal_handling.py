"""Signal-handling regression tests for float_update_flow.

The bug being guarded against: a Ctrl+C / SIGTERM / Prefect cancel
mid-flow leaves the flow_run_log row at status='running' forever
because the async _log_flow_finish() call inside the except handler
gets cancelled itself (asyncio footgun). The fix routes cancellation
exits through a synchronous psycopg2 helper that doesn't depend on
the event loop.

Five cases:
  1. KeyboardInterrupt mid-run → sync helper writes 'cancelled'
  2. asyncio.CancelledError mid-run → sync helper writes 'cancelled'
  3. TerminationSignal (if importable) mid-run → 'cancelled'
  4. Generic Exception mid-run → async helper writes 'failed'
  5. Happy path → async helper writes 'completed' (regression check)

The sync helper itself is also tested directly — psycopg2 stubbed,
asserts the SQL shape and the connect/commit/close lifecycle.
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Test fixture: load the flow module with all DB / Polygon side effects
# stubbed and both _log_flow_finish helpers tracked.
# ---------------------------------------------------------------------------
@pytest.fixture
def flow_module(monkeypatch):
    from flows import float_update_flow as m

    started_ids: list[uuid.UUID] = []
    async_finishes: list[dict] = []
    sync_finishes: list[dict] = []

    async def fake_log_start():
        run_id = uuid.uuid4()
        started_ids.append(run_id)
        return run_id

    async def fake_async_finish(flow_run_id, *, status, summary, error_message):
        async_finishes.append({
            "flow_run_id": flow_run_id,
            "status": status,
            "summary": summary,
            "error_message": error_message,
        })

    def fake_sync_finish(flow_run_id, *, status, error_message):
        sync_finishes.append({
            "flow_run_id": flow_run_id,
            "status": status,
            "error_message": error_message,
        })

    monkeypatch.setattr(m, "_log_flow_start", fake_log_start)
    monkeypatch.setattr(m, "_log_flow_finish", fake_async_finish)
    monkeypatch.setattr(m, "_log_flow_finish_sync", fake_sync_finish)

    class _NoopLogger:
        def info(self, *_a, **_k): pass
        def warning(self, *_a, **_k): pass
        def error(self, *_a, **_k): pass

    monkeypatch.setattr(m, "get_run_logger", lambda: _NoopLogger())

    return m, started_ids, async_finishes, sync_finishes


# ---------------------------------------------------------------------------
# Case 1: KeyboardInterrupt routes through sync helper as 'cancelled'.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_keyboardinterrupt_routes_to_sync_cancelled(flow_module, monkeypatch):
    m, started_ids, async_finishes, sync_finishes = flow_module

    async def fake_run_float_update(progress_callback=None):
        raise KeyboardInterrupt()

    monkeypatch.setattr(m, "_run_float_update", fake_run_float_update)

    with pytest.raises(KeyboardInterrupt):
        await m.float_update_flow.fn()

    assert async_finishes == [], "async helper must not be called on cancellation"
    assert len(sync_finishes) == 1
    fin = sync_finishes[0]
    assert fin["status"] == "cancelled"
    assert "KeyboardInterrupt" in (fin["error_message"] or "")
    assert fin["flow_run_id"] == started_ids[0]


# ---------------------------------------------------------------------------
# Case 2: asyncio.CancelledError routes through sync helper.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancellederror_routes_to_sync_cancelled(flow_module, monkeypatch):
    m, started_ids, async_finishes, sync_finishes = flow_module

    async def fake_run_float_update(progress_callback=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(m, "_run_float_update", fake_run_float_update)

    with pytest.raises(asyncio.CancelledError):
        await m.float_update_flow.fn()

    assert async_finishes == []
    assert len(sync_finishes) == 1
    assert sync_finishes[0]["status"] == "cancelled"
    assert "CancelledError" in (sync_finishes[0]["error_message"] or "")


# ---------------------------------------------------------------------------
# Case 3: TerminationSignal (if importable) routes through sync helper.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_termination_signal_routes_to_sync_cancelled(flow_module, monkeypatch):
    """Skipped gracefully if Prefect doesn't expose TerminationSignal in
    this version. We don't fail the test suite on a name that may not
    exist — the import in the module already handles that."""
    try:
        from prefect.exceptions import TerminationSignal  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("prefect.exceptions.TerminationSignal not available")

    m, started_ids, async_finishes, sync_finishes = flow_module

    # TerminationSignal requires a signal kwarg in this Prefect version.
    # We don't care which signal — just that the type is in the catch
    # tuple. SIGTERM (15) is the canonical case.
    import signal

    async def fake_run_float_update(progress_callback=None):
        raise TerminationSignal(signal=signal.SIGTERM)

    monkeypatch.setattr(m, "_run_float_update", fake_run_float_update)

    with pytest.raises(TerminationSignal):
        await m.float_update_flow.fn()

    assert async_finishes == []
    assert len(sync_finishes) == 1
    assert sync_finishes[0]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Case 4: Generic Exception still routes through ASYNC helper as 'failed'.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_exception_routes_to_async_failed(flow_module, monkeypatch):
    m, _, async_finishes, sync_finishes = flow_module

    async def fake_run_float_update(progress_callback=None):
        raise RuntimeError("polygon down")

    monkeypatch.setattr(m, "_run_float_update", fake_run_float_update)

    with pytest.raises(RuntimeError, match="polygon down"):
        await m.float_update_flow.fn()

    assert sync_finishes == [], "sync helper is cancellation-only"
    assert len(async_finishes) == 1
    fin = async_finishes[0]
    assert fin["status"] == "failed"
    assert "polygon down" in (fin["error_message"] or "")


# ---------------------------------------------------------------------------
# Case 5: Happy path still writes 'completed' via async helper. Regression
# check that the new try/except didn't change happy-path behavior.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_happy_path_routes_to_async_completed(flow_module, monkeypatch):
    m, _, async_finishes, sync_finishes = flow_module

    from ingestion.market_data.float_updater import FloatUpdateReport
    sample_report = FloatUpdateReport(
        total=42, updated=40, deactivated_oversized=1,
        deactivated_not_found=1, errors=0,
    )

    async def fake_run_float_update(progress_callback=None):
        return sample_report

    monkeypatch.setattr(m, "_run_float_update", fake_run_float_update)

    summary = await m.float_update_flow.fn()

    assert sync_finishes == []
    assert len(async_finishes) == 1
    assert async_finishes[0]["status"] == "completed"
    assert async_finishes[0]["summary"] == sample_report.as_dict()
    assert summary == sample_report.as_dict()


# ---------------------------------------------------------------------------
# The sync helper itself: psycopg2 stubbed, assert SQL + lifecycle shape.
# No asyncio, no flow context — direct unit test.
# ---------------------------------------------------------------------------
def test_sync_helper_executes_update_with_correct_lifecycle(monkeypatch):
    """One connect, one UPDATE, one commit, one close. The point of the
    helper's minimalism is that it MUST run to completion under
    cancellation; this pins the shape."""
    # Stub psycopg2 module — sandbox / CI may or may not have it loaded.
    fake_psycopg2 = types.ModuleType("psycopg2")
    connect_calls: list[str] = []
    executes: list[tuple] = []
    commits: list[bool] = []
    closes: list[bool] = []

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, sql, params):
            executes.append((sql, params))

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            commits.append(True)

        def close(self):
            closes.append(True)

    def fake_connect(url):
        connect_calls.append(url)
        return _FakeConn()

    fake_psycopg2.connect = fake_connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")

    from flows.float_update_flow import _log_flow_finish_sync

    run_id = uuid.uuid4()
    _log_flow_finish_sync(
        run_id, status="cancelled",
        error_message="KeyboardInterrupt: cancelled mid-run",
    )

    assert connect_calls == ["postgresql://test/db"]
    assert len(executes) == 1
    sql, params = executes[0]
    assert "update flow_run_log" in sql.lower()
    assert "completed_at=now()" in sql.lower()
    assert params == (
        "cancelled",
        "KeyboardInterrupt: cancelled mid-run",
        str(run_id),
    )
    assert commits == [True]
    assert closes == [True]


def test_sync_helper_closes_connection_even_if_execute_raises(monkeypatch):
    """Conn must always be closed — that's the try/finally contract."""
    fake_psycopg2 = types.ModuleType("psycopg2")
    closes: list[bool] = []

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, sql, params):
            raise RuntimeError("DB error")

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

        def close(self):
            closes.append(True)

    fake_psycopg2.connect = lambda url: _FakeConn()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")

    from flows.float_update_flow import _log_flow_finish_sync

    with pytest.raises(RuntimeError):
        _log_flow_finish_sync(uuid.uuid4(), status="cancelled", error_message="x")

    assert closes == [True], "conn.close() must run even on execute failure"


# ---------------------------------------------------------------------------
# Cancellation-tuple wiring: KeyboardInterrupt and CancelledError MUST be in
# the catch tuple regardless of Prefect version.
# ---------------------------------------------------------------------------
def test_cancellation_excs_includes_kbinterrupt_and_cancelled():
    from flows.float_update_flow import _CANCELLATION_EXCS

    assert KeyboardInterrupt in _CANCELLATION_EXCS
    assert asyncio.CancelledError in _CANCELLATION_EXCS
