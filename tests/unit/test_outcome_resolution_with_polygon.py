"""End-to-end tests for resolve_one with the OHLCVResult-shaped PriceSource.

Each test injects a fake price source matching the new Protocol shape
(returns OHLCVResult, accepts granularity) and asserts that resolve_one
writes the right outcome row given the result the source returned.
"""

from __future__ import annotations

import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest


# Stub polygon SDK first so polygon_client imports cleanly.
def _stub_polygon():
    if "polygon" in sys.modules:
        return
    polygon = types.ModuleType("polygon")
    polygon.RESTClient = MagicMock(name="RESTClient")
    sys.modules["polygon"] = polygon


_stub_polygon()


from config import constants  # noqa: E402
from data.repositories.schemas import PredictionRead  # noqa: E402
from flows.outcome_resolution_flow import (  # noqa: E402
    OHLCVResult,
    PriceBar,
    resolve_one,
)
from ingestion.market_data.polygon_client import (  # noqa: E402
    PolygonNoDataError,
    PolygonNotFoundError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_prediction(
    *,
    window_minutes: int = 60,
    target_pct: Decimal | None = Decimal("5.0"),
    ticker: str = "ABCD",
) -> PredictionRead:
    return PredictionRead(
        prediction_id=uuid.uuid4(),
        created_at=datetime(2026, 4, 7, 14, 0, tzinfo=timezone.utc),
        ticker=ticker,
        signal_type="S1_CATALYST",
        feature_vector={"inputs": {}, "weights": {}},
        feature_schema_version="fv-v1",
        scorer_version="rules-v1",
        confidence=Decimal("0.7000"),
        predicted_window_minutes=window_minutes,
        predicted_target_pct=target_pct,
        alert_sent=False,
    )


class FakePriceSource:
    name = "test-fake"

    def __init__(self, result: OHLCVResult | Exception):
        self._result = result
        self.calls: list[tuple] = []

    async def get_ohlcv(self, ticker, start, end, granularity="1m"):
        self.calls.append((ticker, start, end, granularity))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _mock_session() -> MagicMock:
    """Mock session that satisfies OutcomesRepository.create + attach_outcome."""
    session = MagicMock()
    session.add = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    fake_outcome_id = uuid.uuid4()

    async def refresh(row):
        # Populate server-side defaults the repo expects.
        row.outcome_id = fake_outcome_id
        row.resolved_at = datetime.now(timezone.utc)
    session.refresh = AsyncMock(side_effect=refresh)
    session._fake_outcome_id = fake_outcome_id  # for assertions
    return session


def _winner_bars(start: datetime) -> list[PriceBar]:
    """Series that hits a +5% target."""
    return [
        PriceBar(start, 10.0, 10.1, 9.95, 10.05),
        PriceBar(start + timedelta(minutes=15), 10.05, 10.6, 10.0, 10.55),
    ]


# ---------------------------------------------------------------------------
# Happy path: complete data, target hit → WIN outcome
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_happy_path_writes_win_outcome():
    pred = _make_prediction(window_minutes=60, target_pct=Decimal("5.0"))
    bars = _winner_bars(pred.created_at)
    source = FakePriceSource(OHLCVResult(bars=bars, source="polygon", is_complete=True))
    session = _mock_session()

    outcome_id = await resolve_one(session, pred, source)

    assert outcome_id is not None
    # OutcomesRepository.create added a row + attach_outcome ran an UPDATE
    session.add.assert_called_once()
    # The added row carries the right label
    added = session.add.call_args.args[0]
    assert added.outcome_label == "WIN"
    assert added.invalid_reason is None


# ---------------------------------------------------------------------------
# Insufficient bars → INVALID with 'insufficient_bars'
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_insufficient_bars_writes_invalid():
    pred = _make_prediction()
    source = FakePriceSource(OHLCVResult(
        bars=[PriceBar(pred.created_at, 10.0, 10.1, 9.95, 10.05)],
        source="polygon",
        is_complete=False,
    ))
    session = _mock_session()

    outcome_id = await resolve_one(session, pred, source)

    assert outcome_id is not None
    added = session.add.call_args.args[0]
    assert added.outcome_label == "INVALID"
    assert added.invalid_reason == constants.INVALID_REASONS["INSUFFICIENT_BARS"]


# ---------------------------------------------------------------------------
# PolygonNoDataError → INVALID with 'no_price_data'
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_data_error_writes_invalid_with_no_price_data():
    pred = _make_prediction()
    source = FakePriceSource(PolygonNoDataError("no bars"))
    session = _mock_session()

    outcome_id = await resolve_one(session, pred, source)

    assert outcome_id is not None
    added = session.add.call_args.args[0]
    assert added.outcome_label == "INVALID"
    assert added.invalid_reason == constants.INVALID_REASONS["NO_PRICE_DATA"]


# ---------------------------------------------------------------------------
# PolygonNotFoundError → INVALID with 'no_price_data' (same bucket)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_not_found_error_writes_invalid_with_no_price_data():
    pred = _make_prediction()
    source = FakePriceSource(PolygonNotFoundError("404"))
    session = _mock_session()

    outcome_id = await resolve_one(session, pred, source)

    assert outcome_id is not None
    added = session.add.call_args.args[0]
    assert added.outcome_label == "INVALID"
    assert added.invalid_reason == constants.INVALID_REASONS["NO_PRICE_DATA"]


# ---------------------------------------------------------------------------
# Network error → NO outcome written; prediction stays unresolved
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transient_error_does_not_write_outcome():
    pred = _make_prediction()
    source = FakePriceSource(ConnectionError("timeout"))
    session = _mock_session()

    outcome_id = await resolve_one(session, pred, source)

    assert outcome_id is None
    session.add.assert_not_called()


# ---------------------------------------------------------------------------
# Granularity selection per window length
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_short_window_uses_1m_granularity():
    pred = _make_prediction(window_minutes=60)  # <= 1440
    source = FakePriceSource(OHLCVResult(
        bars=_winner_bars(pred.created_at), is_complete=True,
    ))
    session = _mock_session()
    await resolve_one(session, pred, source)

    assert source.calls[0][3] == "1m"


@pytest.mark.asyncio
async def test_long_window_uses_5m_granularity():
    pred = _make_prediction(window_minutes=24 * 60 + 60)  # > 1440
    source = FakePriceSource(OHLCVResult(
        bars=_winner_bars(pred.created_at), is_complete=True,
    ))
    session = _mock_session()
    await resolve_one(session, pred, source)

    assert source.calls[0][3] == "5m"
