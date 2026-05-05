"""Real-filing replay test for the FV-v2 scorer rewire (PR #31).

The pre-fix predictions ARTL, TVRD, KIDZ all landed at confidence=0.0000
because the rules-v1 scorer's keys had zero overlap with FEATURE_KEYS_FV_V1.
PR #31 rewired the scorer to FV-v2 vocabulary; the integration test in
that PR uses a synthetic "all flags True" fixture and only proves the
wiring works, not that real EDGAR filings produce non-zero scores.

This test is the stronger version: it loads JSON snapshots of the actual
sec_filings rows that produced ARTL/TVRD/KIDZ from production, runs them
through the new FV-v2 extractor + scorer, and asserts EXACT confidence
values plus per-key firing patterns.

Validation scope and limits
---------------------------
All three production fixtures are pure S-3 effective filings with no
other signals — no IR firm, no resolved underwriter, no Form 4, no
reverse-split text, no promoter graph hits. FV-v2 scores all three at
exactly 0.20 (only `edgar_priority_form` fires; the other five weights
are False given fixture content).

What this validates: the FV-v2 rewire correctly produces non-zero
confidence on the three filings that broke under FV-v1. The bug from
PR #30 is empirically fixed end-to-end on real production data.

What this does NOT validate: five of six FV-v2 weights are not exercised
by these fixtures. Multi-signal coverage (an 8-K with material item
numbers, a Form 4 P-code, an S-3 with an IR firm in the promoter graph)
is queued as a follow-up.

s3_effective NOT a separate weight
----------------------------------
The fixtures all have s3_effective=true. FV-v2 emits is_s3_effective in
the feature dict (carried over from FV-v1), but the rules-v1 scorer
DOES NOT read it as a weight key — only `edgar_priority_form`. If
is_s3_effective ever becomes a separate weight, expected_confidence
below changes; the test pins this contract explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import constants
from signals.features.edgar_features import (
    FEATURE_KEYS_FV_V2,
    extract_edgar_features,
)
from signals.scoring.catalyst_scorer import RulesV1Scorer, _RULES_V1_WEIGHTS


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "replay"


def _load_fixtures() -> list[tuple[str, dict]]:
    """Return [(label, snapshot_dict)] for every JSON fixture present."""
    if not FIXTURE_DIR.exists():
        return []
    out: list[tuple[str, dict]] = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        with open(path) as f:
            data = json.load(f)
        out.append((path.stem, data))
    return out


_FIXTURES = _load_fixtures()


# ---------------------------------------------------------------------------
# Contract pin: is_s3_effective is NOT a rules-v1 weight. The expected
# confidence calculation below assumes only edgar_priority_form fires
# for a pure-S-3 filing. If this assertion ever fails, that calculation
# is wrong — surface BEFORE the per-fixture assertion fails.
# ---------------------------------------------------------------------------
def test_is_s3_effective_is_not_a_rules_v1_weight():
    """Pin the contract: FV-v2 emits is_s3_effective in the feature dict
    (carried over from FV-v1) but the rules-v1 scorer reads it as
    information, not as a weight key. If this changes, the expected
    confidence for the pure-S-3 fixtures changes and this test must
    be updated alongside the weight change."""
    assert "is_s3_effective" not in _RULES_V1_WEIGHTS
    assert "s3_effective" not in _RULES_V1_WEIGHTS


# ---------------------------------------------------------------------------
# Pure-S-3 expected firing pattern. All three production fixtures match
# this profile (none has an IR firm, none has an underwriter, none has
# Form 4 fields populated, full_text is empty).
# ---------------------------------------------------------------------------
_EXPECTED_FIRING_PURE_S3: dict[str, bool] = {
    "edgar_priority_form": True,    # S-3 ∈ EDGAR_PRIORITY_FORMS  (fires 0.20)
    "ir_firm_engaged": False,
    "ir_firm_known_promoter": False,
    "underwriter_flagged": False,
    "reverse_split": False,
    "is_form4_buy": False,
}
_EXPECTED_CONFIDENCE_PURE_S3 = 0.20


@pytest.mark.skipif(
    not _FIXTURES,
    reason="No replay fixtures present. Run scripts/snapshot_replay_fixtures.py "
           "on the droplet to generate tests/fixtures/replay/*.json.",
)
@pytest.mark.parametrize(
    "label,snapshot",
    _FIXTURES,
    ids=[label for label, _ in _FIXTURES] if _FIXTURES else None,
)
def test_real_filing_replay_scores_exactly_0_20_under_fv_v2(label, snapshot):
    """For each real S-3 filing that produced a fv-v1 confidence=0
    prediction, the FV-v2 scorer must produce confidence == 0.20 with
    only `edgar_priority_form` firing.

    Strict per-key assertion: any deviation from the expected firing
    pattern fails the test loudly. Strict confidence assertion: 0.20
    exactly (within float tolerance). Both contracts protect future
    weight changes from going unnoticed.
    """
    filing = snapshot["filing"]
    ticker_metadata = snapshot["ticker_metadata"]

    features = extract_edgar_features(filing, ticker_metadata)
    result = RulesV1Scorer().score(features)

    # FV-v2 schema-version pin.
    assert result.feature_schema_version == constants.FEATURE_SCHEMA_VERSION
    assert result.feature_schema_version == "fv-v2"

    # Per-key firing pattern (strict): every weight key must match the
    # expected pure-S-3 profile.
    for weight_key, want in _EXPECTED_FIRING_PURE_S3.items():
        got = features[weight_key]
        assert got == want, (
            f"[{label}] expected {weight_key}={want} for a pure-S-3 "
            f"filing; extractor returned {got!r}. "
            "If this assertion fails, either the extractor changed "
            "(check the FV-v2 derivation logic for this key) or the "
            "fixture content drifted (re-snapshot). Do not relax this "
            "assertion silently."
        )

    # Strict confidence: exactly 0.20.
    assert result.probability == pytest.approx(_EXPECTED_CONFIDENCE_PURE_S3), (
        f"[{label}] expected confidence={_EXPECTED_CONFIDENCE_PURE_S3} "
        f"(only edgar_priority_form fires at weight 0.20); got "
        f"{result.probability}. If this fails, either a weight value "
        "changed (verify against design doc) or another weight key "
        "started firing for these fixtures (verify firing pattern "
        "above). Do not relax this assertion silently."
    )

    # Sanity: the snapshot captured the pre-fix confidence_v1 == 0.0
    # baseline. If this fails, the snapshot is stale or wrong.
    assert snapshot["prediction_confidence_v1"] == pytest.approx(0.0, abs=1e-6), (
        f"[{label}] expected prediction_confidence_v1==0.0 (the bug this "
        f"test validates the fix for); fixture has "
        f"{snapshot['prediction_confidence_v1']}"
    )


@pytest.mark.skipif(
    not _FIXTURES,
    reason="No replay fixtures present.",
)
def test_replay_fixtures_cover_all_three_pre_fix_predictions():
    """The fixture set covers ARTL, TVRD, and KIDZ. If a snapshot is
    missing, the bug-fix verification is incomplete."""
    tickers_present = {snap["prediction_ticker"] for _, snap in _FIXTURES}
    expected = {"ARTL", "TVRD", "KIDZ"}
    missing = expected - tickers_present
    assert not missing, (
        f"missing fixtures for: {missing}. Run "
        "scripts/snapshot_replay_fixtures.py on the droplet to regenerate."
    )


@pytest.mark.skipif(
    not _FIXTURES,
    reason="No replay fixtures present.",
)
def test_every_replay_fixture_has_all_fv_v2_input_fields():
    """Each fixture must contain every field the FV-v2 extractor reads."""
    expected_filing_keys = {
        "form_type", "ir_firm_mentioned", "s3_effective",
        "form4_insider_buy", "form4_transaction_code",
        "underwriter_id", "full_text", "item_numbers",
    }
    expected_meta_keys = {
        "exchange", "float_shares", "promoter_match_count",
        "ir_firm_known_promoter", "underwriter_flagged",
    }
    for label, snap in _FIXTURES:
        missing_filing = expected_filing_keys - set(snap["filing"].keys())
        missing_meta = expected_meta_keys - set(snap["ticker_metadata"].keys())
        assert not missing_filing, (
            f"[{label}] fixture missing filing fields: {missing_filing}"
        )
        assert not missing_meta, (
            f"[{label}] fixture missing ticker_metadata fields: {missing_meta}"
        )
