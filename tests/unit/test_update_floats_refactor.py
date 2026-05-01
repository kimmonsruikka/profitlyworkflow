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

    polygon = MagicMock()
    await fu.update_floats_for_universe(session, polygon)

    assert len(captured) == 1
    compiled = str(captured[0])
    # Order clause should include float_updated_at and NULLS FIRST.
    lowered = compiled.lower()
    assert "float_updated_at" in lowered
    assert "nulls first" in lowered


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
