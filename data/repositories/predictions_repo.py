"""Predictions repository.

Predictions are immutable. To "correct" a prediction, write a new one with
a `supersedes` reference inside its feature_vector — never mutate an
existing row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.prediction import Prediction
from data.repositories.schemas import PredictionCreate, PredictionRead


class PredictionsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, payload: PredictionCreate) -> PredictionRead:
        if not (Decimal("0") <= payload.confidence <= Decimal("1")):
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {payload.confidence}"
            )
        row = Prediction(
            ticker=payload.ticker,
            signal_type=payload.signal_type,
            feature_vector=payload.feature_vector,
            feature_schema_version=payload.feature_schema_version,
            scorer_version=payload.scorer_version,
            confidence=payload.confidence,
            predicted_window_minutes=payload.predicted_window_minutes,
            predicted_target_pct=payload.predicted_target_pct,
            alert_sent=payload.alert_sent,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return PredictionRead.model_validate(row)

    async def get_by_id(self, prediction_id: uuid.UUID) -> PredictionRead | None:
        row = await self.session.get(Prediction, prediction_id)
        return PredictionRead.model_validate(row) if row else None

    async def get_unresolved_matured(self, *, now: datetime | None = None) -> list[PredictionRead]:
        """Return predictions that have matured but no outcome row yet.

        A prediction has matured when
            created_at + predicted_window_minutes minutes <= now.
        Unresolved means outcome_id IS NULL. The combination is what the
        outcome-resolution flow walks every cycle.

        Implemented in Python for the now-comparison since SQLAlchemy can't
        emit `created_at + (col * interval '1 min')` portably across
        backends (sqlite in CI, Postgres in prod). Reads the partial-index
        side first (`outcome_id IS NULL`), then filters by maturity in app.
        """
        current = now or datetime.now(timezone.utc)
        stmt = (
            select(Prediction)
            .where(Prediction.outcome_id.is_(None))
            .order_by(Prediction.created_at)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        out: list[PredictionRead] = []
        for row in rows:
            mature_at = row.created_at + timedelta(minutes=row.predicted_window_minutes)
            if mature_at <= current:
                out.append(PredictionRead.model_validate(row))
        return out

    async def attach_outcome(
        self, prediction_id: uuid.UUID, outcome_id: uuid.UUID
    ) -> None:
        """Set the outcome_id back-reference on the prediction row."""
        await self.session.execute(
            update(Prediction)
            .where(Prediction.prediction_id == prediction_id)
            .values(outcome_id=outcome_id)
        )
        await self.session.flush()

    async def record_user_decision(
        self,
        prediction_id: uuid.UUID,
        decision: str,
        reason: str | None = None,
        trade_id: uuid.UUID | None = None,
    ) -> None:
        """Record the operator's EXECUTE / PASS / EXPIRED decision.

        Predictions are otherwise immutable; the four columns user_decision,
        decision_reason, trade_id, and outcome_id are the only "writable"
        slots after creation. Don't add more.
        """
        await self.session.execute(
            update(Prediction)
            .where(Prediction.prediction_id == prediction_id)
            .values(user_decision=decision, decision_reason=reason, trade_id=trade_id)
        )
        await self.session.flush()
