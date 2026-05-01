from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import constants


def _stub_polygon_module() -> MagicMock:
    """Insert a fake polygon-api-client SDK so PolygonClient imports cleanly."""
    if "polygon" in sys.modules:
        return sys.modules["polygon"].RESTClient  # type: ignore[attr-defined]

    polygon_mod = types.ModuleType("polygon")
    rest_client = MagicMock(name="RESTClient")
    polygon_mod.RESTClient = rest_client
    sys.modules["polygon"] = polygon_mod
    return rest_client


@pytest.fixture
def polygon_stub() -> MagicMock:
    return _stub_polygon_module()


# ---------------------------------------------------------------------------
# PolygonClient — throttling + ticker details
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_polygon_throttler_sleeps_between_requests(polygon_stub, monkeypatch):
    """Two back-to-back calls should incur one inter-call sleep."""
    from ingestion.market_data import polygon_client as pc_mod

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(pc_mod.asyncio, "sleep", fake_sleep)

    client = pc_mod.PolygonClient(api_key="x", requests_per_minute=5)

    # Drive monotonic() so the throttler thinks the second call is back-to-back.
    # Real monotonic still works inside __init__; we override only afterward.
    counter = {"t": 1000.0}

    def stepped_monotonic() -> float:
        counter["t"] += 0.05
        return counter["t"]

    monkeypatch.setattr(pc_mod.time, "monotonic", stepped_monotonic)

    client._client.get_ticker_details = MagicMock(
        return_value=MagicMock(
            ticker="AAPL", name="Apple",
            primary_exchange="XNAS", sic_description="Tech",
            share_class_shares_outstanding=15_000_000_000,
            weighted_shares_outstanding=15_000_000_000,
            market_cap=2.7e12,
        )
    )
    await client.get_ticker_details("AAPL")
    await client.get_ticker_details("AAPL")

    # First call: throttler sleeps 0 (last_request_at is 0). Second call:
    # interval is 60/5 * 1.05 = 12.6s; with a 0.1s gap, sleep ~= 12.5s.
    assert any(s > 10 for s in sleep_calls), f"expected throttle sleep, got {sleep_calls}"


@pytest.mark.asyncio
async def test_polygon_get_ticker_details_returns_dict(polygon_stub, monkeypatch):
    from ingestion.market_data.polygon_client import PolygonClient

    monkeypatch.setattr("ingestion.market_data.polygon_client.asyncio.sleep", AsyncMock())

    client = PolygonClient(api_key="x")
    client._client.get_ticker_details = MagicMock(
        return_value=MagicMock(
            ticker="ABCD", name="Acme Corp", primary_exchange="OTC",
            sic_description="Mining", share_class_shares_outstanding=4_100_000,
            weighted_shares_outstanding=4_100_000, market_cap=20_000_000,
        )
    )
    out = await client.get_ticker_details("ABCD")
    assert out["ticker"] == "ABCD"
    assert out["float_shares"] == 4_100_000
    assert out["shares_outstanding"] == 4_100_000
    assert out["exchange"] == "OTC"


@pytest.mark.asyncio
async def test_polygon_not_found_raises_typed_error(polygon_stub, monkeypatch):
    from ingestion.market_data.polygon_client import (
        PolygonClient,
        PolygonNotFoundError,
    )

    monkeypatch.setattr("ingestion.market_data.polygon_client.asyncio.sleep", AsyncMock())

    client = PolygonClient(api_key="x")

    def boom(_):
        raise Exception("status 404 — Ticker not found")

    client._client.get_ticker_details = boom
    with pytest.raises(PolygonNotFoundError):
        await client.get_ticker_details("ZZZZZ")


# ---------------------------------------------------------------------------
# float_updater — universe filtering + deactivation
# ---------------------------------------------------------------------------
def _make_ticker(ticker: str, *, active: bool = True) -> MagicMock:
    obj = MagicMock(spec=[
        "ticker", "active", "float_shares", "shares_outstanding",
        "float_updated_at",
    ])
    obj.ticker = ticker
    obj.active = active
    obj.float_shares = None
    obj.shares_outstanding = None
    obj.float_updated_at = None
    return obj


