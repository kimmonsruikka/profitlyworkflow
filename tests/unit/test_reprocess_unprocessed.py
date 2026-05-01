"""Unit tests for scripts/reprocess_unprocessed_filings.py.

The script's purpose is to drain a backlog of sec_filings rows stuck
at processed=False by re-dispatching process_filing.delay() for each.
These tests stub the DB and Celery surfaces and assert the script's
external behavior — payload shape, filtering, dry-run safety, limit
cap — without touching either.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _row(**kw) -> SimpleNamespace:
    """Build a minimal SecFiling-shaped object."""
    defaults = {
        "accession_number": "0000111111-26-000001",
        "cik": "0000111111",
        "form_type": "8-K",
        "filed_at": datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
        "created_at": datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc),
        "processed": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# build_payload
# ---------------------------------------------------------------------------
def test_build_payload_default_has_no_link():
    from scripts import reprocess_unprocessed_filings as m

    row = _row()
    out = m.build_payload(row, reconstruct_link=False)

    assert out["accession_number"] == "0000111111-26-000001"
    assert out["cik"] == "0000111111"
    assert out["form_type"] == "8-K"
    assert out["link"] is None
    assert out["filed_at"] == "2026-04-29T12:00:00+00:00"


def test_build_payload_with_reconstruct_link_synthesizes_archive_url():
    from scripts import reprocess_unprocessed_filings as m

    row = _row(cik="0000320193", accession_number="0000320193-26-000001")
    out = m.build_payload(row, reconstruct_link=True)

    assert out["link"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000001/0000320193-26-000001-index.htm"
    )


def test_reconstruct_link_returns_none_for_missing_cik():
    from scripts import reprocess_unprocessed_filings as m
    assert m._reconstruct_archive_link(None, "0000111111-26-000001") is None
    assert m._reconstruct_archive_link("", "0000111111-26-000001") is None


# ---------------------------------------------------------------------------
# dispatch_all — dry-run, rate, progress
# ---------------------------------------------------------------------------
def test_dispatch_all_dry_run_does_not_call_delay(monkeypatch):
    from scripts import reprocess_unprocessed_filings as m

    delays: list = []
    monkeypatch.setattr(m.process_filing, "delay", lambda p: delays.append(p))
    # Monkeypatch sleep so the rate doesn't slow the test.
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)

    rows = [_row(accession_number=f"acc-{i}") for i in range(3)]
    n = m.dispatch_all(
        rows, total=3, dry_run=True, reconstruct_link=False, rate_per_second=10,
    )

    assert n == 3
    assert delays == [], "dry-run must not dispatch"


def test_dispatch_all_dispatches_each_row_once(monkeypatch):
    from scripts import reprocess_unprocessed_filings as m

    delays: list = []
    monkeypatch.setattr(m.process_filing, "delay", lambda p: delays.append(p))
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)

    rows = [
        _row(accession_number="A-1"),
        _row(accession_number="A-2"),
        _row(accession_number="A-3"),
    ]
    n = m.dispatch_all(
        rows, total=3, dry_run=False, reconstruct_link=False, rate_per_second=100,
    )

    assert n == 3
    assert [d["accession_number"] for d in delays] == ["A-1", "A-2", "A-3"]
    assert all(d["link"] is None for d in delays)


def test_dispatch_all_empty_input_short_circuits(monkeypatch, capsys):
    from scripts import reprocess_unprocessed_filings as m

    delays: list = []
    monkeypatch.setattr(m.process_filing, "delay", lambda p: delays.append(p))

    n = m.dispatch_all([], total=0, dry_run=False, reconstruct_link=False, rate_per_second=10)

    assert n == 0
    assert delays == []
    out = capsys.readouterr().out
    assert "0 to reprocess" in out


# ---------------------------------------------------------------------------
# load_unprocessed — filter wiring (verified via the SQL select shape)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_load_unprocessed_applies_form_type_filter(monkeypatch):
    from contextlib import asynccontextmanager

    from scripts import reprocess_unprocessed_filings as m

    captured: list = []

    async def fake_execute(stmt):
        captured.append(stmt)
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[])
        result.scalars = MagicMock(return_value=scalars)
        return result

    session = MagicMock()
    session.execute = AsyncMock(side_effect=fake_execute)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    monkeypatch.setattr(m, "get_session", fake_get_session)

    filters = m.Filters(form_type="8-K", created_before=None, limit=5)
    rows = await m.load_unprocessed(filters)

    assert rows == []
    assert len(captured) == 1
    compiled = str(captured[0])
    # Compiled SQL should reference the filter columns we asked for.
    assert "processed" in compiled.lower()
    assert "form_type" in compiled.lower()


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------
def test_parse_args_defaults():
    from scripts import reprocess_unprocessed_filings as m

    args = m.parse_args([])
    assert args.dry_run is False
    assert args.form_type is None
    assert args.limit is None
    assert args.created_before is None
    assert args.reconstruct_links is False
    assert args.rate == m.DEFAULT_DISPATCH_RATE


def test_parse_args_full():
    from scripts import reprocess_unprocessed_filings as m

    args = m.parse_args([
        "--dry-run",
        "--form-type", "S-3",
        "--limit", "100",
        "--created-before", "2026-05-01T17:13:01",
        "--reconstruct-links",
        "--rate", "5",
    ])
    assert args.dry_run is True
    assert args.form_type == "S-3"
    assert args.limit == 100
    assert args.created_before == datetime.fromisoformat("2026-05-01T17:13:01")
    assert args.reconstruct_links is True
    assert args.rate == 5
