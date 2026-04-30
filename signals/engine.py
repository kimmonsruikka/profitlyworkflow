"""SignalEngine — runs a scorer + persists the prediction row in one call.

This is the chokepoint the rest of the system uses to emit predictions.
Per the project's critical rules, every signal evaluation MUST write a
predictions row before any alert fires. Calling the scorer directly from
alert code paths is a bug.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from data.repositories.predictions_repo import PredictionsRepository
from data.repositories.schemas import PredictionCreate, PredictionRead
from signals.scoring.catalyst_scorer import CatalystScorer, ScoreResult


class SignalEngine:
    def __init__(
        self,
        scorer: CatalystScorer,
        session: AsyncSession,
    ) -> None:
        self.scorer = scorer
        self.session = session

    async def evaluate(
        self,
        *,
        ticker: str,
        signal_type: str,
        features: dict[str, Any],
        predicted_window_minutes: int,
        predicted_target_pct: float | None = None,
    ) -> tuple[ScoreResult, PredictionRead]:
        """Score the features and immediately persist the prediction.

        Returns (score_result, prediction_row). Callers decide whether to
        fire an alert based on the probability — the prediction row is
        already written either way, which is the point: even predictions
        that don't trigger alerts feed the calibration loop.
        """
        result = self.scorer.score(features)
        repo = PredictionsRepository(self.session)
        prediction = await repo.create(
            PredictionCreate(
                ticker=ticker,
                signal_type=signal_type,
                feature_vector=result.feature_vector,
                feature_schema_version=result.feature_schema_version,
                scorer_version=result.scorer_version,
                confidence=result.confidence_decimal,
                predicted_window_minutes=predicted_window_minutes,
                predicted_target_pct=(
                    None if predicted_target_pct is None
                    else Decimal(str(predicted_target_pct))
                ),
                alert_sent=False,
            )
        )
        return result, prediction
