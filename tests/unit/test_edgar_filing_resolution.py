from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from ingestion.edgar.rss_watcher import (
    _ticker_for_ciks,
    persist_and_queue,
)


def _mock_session_with_scalars(rows: list) -> MagicMock:
    """Mock AsyncSession.execute() that returns a result whose .all()/.scalars()
    yield the given rows. Sequential calls return rows in order."""
    session = MagicMock()
    iterator = iter(rows)

    async def execute(_stmt):
        result = MagicMock()
        result_value = next(iterator, [])
        result.all = MagicMock(return_value=result_value)
        scalars_obj = MagicMock()
        scalars_obj.all = MagicMock(return_value=[r[0] if isinstance(r, tuple) else r for r in result_value])
        result.scalars = MagicMock(return_value=scalars_obj)
        return result

    session.execute = AsyncMock(side_effect=execute)
    session.flush = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_ticker_for_ciks_returns_mapping_from_db():
    """Resolver returns a dict {cik: ticker} for matching rows, drops misses."""
    # First execute() returns the cik/ticker pairs the DB has
    rows = [[("0000320193", "AAPL"), ("0001134982", "AAPI")]]
    session = _mock_session_with_scalars(rows)

    out = await _ticker_for_ciks(session, ["0000320193", "0001134982", "0000999999"])
    assert out == {"0000320193": "AAPL", "0001134982": "AAPI"}


@pytest.mark.asyncio
async def test_ticker_for_ciks_empty_input_short_circuits():
    """No DB call when input is empty."""
    session = _mock_session_with_scalars([])
    out = await _ticker_for_ciks(session, [])
    assert out == {}
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_and_queue_resolves_ticker_when_known(monkeypatch):
    """A filing whose CIK is in the tickers table is inserted with that ticker."""
    from contextlib import asynccontextmanager

    from ingestion.edgar import rss_watcher as m

    delay_calls: list = []
    monkeypatch.setattr(m.process_filing, "delay", lambda payload: delay_calls.append(payload))

    insert_calls: list = []
    async def fake_execute(stmt):
        # Capture the values bound to the insert; it's the second/etc call.
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        result.all = MagicMock(return_value=[])
        try:
            insert_calls.append(stmt.compile().params)
        except Exception:
            insert_calls.append(None)
        return result

    # Outer session — used for read-only universe/dedupe lookups (mocked away
    # below). The actual insert now goes through a per-filing get_session().
    session = MagicMock()
    session.execute = AsyncMock(side_effect=fake_execute)
    session.flush = AsyncMock()

    insert_session = MagicMock()
    insert_session.execute = AsyncMock(side_effect=fake_execute)

    @asynccontextmanager
    async def fake_get_session():
        yield insert_session

    monkeypatch.setattr(m, "get_session", fake_get_session)

    monkeypatch.setattr(
        m, "_existing_accessions", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        m, "_ticker_for_ciks", AsyncMock(return_value={"0000320193": "AAPL"})
    )

    filings = [
        {
            "accession_number": "0000320193-26-000001",
            "form_type": "8-K",
            "cik": "0000320193",
            "filed_at": datetime(2026, 4, 29, tzinfo=timezone.utc),
            "company_name": "APPLE INC",
            "link": "https://example",
        },
        {
            "accession_number": "0000999999-26-000002",
            "form_type": "S-3",
            "cik": "0000999999",  # not in tickers — must store NULL
            "filed_at": datetime(2026, 4, 29, tzinfo=timezone.utc),
            "company_name": "UNKNOWN CORP",
            "link": "https://example",
        },
    ]

    queued = await persist_and_queue(session, filings)
    assert queued == 2

    # Both filings enqueued via Celery (with their CIKs and form types intact)
    assert {c["accession_number"] for c in delay_calls} == {
        "0000320193-26-000001",
        "0000999999-26-000002",
    }

    # Both inserts went through; one should have ticker AAPL, the other NULL
    bound_tickers = [c.get("ticker") for c in insert_calls if c]
    assert "AAPL" in bound_tickers
    assert None in bound_tickers
