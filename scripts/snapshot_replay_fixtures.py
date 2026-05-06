"""One-shot snapshot script: dump the three pre-fix fv-v1 prediction filings
to JSON fixtures for the FV-v2 replay test.

For each prediction ID listed in PREDICTION_IDS, this script:
  1. Loads the predictions row to get its filing_id
  2. Loads the sec_filings row by filing_id
  3. Computes ticker_metadata exactly the way _build_signal_payload does
     (so the fixture captures both the parser-level state AND the
     caller-resolved narrow lookups: ir_firm_known_promoter and
     underwriter_flagged)
  4. Writes a JSON file to tests/fixtures/replay/{ticker}_{filing_id_short}.json

Usage on the droplet:

    sudo -u trading bash -c '
        set -a; source /app/profitlyworkflow/.env.production; set +a
        cd /app/profitlyworkflow
        ./venv/bin/python scripts/snapshot_replay_fixtures.py
    '

The script writes JSON to tests/fixtures/replay/. Copy those files
back to the dev environment (or commit on the droplet) so the test
fixture set is complete.

NO production data mutation. Read-only. Safe to run anytime.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env.production"
if env_file.exists():
    load_dotenv(env_file, override=True)


# Prediction IDs (any unique prefix is enough — the script LIKE-matches).
# These three are the pre-FV-v2 confidence=0.0000 predictions identified
# in PR #30.
PREDICTION_IDS = [
    "d2a56bbd",  # ARTL — S2_DILUTION_RISK
    "5f4b799f",  # TVRD — S2_DILUTION_RISK
    "bcc69aa4",  # KIDZ — S2_DILUTION_RISK
]

OUT_DIR = ROOT / "tests" / "fixtures" / "replay"


def _json_safe(obj):
    """Convert non-JSON-serializable types to JSON-friendly forms."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"don't know how to serialize {type(obj).__name__}")


async def snapshot_one(prediction_prefix: str) -> dict | None:
    """Pull filing + ticker_metadata for one prediction. Returns None on miss."""
    from sqlalchemy import func as _func
    from sqlalchemy import select

    from data.db import get_session
    from data.models.prediction import Prediction
    from data.models.promoter_entity import PromoterEntity
    from data.models.sec_filing import SecFiling
    from data.models.ticker import Ticker
    from data.models.underwriter import Underwriter

    async with get_session() as session:
        # 1) Find the prediction by prediction_id prefix.
        pred = (
            await session.execute(
                select(Prediction).where(
                    _func.cast(Prediction.prediction_id, type_=__import__("sqlalchemy").Text)
                    .like(f"{prediction_prefix}%")
                )
            )
        ).scalar_one_or_none()
        if pred is None:
            print(f"[skip] no prediction with prefix {prediction_prefix}")
            return None

        # 2) The prediction may not directly carry a filing_id — pull by
        # ticker + signal_type + the closest filing in time. Adjust this
        # if the predictions schema joins via a different column.
        filing = None
        if hasattr(pred, "filing_id") and getattr(pred, "filing_id", None):
            filing = (
                await session.execute(
                    select(SecFiling).where(SecFiling.filing_id == pred.filing_id)
                )
            ).scalar_one_or_none()
        if filing is None:
            # Fallback: pick the most recent processed filing for this
            # ticker that pre-dates the prediction's created_at.
            filing = (
                await session.execute(
                    select(SecFiling)
                    .where(SecFiling.ticker == pred.ticker)
                    .where(SecFiling.processed.is_(True))
                    .where(SecFiling.created_at <= pred.created_at)
                    .order_by(SecFiling.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if filing is None:
            print(f"[skip] couldn't find filing for prediction {prediction_prefix}")
            return None

        # 3) Look up ticker row.
        ticker_row = (
            await session.execute(
                select(Ticker).where(Ticker.ticker == filing.ticker)
            )
        ).scalar_one_or_none()

        # 4) Compute caller-resolved narrow lookups (mirrors _build_signal_payload).
        ir_firm = filing.ir_firm_mentioned
        ir_firm_normalized = ir_firm.strip().lower() if ir_firm else None

        ir_firm_known_promoter = False
        if ir_firm_normalized:
            ir_firm_known_promoter = bool(
                (
                    await session.execute(
                        select(_func.count())
                        .select_from(PromoterEntity)
                        .where(_func.lower(PromoterEntity.name) == ir_firm_normalized)
                        .where(PromoterEntity.type == "ir_firm")
                    )
                ).scalar_one()
            )

        promoter_match_count = 0
        if ir_firm_normalized:
            promoter_match_count = (
                await session.execute(
                    select(_func.count())
                    .select_from(PromoterEntity)
                    .where(_func.lower(PromoterEntity.name) == ir_firm_normalized)
                )
            ).scalar_one() or 0

        if filing.underwriter_id:
            promoter_match_count += 1

        underwriter_flagged = False
        if filing.underwriter_id:
            underwriter_flagged = bool(
                (
                    await session.execute(
                        select(Underwriter.manipulation_flagged)
                        .where(Underwriter.underwriter_id == filing.underwriter_id)
                    )
                ).scalar_one_or_none()
            )

        return {
            "prediction_id": str(pred.prediction_id),
            "prediction_ticker": pred.ticker,
            "prediction_signal_type": pred.signal_type,
            "prediction_confidence_v1": float(pred.confidence) if pred.confidence is not None else None,
            "filing": {
                "filing_id": str(filing.filing_id),
                "ticker": filing.ticker,
                "cik": filing.cik,
                "form_type": filing.form_type,
                "accession_number": filing.accession_number,
                "filed_at": filing.filed_at,
                "item_numbers": filing.item_numbers or [],
                "ir_firm_mentioned": filing.ir_firm_mentioned,
                "s3_effective": bool(filing.s3_effective),
                "form4_insider_buy": bool(filing.form4_insider_buy),
                # Not currently populated by the parser — captured for completeness.
                "form4_transaction_code": None,
                "form4_value_usd": None,
                "underwriter_id": str(filing.underwriter_id) if filing.underwriter_id else None,
                "full_text": filing.full_text or {},
                "processed": bool(filing.processed),
                "created_at": filing.created_at,
            },
            "ticker_metadata": {
                "ticker": filing.ticker,
                "exchange": ticker_row.exchange if ticker_row else None,
                "float_shares": ticker_row.float_shares if ticker_row else None,
                "market_cap_usd": None,
                "promoter_match_count": int(promoter_match_count),
                "promoter_match_reliability_scores": [],
                "days_since_last_filing": None,
                "days_since_last_promoter_filing": None,
                "ir_firm_known_promoter": ir_firm_known_promoter,
                "underwriter_flagged": underwriter_flagged,
            },
        }


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0

    for prefix in PREDICTION_IDS:
        snapshot = await snapshot_one(prefix)
        if snapshot is None:
            continue

        ticker = snapshot["prediction_ticker"] or "UNKNOWN"
        filing_short = snapshot["filing"]["filing_id"][:8]
        out_path = OUT_DIR / f"{ticker}_{filing_short}.json"
        with open(out_path, "w") as f:
            json.dump(snapshot, f, indent=2, default=_json_safe)
        print(f"[ok] {prefix} -> {out_path}")
        written += 1

    print(f"\nDone. {written} fixture(s) written to {OUT_DIR}")
    return 0 if written == len(PREDICTION_IDS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
