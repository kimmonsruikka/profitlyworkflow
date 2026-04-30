from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest


# Stub polygon-api-client so PolygonClient is importable in CI without the
# real package. Same pattern test_broker.py uses.
def _stub_polygon_module() -> None:
    if "polygon" in sys.modules:
        return
    polygon = types.ModuleType("polygon")
    polygon.RESTClient = MagicMock(name="RESTClient")
    sys.modules["polygon"] = polygon


_stub_polygon_module()


from flows.outcome_resolution_flow import OHLCVResult, PriceBar  # noqa: E402
from ingestion.market_data.polygon_client import (  # noqa: E402
    PolygonNoDataError,
    PolygonNotFoundError,
)
from ingestion.market_data.polygon_price_source import (  # noqa: E402
    PolygonCachedPriceSource,
    identify_gaps,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def t0() -> datetime:
    # Tuesday 09:30 ET = 13:30 UTC. Inside a regular session.
    return datetime(2026, 4, 7, 13, 30, tzinfo=timezone.utc)


def _bar_dict(ts: datetime, price: float, volume: int = 1000) -> dict:
    return {
        "ticker": "ABCD",
        "granularity": "1m",
        "open": price,
        "high": price + 0.05,
        "low": price - 0.05,
        "close": price,
        "volume": volume,
        "vwap": price,
        "timestamp": ts,
    }


def _make_session_factory(read_bars: list[PriceBar], capture: dict | None = None):
    """Return a session-factory async context manager whose mocked session
    yields `read_bars` from the cache and captures Polygon writes."""
    session = MagicMock()

    async def execute(stmt):
        result = MagicMock()
        scalars_obj = MagicMock()
        scalars_obj.all = MagicMock(return_value=_orm_rows_from(read_bars))
        result.scalars = MagicMock(return_value=scalars_obj)
        if capture is not None:
            capture.setdefault("execute_calls", []).append(stmt)
        return result

    session.execute = AsyncMock(side_effect=execute)
    session.flush = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    return factory, session


def _orm_rows_from(bars: list[PriceBar]):
    """Convert PriceBar fixtures to mock-ORM rows with the same attrs the
    cache reader pulls."""
    rows = []
    for b in bars:
        m = MagicMock()
        m.ticker = "ABCD"
        m.granularity = "1m"
        m.timestamp = b.timestamp
        m.open = Decimal(str(b.open))
        m.high = Decimal(str(b.high))
        m.low = Decimal(str(b.low))
        m.close = Decimal(str(b.close))
        m.volume = b.volume
        rows.append(m)
    return rows


# ---------------------------------------------------------------------------
# identify_gaps — pure logic, easiest to pin down with direct asserts
# ---------------------------------------------------------------------------
def test_identify_gaps_empty_cache_returns_full_range(t0):
    end = t0 + timedelta(minutes=60)
    gaps = identify_gaps([], t0, end, "1m")
    assert gaps == [(t0, end)]


def test_identify_gaps_full_coverage_returns_empty(t0):
    """Cached bars span the full window — no gaps."""
    bars = [t0 + timedelta(minutes=i) for i in range(0, 61, 1)]
    gaps = identify_gaps(bars, t0, t0 + timedelta(minutes=60), "1m")
    assert gaps == []


def test_identify_gaps_skips_short_intra_gaps(t0):
    """A 30-min gap inside a fetched range shouldn't trigger a refetch —
    Polygon legitimately returns no bar for minutes the ticker didn't trade."""
    cached = [
        t0 + timedelta(minutes=0),
        t0 + timedelta(minutes=15),
        # 30-min gap here
        t0 + timedelta(minutes=45),
        t0 + timedelta(minutes=60),
    ]
    gaps = identify_gaps(cached, t0, t0 + timedelta(minutes=60), "1m")
    # Both endpoints covered; intra-range gap is under the 60min skip threshold
    assert gaps == []


def test_identify_gaps_emits_range_for_wide_intra_gap(t0):
    cached = [t0, t0 + timedelta(minutes=120)]  # 2-hour gap
    gaps = identify_gaps(cached, t0, t0 + timedelta(minutes=120), "1m")
    assert len(gaps) == 1
    gap_start, gap_end = gaps[0]
    assert gap_start == t0
    assert gap_end == t0 + timedelta(minutes=120)


# ---------------------------------------------------------------------------
# Cache full hit — no Polygon call
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_full_hit_does_not_call_polygon(t0):
    cached = [PriceBar(t0 + timedelta(minutes=i), 4.0, 4.05, 3.95, 4.02) for i in range(60)]
    factory, session = _make_session_factory(cached)

    polygon = MagicMock()
    polygon.get_aggregates = AsyncMock()  # should not be awaited

    src = PolygonCachedPriceSource(polygon, factory)
    result = await src.get_ohlcv("ABCD", t0, t0 + timedelta(minutes=60), "1m")

    polygon.get_aggregates.assert_not_awaited()
    assert result.source == "cache"
    assert len(result.bars) == 60


# ---------------------------------------------------------------------------
# Cache full miss — Polygon called once, response written back, returned
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_full_miss_fetches_polygon_and_writes_back(t0):
    factory, session = _make_session_factory([])

    fetched = [
        _bar_dict(t0 + timedelta(minutes=i), 4.0 + i * 0.01) for i in range(10)
    ]
    polygon = MagicMock()
    polygon.get_aggregates = AsyncMock(return_value=fetched)

    src = PolygonCachedPriceSource(polygon, factory)
    result = await src.get_ohlcv("ABCD", t0, t0 + timedelta(minutes=10), "1m")

    polygon.get_aggregates.assert_awaited_once()
    assert result.source == "polygon"
    assert len(result.bars) == 10
    # Cache write happened (one execute for the read, one for the upsert)
    assert session.execute.await_count >= 2
    session.flush.assert_awaited()


# ---------------------------------------------------------------------------
# Cache PARTIAL hit — only the missing range is fetched, results merged
# This is the load-bearing test. Make it thorough.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_partial_hit_fetches_only_gap_and_merges(t0):
    """First half of window cached, second half missing.

    Verifies:
      - Polygon is called exactly once (one gap)
      - The fetch range covers only the missing portion (within tolerance)
      - The merged result has all bars, sorted by timestamp
      - source == 'mixed'
    """
    end = t0 + timedelta(minutes=240)  # 4-hour window

    # Cache covers the first 60 minutes only.
    cached = [
        PriceBar(t0 + timedelta(minutes=i), 4.0, 4.05, 3.95, 4.02)
        for i in range(0, 60, 5)
    ]

    factory, session = _make_session_factory(cached)

    # Polygon will return bars covering minutes 60..240
    fetched_after = [
        _bar_dict(t0 + timedelta(minutes=i), 4.5) for i in range(60, 240, 5)
    ]
    polygon = MagicMock()
    polygon.get_aggregates = AsyncMock(return_value=fetched_after)

    src = PolygonCachedPriceSource(polygon, factory)
    result = await src.get_ohlcv("ABCD", t0, end, "1m")

    # Exactly one Polygon call — for the trailing gap only.
    assert polygon.get_aggregates.await_count == 1
    call_args = polygon.get_aggregates.await_args
    fetched_ticker, fetched_start, fetched_end, fetched_gran = call_args.args
    assert fetched_ticker == "ABCD"
    assert fetched_gran == "1m"
    # Gap should start at or after the last cached bar (t0 + 55 min).
    assert fetched_start >= t0 + timedelta(minutes=55)
    # And reach to the window end.
    assert fetched_end == end

    assert result.source == "mixed"
    # Cached portion + Polygon portion — merged, deduped, sorted.
    timestamps = [b.timestamp for b in result.bars]
    assert timestamps == sorted(timestamps)
    assert len(result.bars) == len(cached) + len(fetched_after)

    # Cache write happened for the new bars.
    assert session.execute.await_count >= 2
    session.flush.assert_awaited()


# ---------------------------------------------------------------------------
# Polygon returns empty → PolygonNoDataError surfaces (caught and recorded)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_polygon_no_data_marks_range_as_missing(t0):
    factory, session = _make_session_factory([])

    polygon = MagicMock()
    polygon.get_aggregates = AsyncMock(side_effect=PolygonNoDataError("empty"))

    src = PolygonCachedPriceSource(polygon, factory)
    result = await src.get_ohlcv("ABCD", t0, t0 + timedelta(minutes=10), "1m")

    assert result.bars == []
    assert len(result.missing_ranges) == 1
    assert result.is_complete is False


@pytest.mark.asyncio
async def test_polygon_404_marks_range_as_missing(t0):
    factory, session = _make_session_factory([])

    polygon = MagicMock()
    polygon.get_aggregates = AsyncMock(side_effect=PolygonNotFoundError("404"))

    src = PolygonCachedPriceSource(polygon, factory)
    result = await src.get_ohlcv("ZZZZZ", t0, t0 + timedelta(minutes=10), "1m")

    assert result.bars == []
    assert len(result.missing_ranges) == 1


# ---------------------------------------------------------------------------
# is_complete computation against expected bar count
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_is_complete_true_when_above_threshold(t0):
    """A full trading session with all 390 minute-bars is is_complete=True."""
    end = t0 + timedelta(hours=6, minutes=30)  # full session
    cached = [
        PriceBar(t0 + timedelta(minutes=i), 4.0, 4.05, 3.95, 4.02)
        for i in range(390)
    ]
    factory, _ = _make_session_factory(cached)

    polygon = MagicMock()
    polygon.get_aggregates = AsyncMock()

    src = PolygonCachedPriceSource(polygon, factory)
    result = await src.get_ohlcv("ABCD", t0, end, "1m")

    assert result.is_complete is True


@pytest.mark.asyncio
async def test_is_complete_false_when_below_threshold(t0):
    """Only ~10% of expected bars present → is_complete=False."""
    end = t0 + timedelta(hours=6, minutes=30)
    # Only 30 bars — 7.7% of expected 390
    cached = [
        PriceBar(t0 + timedelta(minutes=i), 4.0, 4.05, 3.95, 4.02)
        for i in range(30)
    ]
    factory, _ = _make_session_factory(cached)

    polygon = MagicMock()
    polygon.get_aggregates = AsyncMock(side_effect=PolygonNoDataError("no more bars"))

    src = PolygonCachedPriceSource(polygon, factory)
    result = await src.get_ohlcv("ABCD", t0, end, "1m")

    assert result.is_complete is False
