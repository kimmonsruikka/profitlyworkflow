"""Model versions repository.

Every prediction's `scorer_version` should resolve to a row here. rules-v1
is seeded by migration 0006; future GBDT models register here on training.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.model_version import ModelVersion
from data.repositories.schemas import ModelVersionRead


class ModelVersionsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, version_id: str) -> ModelVersionRead | None:
        row = await self.session.get(ModelVersion, version_id)
        return ModelVersionRead.model_validate(row) if row else None

    async def get_in_production(self) -> ModelVersionRead | None:
        stmt = select(ModelVersion).where(ModelVersion.in_production.is_(True))
        rows = (await self.session.execute(stmt)).scalars().all()
        if len(rows) > 1:
            # Hard error in production — only one model can be the in_production
            # writer at a time. Shadow models use in_shadow=True instead.
            raise RuntimeError(
                f"multiple in_production model versions: {[r.version_id for r in rows]}"
            )
        return ModelVersionRead.model_validate(rows[0]) if rows else None

    async def list_shadow(self) -> list[ModelVersionRead]:
        stmt = select(ModelVersion).where(ModelVersion.in_shadow.is_(True))
        rows = (await self.session.execute(stmt)).scalars().all()
        return [ModelVersionRead.model_validate(r) for r in rows]
