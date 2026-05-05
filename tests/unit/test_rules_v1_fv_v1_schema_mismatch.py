"""Regression tests for the rules-v1 / FV vocabulary contract.

Originally pinned the PR #30 bug where the scorer's weight keys had
zero overlap with `FEATURE_KEYS_FV_V1` and every prediction landed at
confidence=0.0. PR #31 rewires the scorer to FV-v2 vocabulary.

Post-PR-#31:
  - The "zero overlap" assertion flips to "FV-v2 ⊇ all scorer keys"
  - The two strict-xfails un-xfail and pin the post-fix happy path
"""

from __future__ import annotations

import pytest

from signals.features.edgar_features import (
    FEATURE_KEYS_FV_V1,
    FEATURE_KEYS_FV_V2,
    extract_edgar_features,
)
from signals.scoring.catalyst_scorer import RulesV1Scorer, _RULES_V1_WEIGHTS


# ---------------------------------------------------------------------------
# Post-PR-#31 contract: every weight key is in FV-v2. Replaces the original
# "zero overlap = bug" assertion with the positive form.
# ---------------------------------------------------------------------------
def test_every_scorer_weight_key_is_in_fv_v2():
    """The fundamental fix from PR #31. Without this, every prediction
    silently scores 0.0 — that was the production bug. If this test ever
    fails, it means the scorer and extractor have drifted apart again."""
    extractor_keys = set(FEATURE_KEYS_FV_V2)
    weight_keys = set(_RULES_V1_WEIGHTS.keys())
    missing = weight_keys - extractor_keys
    assert missing == set(), (
        f"scorer weights reference keys the FV-v2 extractor doesn't emit: "
        f"{missing}. Either add the key to extract_edgar_features() and "
        "FEATURE_KEYS_FV_V2 (bumping FEATURE_SCHEMA_VERSION), or remove "
        "the weight from _RULES_V1_WEIGHTS."
    )


def test_fv_v2_is_superset_of_fv_v1():
    """FV-v2 keeps all FV-v1 keys (with is_form4_buy semantics narrowed
    per the schema bump). Old fv-v1 predictions remain query-able under
    their version pin."""
    assert set(FEATURE_KEYS_FV_V1).issubset(set(FEATURE_KEYS_FV_V2))


# ---------------------------------------------------------------------------
# The smoking gun, post-fix: end-to-end extractor → scorer emits non-zero
# for an obviously-loaded filing.
# ---------------------------------------------------------------------------
def test_extractor_to_scorer_produces_nonzero_for_obvious_dilution_signal():
    """An S-3 effective filing with a manipulation-flagged underwriter,
    on a priority form. Should clearly score non-zero. Pre-PR-#31 this
    was xfail and produced 0.0; post-PR-#31 it must produce >0."""
    filing = {
        "form_type": "S-3",
        "s3_effective": True,
        "item_numbers": [],
        "form4_insider_buy": False,
        "ir_firm_mentioned": "Acme IR LLC",
        "underwriter_id": "11111111-1111-1111-1111-111111111111",
        "full_text": {},
    }
    ticker_meta = {
        "ticker": "ARTL",
        "exchange": "NASDAQ-CM",
        "promoter_match_count": 2,
        "promoter_match_reliability_scores": [0.7, 0.5],
        "float_shares": 5_000_000,
        "ir_firm_known_promoter": True,
        "underwriter_flagged": True,
    }
    features = extract_edgar_features(filing, ticker_meta)
    out = RulesV1Scorer().score(features)
    assert out.probability > 0.0, (
        f"expected non-zero score for S-3 effective + IR firm in promoter "
        f"graph + flagged underwriter; got {out.probability}"
    )


def test_form4_buy_signal_reaches_scorer_with_explicit_p_code():
    """FV-v2 narrows is_form4_buy to P-code only (open-market purchase).
    The extractor reads `form4_transaction_code` explicitly — the legacy
    `form4_insider_buy` boolean fallback is removed in v2 to keep the
    P-only contract honest."""
    filing = {
        "form_type": "4",
        "form4_insider_buy": True,  # Boolean alone is no longer enough
        "form4_transaction_code": "P",  # Explicit P code — this is what fires
        "item_numbers": [],
        "full_text": {},
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
        f"expected non-zero score for Form 4 P-code buy; got {out.probability}"
    )


def test_form4_buy_a_code_does_not_fire_in_fv_v2():
    """FV-v2 narrowing: A-code (grant/award) should NOT fire is_form4_buy.
    This is the calibration-intent change the schema bump exists to permit.
    FV-v1 accepted P+A; FV-v2 is P-only."""
    filing = {
        "form_type": "4",
        "form4_insider_buy": True,
        "form4_transaction_code": "A",
        "item_numbers": [],
        "full_text": {},
    }
    ticker_meta = {
        "ticker": "X",
        "exchange": "OTC",
        "promoter_match_count": 0,
    }
    features = extract_edgar_features(filing, ticker_meta)
    assert features["is_form4_buy"] is False
    # The Form-4 weight contributes 0.0 — but `form_type='4'` IS in
    # EDGAR_PRIORITY_FORMS, so edgar_priority_form=True and the score is
    # non-zero (just from that one signal, not from form-4).
    assert features["edgar_priority_form"] is True
