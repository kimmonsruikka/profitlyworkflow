"""Integration test for the PR #31 FV-v2 fix.

End-to-end path: extract_edgar_features → RulesV1Scorer.score →
PredictionsRepository.create. Asserts the prediction row's
confidence is non-zero on a known-strong filing fixture, with the
feature_schema_version pinned to 'fv-v2'.

This is the regression test the production observation needed —
all three pre-fix predictions (ARTL, TVRD, KIDZ) had confidence=0
because the scorer's keys didn't match the extractor's. Post-PR-#31
this end-to-end path must produce a non-zero score.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import constants
from signals.engine import SignalEngine
from signals.features.edgar_features import extract_edgar_features
from signals.scoring.catalyst_scorer import RulesV1Scorer


def _mock_session():
    """Mock session that fakes server-default population on refresh."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    fake_pid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async def refresh(row):
        row.prediction_id = fake_pid
        row.created_at = now

    session.refresh = AsyncMock(side_effect=refresh)
    return session


# ---------------------------------------------------------------------------
# Layer 1: pure extractor → scorer. Confidence must be > 0 for a
# known-strong filing.
# ---------------------------------------------------------------------------
def test_known_strong_filing_scores_non_zero_via_extractor_and_scorer():
    """The fixture: an S-3 effective filing on a NASDAQ-CM ticker, with
    an IR firm that's a known type='ir_firm' promoter, and a
    manipulation-flagged underwriter. Six FV-v2 weights should fire:
      - edgar_priority_form (S-3 ∈ EDGAR_PRIORITY_FORMS)
      - is_s3_effective (already a FV-v1 key, weight unchanged)
      - ir_firm_engaged
      - ir_firm_known_promoter (caller-resolved)
      - underwriter_flagged (caller-resolved)
    Maximum attainable from this fixture: 0.20 + 0.15 + 0.20 + 0.15
    = 0.70 (note: is_s3_effective is in FV-v2 but not in
    _RULES_V1_WEIGHTS — it's a feature available to future scorers
    but rules-v1 reads only the explicit weight keys).
    """
    filing = {
        "form_type": "S-3",
        "s3_effective": True,
        "item_numbers": [],
        "form4_insider_buy": False,
        "form4_transaction_code": None,
        "ir_firm_mentioned": "Acme IR LLC",
        "underwriter_id": "11111111-1111-1111-1111-111111111111",
        "full_text": {},
    }
    ticker_meta = {
        "ticker": "FIXTURE",
        "exchange": "NASDAQ-CM",
        "float_shares": 5_000_000,
        "market_cap_usd": 25_000_000.0,
        "promoter_match_count": 1,
        "promoter_match_reliability_scores": [0.7],
        "days_since_last_filing": 14,
        "days_since_last_promoter_filing": 60,
        "ir_firm_known_promoter": True,
        "underwriter_flagged": True,
    }

    features = extract_edgar_features(filing, ticker_meta)

    # Feature flags the rules-v1 scorer cares about — verify they are
    # all True before scoring so the assertion failure mode below is
    # diagnostic.
    assert features["edgar_priority_form"] is True
    assert features["ir_firm_engaged"] is True
    assert features["ir_firm_known_promoter"] is True
    assert features["underwriter_flagged"] is True

    out = RulesV1Scorer().score(features)
    # 0.20 (priority) + 0.15 (ir_engaged) + 0.20 (ir_known_promoter)
    # + 0.15 (underwriter_flagged) = 0.70
    assert out.probability == pytest.approx(0.70)
    assert out.confidence_decimal > 0
    # Schema-version pin so we don't accidentally regress the bump.
    assert out.feature_schema_version == "fv-v2"


# ---------------------------------------------------------------------------
# Layer 2: SignalEngine.evaluate_edgar_filing → PredictionsRepository.create.
# Confirm the non-zero confidence flows all the way to the persisted row.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_evaluate_edgar_filing_persists_nonzero_confidence_for_strong_signal():
    """The user-visible fix. Pre-PR-#31 every persisted prediction had
    confidence=0.0000 because of the schema mismatch. Post-PR-#31 a
    known-strong filing must produce a row whose confidence > 0."""
    session = _mock_session()
    engine = SignalEngine(scorer=RulesV1Scorer(), session=session)

    filing = {
        "ticker": "FIXTURE",
        "form_type": "S-3",
        "cik": "0001234567",
        "accession_number": "0001234567-26-000099",
        "item_numbers": [],
        "s3_effective": True,
        "form4_insider_buy": False,
        "form4_value_usd": None,
        "form4_transaction_code": None,
        "ir_firm_mentioned": "Acme IR LLC",
        "underwriter_id": "11111111-1111-1111-1111-111111111111",
        "full_text": {},
    }
    ticker_meta = {
        "ticker": "FIXTURE",
        "exchange": "NASDAQ-CM",
        "float_shares": 5_000_000,
        "market_cap_usd": 25_000_000.0,
        "promoter_match_count": 1,
        "promoter_match_reliability_scores": [0.7],
        "days_since_last_filing": 14,
        "days_since_last_promoter_filing": 60,
        "ir_firm_known_promoter": True,
        "underwriter_flagged": True,
    }

    prediction = await engine.evaluate_edgar_filing(filing, ticker_meta)

    assert prediction is not None
    assert prediction.confidence > 0, (
        f"expected non-zero confidence on the persisted row; got "
        f"{prediction.confidence}. This is the production-bug regression: "
        "extractor and scorer key vocabularies must overlap."
    )
    assert prediction.feature_schema_version == "fv-v2"


# ---------------------------------------------------------------------------
# Constant-pin: FEATURE_SCHEMA_VERSION moved to fv-v2 in this PR.
# ---------------------------------------------------------------------------
def test_feature_schema_version_is_fv_v2():
    """The version bump is the contract that lets fv-v1 predictions be
    excluded from Phase 1b calibration analysis via WHERE clause."""
    assert constants.FEATURE_SCHEMA_VERSION == "fv-v2"
