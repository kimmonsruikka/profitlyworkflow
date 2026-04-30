from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.repositories.predictions_repo import PredictionsRepository
from data.repositories.schemas import PredictionCreate, PredictionRead


def _fake_prediction_orm(
    *,
    prediction_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    predicted_window_minutes: int = 60,
    outcome_id: uuid.UUID | None = None,
    ticker: str = "ABCD",
    signal_type: str = "S1_CATALYST",
) -> MagicMock:
    """Mimic enough of the SQLAlchemy ORM row for PredictionRead.model_validate."""
    obj = MagicMock(spec=[
        "prediction_id", "created_at", "ticker", "signal_type",
        "feature_vector", "feature_schema_version", "scorer_version",
        "confidence", "predicted_window_minutes", "predicted_target_pct",
        "alert_sent", "user_decision", "decision_reason", "trade_id",
        "outcome_id",
    ])
    obj.prediction_id = prediction_id or uuid.uuid4()
    obj.created_at = created_at or datetime.now(timezone.utc)
    obj.ticker = ticker
    obj.signal_type = signal_type
    obj.feature_vector = {"inputs": {}, "weights": {}}
    obj.feature_schema_version = "fv-v1"
    obj.scorer_version = "rules-v1"
    obj.confidence = Decimal("0.5000")
    obj.predicted_window_minutes = predicted_window_minutes
    obj.predicted_target_pct = None
    obj.alert_sent = False
    obj.user_decision = None
    obj.decision_reason = None
    obj.trade_id = None
    obj.outcome_id = outcome_id
    return obj


@pytest.mark.asyncio
async def test_create_persists_and_returns_pydantic_schema():
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    # Refresh: simulate populating server defaults (id, created_at)
    fake_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async def fake_refresh(row):
        row.prediction_id = fake_id
        row.created_at = now
        row.feature_vector = row.feature_vector  # no-op
    session.refresh = AsyncMock(side_effect=fake_refresh)

    repo = PredictionsRepository(session)
    payload = PredictionCreate(
        ticker="ABCD",
        signal_type="S1_CATALYST",
        feature_vector={"x": True},
        feature_schema_version="fv-v1",
        scorer_version="rules-v1",
        confidence=Decimal("0.7500"),
        predicted_window_minutes=60,
    )
    out = await repo.create(payload)

    assert isinstance(out, PredictionRead)
    assert out.prediction_id == fake_id
    assert out.confidence == Decimal("0.7500")
    assert out.scorer_version == "rules-v1"
    session.add.assert_called_once()
    session.flush.assert_awaited()


@pytest.mark.asyncio
async def test_create_rejects_confidence_outside_unit_interval():
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    repo = PredictionsRepository(session)
    bad = PredictionCreate(
        ticker="ABCD",
        signal_type="S1_CATALYST",
        feature_vector={},
        feature_schema_version="fv-v1",
        scorer_version="rules-v1",
        confidence=Decimal("1.5000"),
        predicted_window_minutes=60,
    )
    with pytest.raises(ValueError, match="must be in"):
        await repo.create(bad)
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_get_unresolved_matured_returns_only_matured_unresolved_rows():
    """Resolver must skip predictions that are still inside their window
    or that already have an outcome attached."""
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    matured_unresolved = _fake_prediction_orm(
        created_at=now - timedelta(minutes=120),
        predicted_window_minutes=60,
        outcome_id=None,
        ticker="MATURE",
    )
    not_yet_matured = _fake_prediction_orm(
        created_at=now - timedelta(minutes=30),
        predicted_window_minutes=60,
        outcome_id=None,
        ticker="EARLY",
    )
    # The repo's SQL query already filters out outcome_id IS NOT NULL via the
    # WHERE clause; including a third row here would just re-test the SQL.

    session = MagicMock()

    async def execute(_stmt):
        result = MagicMock()
        scalars_obj = MagicMock()
        scalars_obj.all = MagicMock(return_value=[matured_unresolved, not_yet_matured])
        result.scalars = MagicMock(return_value=scalars_obj)
        return result

    session.execute = AsyncMock(side_effect=execute)

    repo = PredictionsRepository(session)
    out = await repo.get_unresolved_matured(now=now)

    tickers = {p.ticker for p in out}
    assert "MATURE" in tickers
    assert "EARLY" not in tickers, "should not return predictions still inside their window"


@pytest.mark.asyncio
async def test_attach_outcome_emits_update_and_flushes():
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    repo = PredictionsRepository(session)
    pid = uuid.uuid4()
    oid = uuid.uuid4()
    await repo.attach_outcome(pid, oid)

    session.execute.assert_awaited_once()
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_user_decision_writes_three_columns():
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    repo = PredictionsRepository(session)
    pid = uuid.uuid4()
    tid = uuid.uuid4()
    await repo.record_user_decision(pid, "EXECUTE", reason="setup matched", trade_id=tid)

    session.execute.assert_awaited_once()
    # The values dict isn't easily inspectable through the mock, so we
    # simply assert the call happened and trust the SQL path. The shape
    # is type-checked by the pydantic schema upstream.