@pytest.mark.asyncio
async def test_float_under_cap_keeps_active_and_updates(polygon_stub, monkeypatch):
    from ingestion.market_data import float_updater as fu

    rows = [_make_ticker("ABCD")]
    monkeypatch.setattr(fu, "_load_active_tickers", AsyncMock(return_value=rows))

    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(return_value={
        "float_shares": 4_100_000,
        "shares_outstanding": 4_100_000,
    })
    session = MagicMock()
    session.flush = AsyncMock()

    report = await fu.update_floats_for_universe(session, polygon)
    assert rows[0].active is True
    assert rows[0].float_shares == 4_100_000
    assert rows[0].shares_outstanding == 4_100_000
    assert report.updated == 1
    assert report.deactivated_oversized == 0


@pytest.mark.asyncio
async def test_float_over_10m_deactivates(polygon_stub, monkeypatch):
    from ingestion.market_data import float_updater as fu

    rows = [_make_ticker("BIGCO")]
    monkeypatch.setattr(fu, "_load_active_tickers", AsyncMock(return_value=rows))

    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(return_value={
        "float_shares": constants.FLOAT_MAX + 1,
        "shares_outstanding": constants.FLOAT_MAX + 1,
    })
    session = MagicMock()
    session.flush = AsyncMock()

    report = await fu.update_floats_for_universe(session, polygon)
    assert rows[0].active is False
    assert rows[0].float_shares == constants.FLOAT_MAX + 1
    assert report.deactivated_oversized == 1
    assert report.updated == 0
    assert "BIGCO" in report.oversized_tickers


@pytest.mark.asyncio
async def test_polygon_not_found_deactivates_ticker(polygon_stub, monkeypatch):
    from ingestion.market_data import float_updater as fu
    from ingestion.market_data.polygon_client import PolygonNotFoundError

    rows = [_make_ticker("GHOST")]
    monkeypatch.setattr(fu, "_load_active_tickers", AsyncMock(return_value=rows))

    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(side_effect=PolygonNotFoundError("404"))
    session = MagicMock()
    session.flush = AsyncMock()

    report = await fu.update_floats_for_universe(session, polygon)
    assert rows[0].active is False
    assert report.deactivated_not_found == 1
    assert "GHOST" in report.not_found_tickers


@pytest.mark.asyncio
async def test_network_error_keeps_active_for_retry(polygon_stub, monkeypatch):
    """Transient errors leave the row active so the next run retries."""
    from ingestion.market_data import float_updater as fu

    rows = [_make_ticker("FLAKY")]
    monkeypatch.setattr(fu, "_load_active_tickers", AsyncMock(return_value=rows))

    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(side_effect=ConnectionError("timeout"))
    session = MagicMock()
    session.flush = AsyncMock()

    report = await fu.update_floats_for_universe(session, polygon)
    assert rows[0].active is True
    assert report.errors == 1
    assert report.deactivated_not_found == 0


@pytest.mark.asyncio
async def test_progress_callback_invoked_each_ticker(polygon_stub, monkeypatch):
    from ingestion.market_data import float_updater as fu

    rows = [_make_ticker("AAA"), _make_ticker("BBB"), _make_ticker("CCC")]
    monkeypatch.setattr(fu, "_load_active_tickers", AsyncMock(return_value=rows))

    polygon = MagicMock()
    polygon.get_ticker_details = AsyncMock(return_value={
        "float_shares": 1_000_000, "shares_outstanding": 1_000_000,
    })
    session = MagicMock()
    session.flush = AsyncMock()

    seen: list[tuple[int, int, str]] = []
    await fu.update_floats_for_universe(
        session, polygon,
        progress_callback=lambda visited, total, ticker: seen.append((visited, total, ticker)),
    )
    assert seen == [(1, 3, "AAA"), (2, 3, "BBB"), (3, 3, "CCC")]
