"""Regression tests pinning the rules-v1 / FV-v1 schema mismatch bug.

Bug: signals/scoring/catalyst_scorer.py:RulesV1Scorer reads features
keyed by an OLD vocabulary (`edgar_priority_form`, `ir_firm_engagement`,
`ir_firm_known_promoter`, `underwriter_flagged`, `reverse_split_announced`,
`form4_insider_buy`, `social_velocity_spike`, `short_interest_high`)
that doesn't match what signals/features/edgar_features.py:extract_edgar_features
actually produces (FV-v1: `is_s3_effective`, `has_known_promoter_match`,
`is_form4_buy`, `issuer_is_otc`, etc.). Zero key overlap. So
features.get(name) returns None for every weight key, total stays at 0,
and every prediction's confidence is 0.0.

These tests document the contract that the fix PR must satisfy. They
are XFAIL today and will START PASSING when the scorer is fixed —
that's the signal to remove the xfail markers.
"""

from __future__ import annotations

import pytest

from signals.features.edgar_features import (
    FEATURE_KEYS_FV_V1,
    extract_edgar_features,
)
from signals.scoring.catalyst_scorer import RulesV1Scorer, _RULES_V1_WEIGHTS


# ---------------------------------------------------------------------------
# Diagnosis: the two key vocabularies have ZERO intersection. This isn't
# xfailed — it's the literal current state. When the fix lands, the
# overlap will become non-empty and this test will need updating to
# reflect the new contract.
# ---------------------------------------------------------------------------
def test_diagnosis_extractor_and_scorer_share_zero_keys():
    """Pin the schema-mismatch finding from PR #30 investigation.

    When the fix PR rewires the scorer to read FV-v1 keys (or wires a
    bridge), this assertion will need to flip — that's the deliberate
    signal that the diagnosis has been addressed.
    """
    extractor_keys = set(FEATURE_KEYS_FV_V1)
    weight_keys = set(_RULES_V1_WEIGHTS.keys())
    overlap = extractor_keys & weight_keys
    assert overlap == set(), (
        f"expected empty overlap (current bug state); got {overlap}. "
        "If this fails, the fix has landed — flip this test to pin the new contract."
    )


# ---------------------------------------------------------------------------
# The smoking gun: end-to-end extractor → scorer always emits 0.0.
# Marked xfail so CI still passes; will start passing after the fix PR
# at which point the xfail marker should be removed.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(
    reason="PR #30 investigation: scorer reads keys the extractor doesn't emit. "
           "Will pass once scorer is rewired to FV-v1 vocabulary.",
    strict=True,
)
def test_extractor_to_scorer_produces_nonzero_for_obvious_dilution_signal():
    """An S-3 effective filing with a known promoter-network match should
    NOT score 0. Today it does, because the scorer's keys don't match
    the extractor's. This is the user-visible symptom: every prediction
    in production landed at confidence=0.0000."""
    filing = {
        "form_type": "S-3",
        "s3_effective": True,
        "item_numbers": [],
        "form4_insider_buy": False,
    }
    ticker_meta = {
        "ticker": "ARTL",
        "exchange": "NASDAQ-CM",
        "promoter_match_count": 2,
        "promoter_match_reliability_scores": [0.7, 0.5],
        "float_shares": 5_000_000,
    }
    features = extract_edgar_features(filing, ticker_meta)
    out = RulesV1Scorer().score(features)
    # Today: probability is 0.0 — the xfail above absorbs that. After
    # the fix, probability should be non-zero for this clearly-loaded
    # input (S-3 effective + 2 promoter matches).
    assert out.probability > 0.0, (
        f"expected non-zero score for S-3 effective + promoter match; got {out.probability}"
    )


@pytest.mark.xfail(
    reason="PR #30 investigation: scorer's form4_insider_buy weight key "
           "doesn't match extractor's is_form4_buy. Will pass after fix.",
    strict=True,
)
def test_form4_buy_signal_reaches_scorer():
    """Even where the names ALMOST line up, they don't quite —
    extractor emits `is_form4_buy`; scorer expects `form4_insider_buy`.
    A buy-side Form 4 should produce a non-zero score; today it
    produces 0."""
    filing = {
        "form_type": "4",
        "form4_insider_buy": True,
        "item_numbers": [],
    }
    ticker_meta = {
        "ticker": "TVRD",
        "exchange": "OTC",
        "promoter_match_count": 0,
        "float_shares": 3_000_000,
    }
    features = extract_edgar_features(filing, ticker_meta)
    out = RulesV1Scorer().score(features)
    assert out.probability > 0.0, (
        f"expected non-zero score for Form 4 buy; got {out.probability}"
    )
