from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from data.models.promoter_campaign import PromoterCampaign
from data.models.promoter_entity import PromoterEntity
from data.models.promoter_network_edge import PromoterNetworkEdge
from data.repositories.schemas import (
    PromoterCampaignSchema,
    PromoterEntitySchema,
    PromoterFingerprint,
    PromoterNetworkEdgeSchema,
)


class PromoterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_entity_by_name(self, name: str) -> PromoterEntitySchema | None:
        stmt = select(PromoterEntity).where(PromoterEntity.name == name)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return PromoterEntitySchema.model_validate(row) if row else None

    async def get_entities_by_type(self, entity_type: str) -> list[PromoterEntitySchema]:
        stmt = (
            select(PromoterEntity)
            .where(PromoterEntity.type == entity_type)
            .order_by(PromoterEntity.name)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [PromoterEntitySchema.model_validate(r) for r in rows]

    async def get_campaign_history(
        self, entity_id: uuid.UUID, limit: int = 20
    ) -> list[PromoterCampaignSchema]:
        stmt = (
            select(PromoterCampaign)
            .where(PromoterCampaign.entity_id == entity_id)
            .order_by(PromoterCampaign.launch_date.desc().nullslast())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [PromoterCampaignSchema.model_validate(r) for r in rows]

    async def get_network_edges(
        self, entity_id: uuid.UUID
    ) -> list[PromoterNetworkEdgeSchema]:
        stmt = select(PromoterNetworkEdge).where(
            or_(
                PromoterNetworkEdge.entity_a == entity_id,
                PromoterNetworkEdge.entity_b == entity_id,
            )
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [PromoterNetworkEdgeSchema.model_validate(r) for r in rows]

    async def get_cluster_by_entities(
        self, entity_ids: list[uuid.UUID]
    ) -> list[PromoterNetworkEdgeSchema]:
        """Edges where both endpoints are in the supplied entity set."""
        if not entity_ids:
            return []
        stmt = select(PromoterNetworkEdge).where(
            and_(
                PromoterNetworkEdge.entity_a.in_(entity_ids),
                PromoterNetworkEdge.entity_b.in_(entity_ids),
            )
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [PromoterNetworkEdgeSchema.model_validate(r) for r in rows]

    async def get_active_campaigns(self) -> list[PromoterCampaignSchema]:
        """Campaigns whose end_date is null or in the future."""
        today = datetime.utcnow().date()
        stmt = (
            select(PromoterCampaign)
            .where(
                or_(
                    PromoterCampaign.end_date.is_(None),
                    PromoterCampaign.end_date >= today,
                )
            )
            .order_by(PromoterCampaign.launch_date.desc().nullslast())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [PromoterCampaignSchema.model_validate(r) for r in rows]

    async def upsert_entity(self, entity_data: dict) -> PromoterEntitySchema:
        """Upsert by name (entities don't have a stable natural key beyond it)."""
        existing_stmt = select(PromoterEntity).where(
            PromoterEntity.name == entity_data["name"]
        )
        existing = (await self.session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            for k, v in entity_data.items():
                if k != "name":
                    setattr(existing, k, v)
            existing.updated_at = func.now()
            await self.session.flush()
            return PromoterEntitySchema.model_validate(existing)

        entity = PromoterEntity(**entity_data)
        self.session.add(entity)
        await self.session.flush()
        return PromoterEntitySchema.model_validate(entity)

    async def add_campaign(self, campaign_data: dict) -> PromoterCampaignSchema:
        campaign = PromoterCampaign(**campaign_data)
        self.session.add(campaign)
        await self.session.flush()
        return PromoterCampaignSchema.model_validate(campaign)

    async def add_network_edge(
        self, entity_a: uuid.UUID, entity_b: uuid.UUID, filing_ref: str
    ) -> PromoterNetworkEdgeSchema:
        """Create or increment a co-appearance edge between two entities."""
        existing_stmt = select(PromoterNetworkEdge).where(
            or_(
                and_(
                    PromoterNetworkEdge.entity_a == entity_a,
                    PromoterNetworkEdge.entity_b == entity_b,
                ),
                and_(
                    PromoterNetworkEdge.entity_a == entity_b,
                    PromoterNetworkEdge.entity_b == entity_a,
                ),
            )
        )
        existing = (await self.session.execute(existing_stmt)).scalar_one_or_none()
        now = datetime.utcnow()

        if existing:
            existing.co_appearance_count = (existing.co_appearance_count or 0) + 1
            existing.last_co_appearance = now
            refs = list(existing.filing_references or [])
            if filing_ref not in refs:
                refs.append(filing_ref)
            existing.filing_references = refs
            await self.session.flush()
            return PromoterNetworkEdgeSchema.model_validate(existing)

        edge = PromoterNetworkEdge(
            entity_a=entity_a,
            entity_b=entity_b,
            co_appearance_count=1,
            first_co_appearance=now,
            last_co_appearance=now,
            filing_references=[filing_ref],
        )
        self.session.add(edge)
        await self.session.flush()
        return PromoterNetworkEdgeSchema.model_validate(edge)

    async def get_reliability_score(self, entity_id: uuid.UUID) -> float:
        """winners / total resolved campaigns. Returns 0.0 if no resolved campaigns."""
        stmt = select(
            func.count().label("total"),
            func.sum(
                func.case((PromoterCampaign.campaign_result == "winner", 1), else_=0)
            ).label("winners"),
        ).where(
            PromoterCampaign.entity_id == entity_id,
            PromoterCampaign.campaign_result.in_(("winner", "loser")),
        )
        row = (await self.session.execute(stmt)).one()
        total = row.total or 0
        winners = row.winners or 0
        return float(winners) / float(total) if total else 0.0

    async def get_promoter_fingerprint(
        self, entity_id: uuid.UUID
    ) -> PromoterFingerprint:
        stmt = select(
            PromoterCampaign.day1_move_pct,
            PromoterCampaign.days_to_peak,
            PromoterCampaign.decay_speed,
        ).where(PromoterCampaign.entity_id == entity_id)
        rows = (await self.session.execute(stmt)).all()

        day1_values = [float(r.day1_move_pct) for r in rows if r.day1_move_pct is not None]
        peak_values = [r.days_to_peak for r in rows if r.days_to_peak is not None]
        decay_counts = Counter(r.decay_speed for r in rows if r.decay_speed)

        return PromoterFingerprint(
            entity_id=entity_id,
            campaign_count=len(rows),
            avg_day1_move_pct=(sum(day1_values) / len(day1_values)) if day1_values else None,
            avg_days_to_peak=(sum(peak_values) / len(peak_values)) if peak_values else None,
            decay_speed_distribution=dict(decay_counts),
        )
