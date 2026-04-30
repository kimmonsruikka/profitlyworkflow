"""SignalEngine — runs a scorer + persists the prediction row in one call.

This is the chokepoint the rest of the system uses to emit predictions.
Per the project's critical rules, every signal evaluation MUST write a
predictions row before any alert fires. Calling the scorer directly from
alert code paths is a bug.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from config import constants
from data.repositories.predictions_repo import PredictionsRepository
from data.repositories.schemas import PredictionCreate, PredictionRead
from signals.features.edgar_features import extract_edgar_features
from signals.filters.edgar_prediction_filter import is_prediction_worthy
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

    async def evaluate_edgar_filing(
        self,
        filing: dict[str, Any],
        ticker_metadata: dict[str, Any],
    ) -> PredictionRead | None:
        """End-to-end EDGAR path: filter → features → score → persist.

        Returns the persisted PredictionRead when a prediction was
        written, or None when the filter rejected the filing. Logs the
        skip reason at INFO level so operators can audit the filter
        without scanning every filing.

        Exceptions inside this method propagate. The Celery task that
        calls this is the boundary that catches and logs errors so
        signal evaluation failures don't break filing persistence —
        keep this contract or the watcher will start losing filings.
        """
        worthy, skip_reason = is_prediction_worthy(
            filing,
            has_promoter_match=bool(ticker_metadata.get("promoter_match_count")),
        )
        ticker = filing.get("ticker") or ticker_metadata.get("ticker")
        if not worthy:
            logger.info(
                "edgar prediction skipped: ticker={t} form={f} reason={r}",
                t=ticker, f=filing.get("form_type"), r=skip_reason,
            )
            return None

        if not ticker:
            # Without a ticker we can't write a prediction row — log loud
            # but don't raise; this is a data-quality issue upstream.
            logger.warning(
                "edgar prediction worthy but no ticker resolved (cik={c}, accession={a})",
                c=filing.get("cik"), a=filing.get("accession_number"),
            )
            return None

        features = extract_edgar_features(filing, ticker_metadata)
        signal_type = _signal_type_for_filing(filing, ticker_metadata)
        defaults = constants.SIGNAL_TYPE_DEFAULTS[signal_type]

        _, prediction = await self.evaluate(
            ticker=ticker,
            signal_type=signal_type,
            features=features,
            predicted_window_minutes=int(defaults["window_minutes"]),
            predicted_target_pct=float(defaults["target_pct"]),
        )
        logger.info(
            "edgar prediction written: id={pid} ticker={t} signal={s} confidence={c}",
            pid=prediction.prediction_id,
            t=ticker,
            s=signal_type,
            c=prediction.confidence,
        )
        return prediction


def _signal_type_for_filing(
    filing: dict[str, Any], ticker_metadata: dict[str, Any]
) -> str:
    """Map a filing to one of SIGNAL_TYPE_DEFAULTS keys.

    Priority order:
      1. 8-K with material items → S1_CATALYST (intraday momentum play)
      2. S-3 effective → S2_DILUTION_RISK (downward bias)
      3. Form 4 buy ≥ $50K → S2_CATEGORY_D (insider knows something)
      4. Promoter-network-only match → S2_CATEGORY_A (pre-promotion)
    """
    form_type = (filing.get("form_type") or "").upper()

    if form_type.startswith("8-K"):
        return "S1_CATALYST"
    if form_type.startswith("S-3") and filing.get("s3_effective"):
        return "S2_DILUTION_RISK"
    if form_type == "4":
        return "S2_CATEGORY_D"
    # Anything else here is here only because of a promoter-network match.
    if ticker_metadata.get("promoter_match_count"):
        return "S2_CATEGORY_A"
    # Fallback. The filter shouldn't have let us through, but if it did,
    # treat as catalyst — the scorer will give it a low probability if
    # the features don't justify one.
    return "S1_CATALYST"
