"""Pre-market brief — runs on cron weekday mornings, sends a Telegram
summary of overnight system activity.

Sections:
  1. Header — date · brief number · feature schema version
  2. Calibration — last-30-resolved hit-rate split by confidence median
  3. This week's signal-pattern distribution
  4. New predictions in the last 24h, sorted by confidence DESC

Designed as a sequence of pure formatting helpers backed by a small
async DB layer. Tests target the helpers directly; the DB layer
gets mocked once.

Operational behavior
--------------------
  - Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from env. Missing or
    blank → exit 1 with stderr message; do NOT silently no-op.
  - On any error (DB / Telegram / data) prints traceback to stderr,
    exit 1.
  - Idempotent in the safe direction: running twice sends two briefs.
    "Already sent" gating is queued for v2.

Cron entry (lives in /etc/cron.d/pre-market-brief or equivalent —
NOT modified by this script):

  14 11 * * 1-5 trading cd /app/profitlyworkflow && \\
      ./venv/bin/python scripts/pre_market_brief.py >> \\
      /var/log/trading/brief.log 2>&1

11:14 UTC = 6:14 AM ET during EDT (Mar–Nov). During EST (Nov–Mar),
shift to `14 12 * * 1-5`. /var/log/trading/ must exist and be
writable by the trading user.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env.production"
if env_file.exists():
    load_dotenv(env_file, override=True)


# Anchor for the brief-number counter. May 6 2026 = brief #1.
BRIEF_NUMBER_ANCHOR = date(2026, 5, 6)

# How many resolved (HIT/MISS) outcomes to include in the calibration line.
CALIBRATION_WINDOW = 30

# Below this many resolved outcomes, the calibration line shows the
# "need ~50 for signal" placeholder rather than computed hit rates.
CALIBRATION_MIN_N_FOR_BUCKETS = 10

# Signal-pattern lookback for section 3.
PATTERN_WINDOW_DAYS = 7

# New-predictions lookback for section 4 (v1 — durable timestamp
# tracking is queued).
NEW_PREDICTION_LOOKBACK_HOURS = 24

# Telegram message size cap. Real cap is 4096; pad slightly so we
# don't send right at the edge.
TELEGRAM_MAX_LEN = 4000


# ---------------------------------------------------------------------------
# Pure data shapes for the formatting helpers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OutcomePoint:
    confidence: float
    hit: bool


@dataclass(frozen=True)
class PredictionRow:
    ticker: str
    signal_type: str
    confidence: Decimal
    feature_vector: dict[str, Any]
    created_at: datetime
    # Latest close from price_data (None when no price data exists)
    latest_price: Decimal | None
    # Float shares from tickers (None when unresolved)
    float_shares: int | None


# ---------------------------------------------------------------------------
# Pure formatting helpers
# ---------------------------------------------------------------------------
def format_brief_number(today: date, anchor: date = BRIEF_NUMBER_ANCHOR) -> int:
    """Brief #N counter. Anchor day = #1; each subsequent calendar day
    increments. Includes weekends in the counting (the cron schedule
    handles weekday-only delivery; the number itself counts days)."""
    delta = (today - anchor).days
    return max(1, delta + 1)


def format_header(today: date, schema_version: str) -> str:
    n = format_brief_number(today)
    return f"Pre-market {today.isoformat()} · brief #{n} · {schema_version}"


def format_human_int(n: int | float | None) -> str:
    """938_133 → '938K'; 1_174_718 → '1.2M'; 9_381_344 → '9.4M'.

    None → '—'. Below 1000 → exact integer. The 999/1000 K/M
    boundary uses 'K' for [1, 999_999] and 'M' for >= 1_000_000.
    1_000_000 displays as '1.0M' (one decimal) for stability.
    """
    if n is None:
        return "—"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{round(n / 1000)}K"
    return f"{n / 1_000_000:.1f}M"


def _firing_weights(feature_vector: dict[str, Any]) -> list[str]:
    """Keys whose weight existed in the snapshot AND whose input was truthy.

    The scorer stores both the feature inputs and the weights snapshot
    on each prediction; firing weights = intersection where input=True.
    """
    inputs = feature_vector.get("inputs") or {}
    weights = feature_vector.get("weights") or {}
    return sorted(k for k, _w in weights.items() if inputs.get(k))


def classify_signal_pattern(feature_vector: dict[str, Any]) -> str:
    """Map a prediction's feature vector to one of the brief categories.

    Priority order (first match wins):
      1. multi-signal — 3+ weights firing
      2. Form 4 buy — is_form4_buy True
      3. 8-K material — filing_form_type starts with '8-K' and
         edgar_priority_form fires
      4. S-3-only — filing_form_type starts with 'S-3' and ONLY
         edgar_priority_form fires
      5. other
    """
    firing = _firing_weights(feature_vector)
    inputs = feature_vector.get("inputs") or {}
    form_type = (inputs.get("filing_form_type") or "").strip()

    if len(firing) >= 3:
        return "multi-signal"
    if inputs.get("is_form4_buy"):
        return "Form 4 buy"
    if form_type.startswith("8-K") and "edgar_priority_form" in firing:
        return "8-K material"
    if (
        form_type.startswith("S-3")
        and firing == ["edgar_priority_form"]
    ):
        return "S-3-only"
    return "other"


def format_filing_context(feature_vector: dict[str, Any]) -> str:
    """One-phrase filing description from the feature_vector inputs.

    All data sourced from the prediction's stored inputs — no extra
    DB lookup. Order matters: most-specific first.
    """
    inputs = feature_vector.get("inputs") or {}
    form_type = (inputs.get("filing_form_type") or "").strip()

    if inputs.get("is_form4_buy"):
        v = inputs.get("form4_value_usd")
        if v:
            try:
                return f"Form 4 P-buy ${float(v) / 1000:.1f}K"
            except (TypeError, ValueError):
                pass
        return "Form 4 P-buy"

    if inputs.get("is_s3_effective"):
        return "S-3 effective"

    if form_type.startswith("8-K"):
        items = inputs.get("filing_items") or []
        if items:
            return f"8-K item {items[0]}"
        return "8-K"

    if form_type:
        return form_type

    return "—"


def format_calibration_line(outcomes: Sequence[OutcomePoint]) -> str:
    """Section 2.

    n < 10: 'Calibration: N outcomes resolved · need ~50 for signal'
    n >= 10: 'Calibration (last N outcomes): high-conf hit rate X% (n=A),
             low-conf hit rate Y% (n=B) · spread: ±Zpp'

    Spread sign convention: '+' when high-conf hit rate exceeds low-conf
    (the desired direction); '-' when reversed; always show explicitly.
    """
    n = len(outcomes)
    if n == 0:
        return "Calibration: 0 outcomes resolved · need ~50 for signal"
    if n < CALIBRATION_MIN_N_FOR_BUCKETS:
        return f"Calibration: {n} outcomes resolved · need ~50 for signal"

    confidences = [o.confidence for o in outcomes]
    median = statistics.median(confidences)

    high = [o for o in outcomes if o.confidence >= median]
    low = [o for o in outcomes if o.confidence < median]

    # If everything ties at the median, low bucket is empty. Fall back
    # to a plain hit rate without the high/low split.
    if not low or not high:
        hits = sum(1 for o in outcomes if o.hit)
        rate = round(100 * hits / n)
        return (
            f"Calibration (last {n} outcomes): hit rate {rate}% "
            f"(median confidence ties; no high/low split)"
        )

    high_rate = 100 * sum(1 for o in high if o.hit) / len(high)
    low_rate = 100 * sum(1 for o in low if o.hit) / len(low)
    spread = high_rate - low_rate
    sign = "+" if spread >= 0 else "-"

    # Spread leads — that's the headline number. The high/low breakdown
    # follows for context.
    return (
        f"Calibration: {sign}{abs(round(spread))}pp spread (last {n}) · "
        f"high-conf {round(high_rate)}% (n={len(high)}) · "
        f"low-conf {round(low_rate)}% (n={len(low)})"
    )


def format_signal_pattern_line(rows: Sequence[PredictionRow]) -> str:
    """Section 3 — pattern distribution for the last 7 days."""
    n = len(rows)
    if n == 0:
        return "This week: 0 predictions."

    counts: dict[str, int] = {}
    for r in rows:
        cat = classify_signal_pattern(r.feature_vector)
        counts[cat] = counts.get(cat, 0) + 1

    parts: list[str] = [f"This week: {n} predictions"]
    # Display order — fixed, not by count, so the message shape is stable.
    display_order = [
        ("S-3-only", "S-3-only"),
        ("Form 4 buy", "Form 4 buys"),
        ("8-K material", "8-K material"),
        ("multi-signal", "multi-signal"),
        ("other", "other"),
    ]
    for key, label in display_order:
        if counts.get(key):
            parts.append(f"{label} ({counts[key]})")
    return " · ".join(parts)


def format_prediction_block(row: PredictionRow) -> str:
    """Three-line block per prediction in section 4.

    Line 1: TICKER · SIGNAL_TYPE · CONFIDENCE
    Line 2: FLOAT_HUMAN · $PRICE · FILING_CONTEXT
    Line 3: weights: WEIGHT_LIST
    """
    confidence = f"{float(row.confidence):.4f}"
    float_human = format_human_int(row.float_shares)
    if row.latest_price is None:
        price = "$—"
    else:
        price = f"${float(row.latest_price):.2f}"
    context = format_filing_context(row.feature_vector)
    firing = _firing_weights(row.feature_vector)
    weights = ", ".join(firing) if firing else "(none)"

    return (
        f"{row.ticker} · {row.signal_type} · {confidence}\n"
        f"   {float_human} · {price} · {context}\n"
        f"   weights: {weights}"
    )


def format_new_predictions_section(rows: Sequence[PredictionRow]) -> str:
    """Section 4 — variable length, prediction blocks separated by blank lines."""
    if not rows:
        return "No new predictions in last 24h."
    blocks = [format_prediction_block(r) for r in rows]
    return "\n\n".join(blocks)


def build_message(
    today: date,
    schema_version: str,
    outcomes: Sequence[OutcomePoint],
    week_predictions: Sequence[PredictionRow],
    new_predictions: Sequence[PredictionRow],
) -> str:
    """Compose the full brief from pre-fetched data."""
    return "\n\n".join(
        [
            format_header(today, schema_version),
            format_calibration_line(outcomes),
            format_signal_pattern_line(week_predictions),
            format_new_predictions_section(new_predictions),
        ]
    )


def split_for_telegram(message: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split the brief on prediction-block boundaries (double newlines).

    At expected volumes (3-8 predictions/week → typically 5-30 lines)
    this is a no-op pass-through. Defensive only.
    """
    if len(message) <= max_len:
        return [message]
    parts: list[str] = []
    blocks = message.split("\n\n")
    current = ""
    for b in blocks:
        candidate = b if not current else current + "\n\n" + b
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            parts.append(current)
        # Block alone exceeds limit (very unlikely) — push as-is and let
        # Telegram truncate; logging will surface the overflow.
        current = b
    if current:
        parts.append(current)
    return parts


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------
def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Send via Telegram Bot API. Raises on non-2xx so the cron logger
    captures the failure and the script exits 1."""
    import httpx

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = httpx.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=15.0,
    )
    response.raise_for_status()


# ---------------------------------------------------------------------------
# DB layer — kept thin and isolated so tests can mock the whole orchestrator.
# ---------------------------------------------------------------------------
async def fetch_resolved_outcomes(session, limit: int = CALIBRATION_WINDOW) -> list[OutcomePoint]:
    """Last N HIT/MISS outcomes, joined with predictions for confidence."""
    from sqlalchemy import select
    from data.models.outcome import Outcome
    from data.models.prediction import Prediction

    stmt = (
        select(Prediction.confidence, Outcome.outcome_label)
        .join(Outcome, Outcome.prediction_id == Prediction.prediction_id)
        .where(Outcome.outcome_label.in_(("HIT", "MISS")))
        .order_by(Outcome.resolved_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        OutcomePoint(
            confidence=float(conf),
            hit=(label == "HIT"),
        )
        for conf, label in rows
    ]


async def _enrich_predictions(session, prediction_rows) -> list[PredictionRow]:
    """Attach latest price + float_shares to each prediction row."""
    from sqlalchemy import select
    from data.models.price_data import PriceData
    from data.models.ticker import Ticker

    if not prediction_rows:
        return []

    tickers = {r.ticker for r in prediction_rows}

    # Float shares per ticker
    ticker_rows = (
        await session.execute(
            select(Ticker.ticker, Ticker.float_shares).where(Ticker.ticker.in_(tickers))
        )
    ).all()
    float_by_ticker: dict[str, int | None] = {t: f for t, f in ticker_rows}

    # Latest close per ticker — daily granularity preferred, fall back to any.
    # Run one query per ticker; ticker count is small in practice (3-8/week).
    price_by_ticker: dict[str, Decimal | None] = {}
    for t in tickers:
        latest = (
            await session.execute(
                select(PriceData.close)
                .where(PriceData.ticker == t)
                .order_by(PriceData.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        price_by_ticker[t] = latest

    return [
        PredictionRow(
            ticker=r.ticker,
            signal_type=r.signal_type,
            confidence=r.confidence,
            feature_vector=r.feature_vector or {},
            created_at=r.created_at,
            latest_price=price_by_ticker.get(r.ticker),
            float_shares=float_by_ticker.get(r.ticker),
        )
        for r in prediction_rows
    ]


async def fetch_week_predictions(session) -> list[PredictionRow]:
    from sqlalchemy import select
    from data.models.prediction import Prediction

    cutoff = datetime.now(timezone.utc) - timedelta(days=PATTERN_WINDOW_DAYS)
    stmt = (
        select(Prediction)
        .where(Prediction.created_at > cutoff)
        .order_by(Prediction.created_at.asc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return await _enrich_predictions(session, rows)


async def fetch_new_predictions(session) -> list[PredictionRow]:
    from sqlalchemy import select
    from data.models.prediction import Prediction

    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEW_PREDICTION_LOOKBACK_HOURS)
    stmt = (
        select(Prediction)
        .where(Prediction.created_at > cutoff)
        .order_by(Prediction.confidence.desc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return await _enrich_predictions(session, rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def assemble_brief() -> str:
    from config import constants
    from data.db import get_session

    today = datetime.now(timezone.utc).date()
    schema_version = constants.FEATURE_SCHEMA_VERSION

    async with get_session() as session:
        outcomes = await fetch_resolved_outcomes(session)
        week_predictions = await fetch_week_predictions(session)
        new_predictions = await fetch_new_predictions(session)

    return build_message(
        today, schema_version, outcomes, week_predictions, new_predictions
    )


async def main() -> int:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        missing = [
            name for name, val in (
                ("TELEGRAM_BOT_TOKEN", token),
                ("TELEGRAM_CHAT_ID", chat_id),
            ) if not val
        ]
        print(
            f"pre_market_brief: missing required env var(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    try:
        message = await assemble_brief()
        for chunk in split_for_telegram(message):
            send_telegram(token, chat_id, chunk)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    # Count for the success log line
    new_count = message.count("\n   weights: ")
    # Calibration line tells us roughly how many resolved outcomes
    # — extract the leading 'last N' if present.
    print(
        f"Brief sent at {datetime.now(timezone.utc).isoformat()}: "
        f"{new_count} predictions in body."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
