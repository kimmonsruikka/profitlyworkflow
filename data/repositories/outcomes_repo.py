"""Outcomes repository.

One outcome per prediction (UNIQUE constraint enforces 1:1). The
outcome-resolution flow is the only writer.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.outcome import Outcome
from data.repositories.schemas import OutcomeCreate, OutcomeRead


class OutcomesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, payload: OutcomeCreate) -> OutcomeRead:
        row = Outcome(
            prediction_id=payload.prediction_id,
            window_close_at=payload.window_close_at,
            max_favorable_excursion_pct=payload.max_favorable_excursion_pct,
            max_adverse_excursion_pct=payload.max_adverse_excursion_pct,
            realized_return_pct=payload.realized_return_pct,
            paper_return_pct=payload.paper_return_pct,
            hit_target=payload.hit_target,
            hit_stop=payload.hit_stop,
            outcome_label=payload.outcome_label,
            price_data_source=payload.price_data_source,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return OutcomeRead.model_validate(row)

    async def get_by_prediction(
        self, prediction_id: uuid.UUID
    ) -> OutcomeRead | None:
        stmt = select(Outcome).where(Outcome.prediction_id == prediction_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return OutcomeRead.model_validate(row) if row else None
