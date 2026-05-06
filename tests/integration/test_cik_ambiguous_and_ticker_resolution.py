"""Regression tests for the production outage fixed in
hotfix/cik-ambiguous-and-ticker-resolution.

Two bugs in `_build_signal_payload`'s CIK-fallback ticker resolution:

  1. The watcher dispatched the celery task without a ticker key in the
     payload. Combined with neither update_values nor findings carrying
     ticker, this forced every filing through the CIK fallback path —
     even though the watcher had already resolved the ticker correctly
     and stored it on the sec_filings row.

  2. The CIK fallback used .scalar_one_or_none(), which raised
     MultipleResultsFound on CIKs with >1 ticker (Freddie Mac CIK has
     23 tickers across common + preferred series; AGNC has 3; etc.).
     The crash propagated up and broke the entire celery task,
     stranding 24 filings in unprocessed state.

This test file pins both fixes:
  - The watcher's celery dispatch payload includes "ticker"
  - The CIK fallback handles 0 / 1 / >1 row results explicitly
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fix 1: watcher dispatch payload carries ticker.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_persist_and_queue_dispatch_payload_includes_ticker(monkeypatch):
    """The celery task dispatched by the watcher MUST carry the ticker the
    watcher resolved from cik_to_ticker. Without it, downstream
    _build_signal_payload falls through to a CIK lookup that crashes on
    multi-ticker CIKs."""
    from ingestion.edgar import rss_watcher as m

    delay_calls: list = []
    monkeypatch.setattr(m.process_filing, "delay", lambda p: delay_calls.append(p))

    monkeypatch.setattr(
        m, "_existing_accessions", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        m, "_ticker_for_ciks", AsyncMock(return_value={"0000320193": "AAPL"})
    )

    insert_session = MagicMock()
    insert_session.execute = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield insert_session

    monkeypatch.setattr(m, "get_session", fake_get_session)

    outer_session = MagicMock()
    outer_session.execute = AsyncMock()
    outer_session.flush = AsyncMock()

    filings = [
        {
            "accession_number": "0000320193-26-000001",
            "form_type": "8-K",
            "cik": "0000320193",
            "filed_at": datetime(2026, 5, 5, tzinfo=timezone.utc),
            "company_name": "APPLE INC",
            "link": "https://example/apple",
        },
    ]

    queued = await m.persist_and_queue(outer_session, filings)
    assert queued == 1
    assert len(delay_calls) == 1
    payload = delay_calls[0]
    assert payload["ticker"] == "AAPL", (
        "celery task payload must carry the resolved ticker; got "
        f"{payload!r}"
    )


@pytest.mark.asyncio
async def test_persist_and_queue_dispatches_none_ticker_when_cik_not_in_universe(monkeypatch):
    """When the watcher couldn't resolve a ticker for a CIK, ticker MUST
    still appear in the payload — explicitly as None. Downstream code
    distinguishes 'key absent' (a regression) from 'key present but None'
    (legitimate unresolved-ticker filing). Always emit the key."""
    from ingestion.edgar import rss_watcher as m

    delay_calls: list = []
    monkeypatch.setattr(m.process_filing, "delay", lambda p: delay_calls.append(p))

    monkeypatch.setattr(
        m, "_existing_accessions", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        m, "_ticker_for_ciks", AsyncMock(return_value={})  # no map hit
    )

    insert_session = MagicMock()
    insert_session.execute = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield insert_session

    monkeypatch.setattr(m, "get_session", fake_get_session)

    outer_session = MagicMock()
    outer_session.execute = AsyncMock()
    outer_session.flush = AsyncMock()

    filings = [
        {
            "accession_number": "0000999999-26-000001",
            "form_type": "S-3",
            "cik": "0000999999",
            "filed_at": datetime(2026, 5, 5, tzinfo=timezone.utc),
            "company_name": "UNKNOWN CORP",
            "link": "https://example/unk",
        },
    ]

    await m.persist_and_queue(outer_session, filings)
    assert len(delay_calls) == 1
    payload = delay_calls[0]
    assert "ticker" in payload, "ticker key must be present even when None"
    assert payload["ticker"] is None


# ---------------------------------------------------------------------------
# Fix 2: CIK fallback handles 0 / 1 / >1 ticker rows safely.
# ---------------------------------------------------------------------------
def _build_session_for_cik_lookup(rows_for_cik):
    """Mock a session whose first execute() returns the given rows for the
    CIK lookup. Subsequent executes return empty (handles the
    promoter/ticker_row queries downstream of the CIK fallback)."""
    call_count = {"n": 0}

    def make_result(rows):
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=rows)
        result.scalars = MagicMock(return_value=scalars)
        # scalar_one_or_none-style for downstream queries:
        result.scalar_one_or_none = MagicMock(return_value=None)
        result.scalar_one = MagicMock(return_value=0)
        return result

    async def fake_execute(stmt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return make_result(rows_for_cik)
        return make_result([])

    session = MagicMock()
    session.execute = AsyncMock(side_effect=fake_execute)
    return session


@pytest.mark.asyncio
async def test_cik_fallback_zero_rows_returns_none_ticker(monkeypatch):
    """0 ticker rows for the CIK → ticker stays None, no crash."""
    from ingestion.edgar import rss_watcher as m

    session = _build_session_for_cik_lookup([])

    payload = {"cik": "0000111111", "form_type": "S-3"}
    out = await m._build_signal_payload(
        session, "ACC-1", payload, update_values={}, findings={},
    )
    # 0-row case behaves identically to pre-fix when ticker is unresolvable.
    # The function returns its snapshot with ticker=None.
    assert out is not None
    assert out["ticker_metadata"]["ticker"] is None


@pytest.mark.asyncio
async def test_cik_fallback_single_row_uses_it(monkeypatch):
    """1 ticker row for the CIK → use it. Backward-compatible behavior."""
    from ingestion.edgar import rss_watcher as m

    fake_ticker = MagicMock()
    fake_ticker.ticker = "AAPL"
    session = _build_session_for_cik_lookup([fake_ticker])

    payload = {"cik": "0000320193", "form_type": "8-K"}
    out = await m._build_signal_payload(
        session, "ACC-2", payload, update_values={}, findings={},
    )
    assert out is not None
    assert out["ticker_metadata"]["ticker"] == "AAPL"


@pytest.mark.asyncio
async def test_cik_fallback_multiple_rows_skips_with_warning(monkeypatch, caplog):
    """>1 ticker rows for the CIK → ticker stays None and a warning is
    logged. The pre-fix code raised MultipleResultsFound and crashed the
    celery task. NEVER silently pick one (that would mis-attribute the
    signal to the wrong security class)."""
    import logging

    from ingestion.edgar import rss_watcher as m

    # Capture loguru output by intercepting the logger's warning method.
    warnings_seen: list = []
    original_warning = m.logger.warning

    def capture_warning(msg, *args, **kwargs):
        warnings_seen.append((msg, kwargs))
        return original_warning(msg, *args, **kwargs)

    monkeypatch.setattr(m.logger, "warning", capture_warning)

    fmcc = MagicMock(); fmcc.ticker = "FMCC"
    fmckp = MagicMock(); fmckp.ticker = "FMCKP"
    fmccm = MagicMock(); fmccm.ticker = "FMCCM"

    session = _build_session_for_cik_lookup([fmcc, fmckp, fmccm])

    payload = {"cik": "0001026214", "form_type": "S-3"}
    out = await m._build_signal_payload(
        session, "ACC-3", payload, update_values={}, findings={},
    )

    # No crash, ticker is None
    assert out is not None
    assert out["ticker_metadata"]["ticker"] is None

    # Warning was emitted with the cik and the list of tickers
    assert any("cik_ambiguous_skip" in str(msg) for msg, _ in warnings_seen), (
        f"expected cik_ambiguous_skip warning; got {warnings_seen}"
    )


@pytest.mark.asyncio
async def test_payload_ticker_short_circuits_cik_fallback(monkeypatch):
    """When payload carries ticker (the post-fix happy path), the CIK
    fallback is not even executed. This test exercises the full code
    path that the production watcher uses post-fix."""
    from ingestion.edgar import rss_watcher as m

    # If the CIK fallback runs, this raises — which is exactly the
    # invariant we're pinning ('don't run the fallback when payload has it')
    cik_lookup_calls: list = []

    def trap_cik_lookup(stmt):
        # Crude detection: the CIK lookup is the only execute that filters
        # by Ticker.cik in this code path.
        if "WHERE tickers.cik" in str(stmt) or "tickers.cik =" in str(stmt):
            cik_lookup_calls.append(stmt)
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[])
        result.scalars = MagicMock(return_value=scalars)
        result.scalar_one_or_none = MagicMock(return_value=None)
        result.scalar_one = MagicMock(return_value=0)
        return result

    session = MagicMock()

    async def fake_execute(stmt):
        return trap_cik_lookup(stmt)

    session.execute = AsyncMock(side_effect=fake_execute)

    payload = {"ticker": "AAPL", "cik": "0000320193", "form_type": "8-K"}
    out = await m._build_signal_payload(
        session, "ACC-OK", payload, update_values={}, findings={},
    )
    assert out is not None
    assert out["ticker_metadata"]["ticker"] == "AAPL"
    assert cik_lookup_calls == [], (
        "CIK fallback ran even though payload carried ticker — that's the "
        "regression this hotfix exists to prevent"
    )
