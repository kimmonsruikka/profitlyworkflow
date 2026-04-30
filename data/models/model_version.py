from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from data.models.base import Base


class ModelVersion(Base):
    """Catalog of every scorer that has ever written predictions.

    rules-v1 is seeded by migration 0006. GBDT graduates here once trained
    in shadow mode and promoted by setting in_production=True.
    """

    __tablename__ = "model_versions"

    version_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    model_class: Mapped[str] = mapped_column(String(40), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    trained_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    training_set_size: Mapped[int | None] = mapped_column(Integer)
    calibration_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    in_production: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_shadow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return (
            f"<ModelVersion {self.version_id} class={self.model_class} "
            f"prod={self.in_production} shadow={self.in_shadow}>"
        )
