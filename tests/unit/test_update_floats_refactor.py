"""Tests for update_one_ticker — the per-row primitive that the
universe sweep and any future targeted refresh both call.

Covers:
  - happy path returns 'updated' with old/new floats and sets float_updated_at
  - oversized float deactivates and tags float_updated_at
  - PolygonNotFoundError deactivates and tags float_updated_at
  - network error returns 'error' WITHOUT touching float_updated_at
    (so the row stays eligible for the next sweep)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from ingestion.market_data import float_updater as fu
from ingestion.market_data.polygon_client import PolygonNotFoundError


def _make_ticker(symbol: str, *, float_shares=None, active: bool = True) -> MagicMock:
    obj = MagicMock(spec=[
        "ticker", "active", "float_shares", "shares_outstanding",
        "float_updated_at",
    ])
    obj.ticker = symbol
    obj.active = active
    obj.float_shares = float_shares
    obj.shares_outstanding = None
    obj.float_updated_at = None
    return obj


@pytest.mark.asyncio
async def test_update_one_ticker_happy_path_under_cap():
    row = _make_ticker("ABCD", float_shares=1_000_000)
    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(return_value={
        "float_shares": 4_100_000,
        "shares_outstanding": 4_100_000,
    })
    session = MagicMock()

    result = await fu.update_one_ticker(session, polygon, row)

    assert result.status == "updated"
    assert result.ticker_symbol == "ABCD"
    assert result.old_float == 1_000_000
    assert result.new_float == 4_100_000
    assert row.float_shares == 4_100_000
    assert row.shares_outstanding == 4_100_000
    assert row.active is True
    assert isinstance(row.float_updated_at, datetime)
    assert row.float_updated_at.tzinfo is timezone.utc


@pytest.mark.asyncio
async def test_update_one_ticker_oversized_deactivates_and_tags_timestamp():
    from config import constants

    row = _make_ticker("BIGCO", float_shares=1_000_000)
    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(return_value={
        "float_shares": constants.FLOAT_MAX + 1,
        "shares_outstanding": constants.FLOAT_MAX + 1,
    })
    session = MagicMock()

    result = await fu.update_one_ticker(session, polygon, row)

    assert result.status == "deactivated_oversized"
    assert result.new_float == constants.FLOAT_MAX + 1
    assert row.active is False
    assert row.float_updated_at is not None  # tagged so we don't re-fetch


@pytest.mark.asyncio
async def test_update_one_ticker_polygon_not_found_deactivates_and_tags():
    row = _make_ticker("GHOST", float_shares=2_000_000)
    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(side_effect=PolygonNotFoundError("404"))
    session = MagicMock()

    result = await fu.update_one_ticker(session, polygon, row)

    assert result.status == "deactivated_not_on_polygon"
    assert row.active is False
    assert row.float_updated_at is not None
    # old float preserved on the row even though shares lookup failed
    assert row.float_shares == 2_000_000


@pytest.mark.asyncio
async def test_update_one_ticker_network_error_leaves_row_for_retry():
    """Transient errors must NOT bump float_updated_at — otherwise the
    next sweep skips the row and the staleness tracking lies."""
    row = _make_ticker("FLAKY", float_shares=3_000_000)
    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(side_effect=ConnectionError("timeout"))
    session = MagicMock()

    result = await fu.update_one_ticker(session, polygon, row)

    assert result.status == "error"
    assert "timeout" in (result.error or "")
    assert row.active is True  # still in the active universe
    assert row.float_shares == 3_000_000  # unchanged
    assert row.float_updated_at is None  # untouched — eligible for retry


@pytest.mark.asyncio
async def test_universe_sweep_orders_oldest_first(monkeypatch):
    """update_floats_for_universe must walk active tickers in
    float_updated_at ASC NULLS FIRST order — the SELECT must include
    that ordering so brand-new and stalest rows are refreshed first."""
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
    session.flush = AsyncMock()
    session.commit = AsyncMock()

    polygon = MagicMock()
    await fu.update_floats_for_universe(session, polygon)

    assert len(captured) == 1
    compiled = str(captured[0])
    # Order clause should include float_updated_at and NULLS FIRST.
    lowered = compiled.lower()
    assert "float_updated_at" in lowered
    assert "nulls first" in lowered


@pytest.mark.asyncio
async def test_universe_sweep_commits_every_n_rows(monkeypatch):
    """Per-batch COMMIT (not flush) bounds rollback blast radius and
    makes writes visible to other connections during the ~17h sweep.
    A flush keeps writes inside the open transaction — operator psql
    shells and dashboards can't see them, and a crash mid-flow rolls
    everything back. PR #27 originally used flush() and the acceptance
    query returned 0 even after 30 successful per-row updates because
    the Ctrl+C rolled them all back. This test pins commit() so we
    don't regress."""
    from config import constants

    rows = [_make_ticker(f"T{i:04d}", float_shares=1_000_000) for i in range(70)]
    monkeypatch.setattr(fu, "_load_active_tickers", AsyncMock(return_value=rows))

    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(return_value={
        "float_shares": 2_000_000,
        "shares_outstanding": 2_000_000,
    })

    commit_calls: list[int] = []
    counter = {"i": 0}

    async def fake_commit():
        commit_calls.append(counter["i"])

    session = MagicMock()
    session.commit = AsyncMock(side_effect=fake_commit)
    # flush() may still be called by SQLAlchemy internals; we don't care.
    session.flush = AsyncMock()

    real_update_one_ticker = fu.update_one_ticker

    async def counting_update_one_ticker(s, p, r):
        counter["i"] += 1
        return await real_update_one_ticker(s, p, r)

    monkeypatch.setattr(fu, "update_one_ticker", counting_update_one_ticker)

    await fu.update_floats_for_universe(session, polygon)

    # 70 rows with COMMIT_INTERVAL=25 → commits at rows 25, 50, plus the
    # final commit at end (after row 70). The end commit captures row 70.
    commit_interval = constants.FLOAT_UPDATE_COMMIT_INTERVAL
    assert commit_interval == 25  # pin the contract
    # At least 3 commits: two batch boundaries + end. Could be more if
    # the count lines up with commit_interval exactly, e.g. 75 → 25/50/75/end.
    assert len(commit_calls) >= 3
    # The first batch commit should fire on or near row 25.
    assert commit_calls[0] == commit_interval


@pytest.mark.asyncio
async def test_report_as_dict_shape():
    """flow_run_log.summary writes this dict — pin the keys."""
    report = fu.FloatUpdateReport(
        total=10, updated=8, deactivated_oversized=1,
        deactivated_not_found=0, errors=1,
    )
    assert report.as_dict() == {
        "total": 10,
        "updated": 8,
        "deactivated_oversized": 1,
        "deactivated_not_found": 0,
        "errors": 1,
    }
