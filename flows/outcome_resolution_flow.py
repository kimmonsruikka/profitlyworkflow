"""Outcome resolution flow.

Walks predictions whose window has matured and writes their outcome row.
Runs hourly during market hours plus once at 17:00 ET for a full sweep
that catches anything outside market hours and slow S2 maturations.

The price-data fetch is behind a Protocol injection point so unit tests
can supply a fake price series and Polygon integration can be wired in
later without touching this file.

Deploy with (Prefect 3):
    prefect deploy flows/outcome_resolution_flow.py:outcome_resolution_flow \\
        --name hourly-resolution \\
        --pool default-agent-pool \\
        --cron "0 9-16 * * 1-5" \\
        --timezone America/New_York
    prefect deploy flows/outcome_resolution_flow.py:outcome_resolution_flow \\
        --name daily-resolution-sweep \\
        --pool default-agent-pool \\
        --cron "0 17 * * 1-5" \\
        --timezone America/New_York
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Literal, Protocol, runtime_checkable

from loguru import logger as _logger
from prefect import flow, get_run_logger, task

from config import constants
from data.db import get_session
from data.repositories.outcomes_repo import OutcomesRepository
from data.repositories.predictions_repo import PredictionsRepository
from data.repositories.schemas import (
    OutcomeCreate,
    PredictionRead,
)


# ---------------------------------------------------------------------------
# Price-source Protocol — concrete impl injected by callers.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PriceBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None


@dataclass(frozen=True)
class OHLCVResult:
    """Return value of PriceSource.get_ohlcv.

    `is_complete` is the gate the resolver uses to decide WIN/LOSS/NEUTRAL
    vs. INVALID-with-insufficient-bars. `missing_ranges` are subranges
    Polygon couldn't fill (404 / NoData / network).
    """

    bars: list[PriceBar]
    source: Literal["cache", "polygon", "mixed"] = "polygon"
    missing_ranges: list[tuple[datetime, datetime]] = field(default_factory=list)
    is_complete: bool = True


@runtime_checkable
class PriceSource(Protocol):
    """Minimal contract the resolver needs.

    `get_ohlcv` takes a granularity ('1m' / '5m') and returns a structured
    OHLCVResult so the resolver can branch on completeness without
    re-running the bar-count math.
    """

    name: str  # e.g. "polygon", "test-fake" — recorded on the outcome row

    def get_ohlcv(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        granularity: str = "1m",
    ) -> OHLCVResult:
        ...


# ---------------------------------------------------------------------------
# Pure outcome-classification logic (testable independently of Prefect/DB)
# ---------------------------------------------------------------------------
def classify_outcome(
    *,
    realized_return_pct: float | None,
    hit_target: bool | None,
    hit_stop: bool | None,
) -> str:
    """Apply OUTCOME_LABEL_RULES to derive WIN/LOSS/NEUTRAL/INVALID.

    INVALID: we couldn't compute a realized return AND neither flag is set —
    typically means no price data was available for the window.
    """
    rules = constants.OUTCOME_LABEL_RULES
    if realized_return_pct is None and hit_target is None and hit_stop is None:
        return rules["label_invalid"]
    if hit_target:
        return rules["label_win"]
    if hit_stop:
        return rules["label_loss"]
    if realized_return_pct is None:
        return rules["label_invalid"]
    if realized_return_pct >= rules["win_threshold_pct"]:
        return rules["label_win"]
    if realized_return_pct <= rules["loss_threshold_pct"]:
        return rules["label_loss"]
    return rules["label_neutral"]


def compute_outcome_metrics(
    bars: list[PriceBar],
    *,
    target_pct: float | None,
    stop_pct: float | None = None,
) -> dict:
    """Reduce a price series into MFE / MAE / realized / target-hit / stop-hit.

    Entry price is the first bar's open. Exit is the last bar's close.
    All percentages are signed relative to entry.
    """
    if not bars:
        return {
            "max_favorable_excursion_pct": None,
            "max_adverse_excursion_pct": None,
            "realized_return_pct": None,
            "hit_target": None,
            "hit_stop": None,
        }

    entry = bars[0].open
    if entry <= 0:
        return {
            "max_favorable_excursion_pct": None,
            "max_adverse_excursion_pct": None,
            "realized_return_pct": None,
            "hit_target": None,
            "hit_stop": None,
        }

    highest = max(b.high for b in bars)
    lowest = min(b.low for b in bars)
    exit_close = bars[-1].close

    mfe = (highest - entry) / entry * 100.0
    mae = (lowest - entry) / entry * 100.0
    realized = (exit_close - entry) / entry * 100.0

    hit_target = None if target_pct is None else mfe >= target_pct
    hit_stop = None if stop_pct is None else mae <= stop_pct  # stop is signed negative

    return {
        "max_favorable_excursion_pct": mfe,
        "max_adverse_excursion_pct": mae,
        "realized_return_pct": realized,
        "hit_target": hit_target,
        "hit_stop": hit_stop,
    }


def _select_granularity(window_minutes: int) -> str:
    rules = constants.PRICE_GRANULARITY_RULES
    if window_minutes <= rules["short_window_max_minutes"]:
        return rules["short_granularity"]
    return rules["long_granularity"]


# ---------------------------------------------------------------------------
# Resolver — pure business logic, takes a session + price source.
# ---------------------------------------------------------------------------
async def resolve_one(
    session,
    prediction: PredictionRead,
    price_source: PriceSource,
) -> uuid.UUID | None:
    """Compute the outcome row for one matured prediction.

    Returns the outcome_id when an outcome row is written. Returns None
    on transient errors so the prediction stays unresolved and gets
    retried on the next flow run.
    """
    # Local imports avoid the polygon SDK at module-import time. CI runs
    # without POLYGON_API_KEY and the polygon errors are only inspected
    # when an actual price-source call raised them.
    from ingestion.market_data.polygon_client import (
        PolygonNoDataError,
        PolygonNotFoundError,
    )

    window_close = prediction.created_at + timedelta(
        minutes=prediction.predicted_window_minutes
    )
    granularity = _select_granularity(prediction.predicted_window_minutes)
    outcomes = OutcomesRepository(session)
    predictions = PredictionsRepository(session)

    # ---- Fetch price data, branch on what came back ----
    try:
        result: OHLCVResult = await _maybe_await(
            price_source.get_ohlcv(
                prediction.ticker,
                prediction.created_at,
                window_close,
                granularity,
            )
        )
    except (PolygonNotFoundError, PolygonNoDataError):
        # Ticker doesn't exist on Polygon, or no bars in the entire
        # window. Both are legitimate INVALID outcomes — the prediction
        # was made on something we can't measure.
        written = await outcomes.write_invalid_outcome(
            prediction.prediction_id,
            window_close_at=window_close,
            reason=constants.INVALID_REASONS["NO_PRICE_DATA"],
            price_data_source=price_source.name,
        )
        await predictions.attach_outcome(prediction.prediction_id, written.outcome_id)
        return written.outcome_id
    except Exception:
        # Transient — leave the prediction unresolved for the next sweep.
        _logger.exception(
            "resolve_one transient error for prediction {}", prediction.prediction_id,
        )
        return None

    if not result.is_complete:
        # Polygon returned data but not enough of it. INVALID with a
        # different reason so the dashboard can group these separately.
        written = await outcomes.write_invalid_outcome(
            prediction.prediction_id,
            window_close_at=window_close,
            reason=constants.INVALID_REASONS["INSUFFICIENT_BARS"],
            price_data_source=price_source.name,
        )
        await predictions.attach_outcome(prediction.prediction_id, written.outcome_id)
        return written.outcome_id

    # ---- Happy path: compute metrics, classify, write outcome ----
    target = (
        float(prediction.predicted_target_pct)
        if prediction.predicted_target_pct is not None else None
    )
    metrics = compute_outcome_metrics(result.bars, target_pct=target)

    label = classify_outcome(
        realized_return_pct=metrics["realized_return_pct"],
        hit_target=metrics["hit_target"],
        hit_stop=metrics["hit_stop"],
    )

    payload = OutcomeCreate(
        prediction_id=prediction.prediction_id,
        window_close_at=window_close,
        max_favorable_excursion_pct=_to_decimal(metrics["max_favorable_excursion_pct"]),
        max_adverse_excursion_pct=_to_decimal(metrics["max_adverse_excursion_pct"]),
        realized_return_pct=_to_decimal(metrics["realized_return_pct"]),
        paper_return_pct=_to_decimal(metrics["realized_return_pct"]),
        hit_target=metrics["hit_target"],
        hit_stop=metrics["hit_stop"],
        outcome_label=label,
        price_data_source=price_source.name,
    )
    written = await outcomes.create(payload)
    await predictions.attach_outcome(prediction.prediction_id, written.outcome_id)
    return written.outcome_id


async def _maybe_await(value):
    """Accept either a sync or async PriceSource.get_ohlcv return.

    Tests use a sync FakePriceSource (returns OHLCVResult directly);
    PolygonCachedPriceSource is async (returns a coroutine). Resolver
    accepts either shape.
    """
    import inspect as _inspect

    if _inspect.isawaitable(value):
        return await value
    return value


def _to_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(value, 3)))


# ---------------------------------------------------------------------------
# Prefect task + flow
# ---------------------------------------------------------------------------
@task(name="resolve-matured-predictions")
async def _resolve_all(price_source: PriceSource) -> dict[str, int]:
    counts = {"resolved": 0, "errors": 0, "labels": {}}
    async with get_session() as session:
        predictions = PredictionsRepository(session)
        matured = await predictions.get_unresolved_matured()
        for pred in matured:
            try:
                await resolve_one(session, pred, price_source)
                counts["resolved"] += 1
            except Exception:  # noqa: BLE001
                counts["errors"] += 1
                # Continue; one bad row shouldn't block the rest of the sweep.
    return counts


@flow(name="outcome-resolution")
async def outcome_resolution_flow(price_source: PriceSource | None = None) -> dict:
    """Resolve every matured prediction.

    `price_source` is injectable for tests. Production usage will wire in
    a Polygon-backed source — that wiring lives in a later PR.
    """
    logger = get_run_logger()
    if price_source is None:
        # Production wiring lives here once the Polygon adapter is built.
        # Until then, refusing to run prevents accidental data corruption
        # via wrong defaults.
        raise NotImplementedError(
            "outcome_resolution_flow requires an injected PriceSource. "
            "Polygon adapter wiring lands in a later PR."
        )

    result = await _resolve_all(price_source)
    logger.info("outcome resolution complete: %s", result)
    return result
