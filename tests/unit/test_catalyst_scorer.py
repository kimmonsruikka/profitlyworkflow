from __future__ import annotations

from decimal import Decimal

import pytest

from config import constants
from signals.scoring.catalyst_scorer import (
    CatalystScorer,
    RulesV1Scorer,
    ScoreResult,
)


# ---------------------------------------------------------------------------
# RulesV1Scorer — output shape + invariants
# ---------------------------------------------------------------------------
def test_score_result_probability_is_in_unit_interval_for_no_features():
    scorer = RulesV1Scorer()
    out = scorer.score({})
    assert 0.0 <= out.probability <= 1.0
    assert out.probability == 0.0


def test_score_result_probability_is_in_unit_interval_for_all_features():
    """All FV-v2 weight keys set True. The two pre-PR-#31 inert weights
    (social_velocity_spike, short_interest_high) were dropped — see the
    rewire design doc Section C."""
    scorer = RulesV1Scorer()
    inputs = {
        "edgar_priority_form": True,
        "ir_firm_engaged": True,
        "ir_firm_known_promoter": True,
        "underwriter_flagged": True,
        "reverse_split": True,
        "is_form4_buy": True,
    }
    out = scorer.score(inputs)
    assert 0.0 <= out.probability <= 1.0
    # Sum of remaining weights = 0.85; clamp doesn't kick in.
    assert out.probability == pytest.approx(0.85)


def test_score_result_clamps_to_one_when_weights_overshoot():
    """Post-PR-#31 the placeholder weights total 0.85. A future weight
    bump or bad config could overshoot; the scorer must still clamp."""
    from signals.scoring import catalyst_scorer as mod

    original = dict(mod._RULES_V1_WEIGHTS)
    try:
        mod._RULES_V1_WEIGHTS["edgar_priority_form"] = 5.0  # absurd
        scorer = RulesV1Scorer()
        out = scorer.score({"edgar_priority_form": True})
        assert out.probability == 1.0
    finally:
        mod._RULES_V1_WEIGHTS.clear()
        mod._RULES_V1_WEIGHTS.update(original)


def test_score_result_includes_version_strings():
    scorer = RulesV1Scorer()
    out = scorer.score({})
    assert out.scorer_version == "rules-v1"
    assert out.feature_schema_version == constants.FEATURE_SCHEMA_VERSION


def test_score_result_echoes_input_features_in_feature_vector():
    """The ScoreResult.feature_vector is what gets persisted; it must
    capture exactly what the scorer saw plus its weights."""
    scorer = RulesV1Scorer()
    inputs = {
        "edgar_priority_form": True,
        "ir_firm_engaged": False,
        "extra_caller_provided_field": "promoter-X",  # extras must survive
    }
    out = scorer.score(inputs)
    assert out.feature_vector["inputs"] == inputs
    assert "weights" in out.feature_vector
    assert "edgar_priority_form" in out.feature_vector["weights"]


def test_confidence_decimal_rounds_to_4_places_for_db_persistence():
    """confidence column is NUMERIC(5,4) — the helper must round there."""
    scorer = RulesV1Scorer()
    out = scorer.score({"edgar_priority_form": True})
    assert isinstance(out.confidence_decimal, Decimal)
    # Decimal precision check — value should have at most 4 decimal places
    assert -out.confidence_decimal.as_tuple().exponent <= 4


def test_score_handles_truthy_falsy_values_consistently():
    """Features may arrive as 0/1 ints, bools, or even strings — the
    scorer should treat any truthy value as 'feature present'."""
    scorer = RulesV1Scorer()
    a = scorer.score({"edgar_priority_form": 1})
    b = scorer.score({"edgar_priority_form": True})
    assert a.probability == b.probability


def test_score_nan_input_treated_as_zero_via_clamp():
    """NaN input shouldn't propagate into the probability."""
    from signals.scoring import catalyst_scorer as mod

    original = dict(mod._RULES_V1_WEIGHTS)
    try:
        mod._RULES_V1_WEIGHTS["edgar_priority_form"] = float("nan")
        scorer = RulesV1Scorer()
        out = scorer.score({"edgar_priority_form": True})
        # _clamp_unit returns 0.0 for NaN
        assert out.probability == 0.0
    finally:
        mod._RULES_V1_WEIGHTS.clear()
        mod._RULES_V1_WEIGHTS.update(original)


# ---------------------------------------------------------------------------
# Abstract base contract — subclasses must implement score()
# ---------------------------------------------------------------------------
def test_abstract_catalyst_scorer_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        CatalystScorer()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# uncalibrated_warning — rules-v1 is uncalibrated by design
# ---------------------------------------------------------------------------
def test_rules_v1_emits_uncalibrated_warning_true():
    """rules-v1 is a heuristic. The flag stays True until a successor
    scorer passes calibration validation (Brier / ECE)."""
    scorer = RulesV1Scorer()
    out = scorer.score({"edgar_priority_form": True})
    assert out.uncalibrated_warning is True


def test_score_result_uncalibrated_warning_defaults_to_true():
    """Safety default — a future scorer that forgets to pass the flag is
    treated as uncalibrated (the safer assumption) until proven otherwise."""
    result = ScoreResult(
        probability=0.7,
        scorer_version="hypothetical-future",
        feature_schema_version="fv-v1",
    )
    assert result.uncalibrated_warning is True


def test_score_result_can_be_marked_calibrated_explicitly():
    """A calibration-validated scorer opts out by passing False."""
    result = ScoreResult(
        probability=0.7,
        scorer_version="gbdt-v3-calibrated",
        feature_schema_version="fv-v2",
        uncalibrated_warning=False,
    )
    assert result.uncalibrated_warning is False
