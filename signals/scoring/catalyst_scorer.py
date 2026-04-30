"""Catalyst scorer abstraction + the rules-v1 Phase-1 implementation.

Every scorer maps a feature dict → a calibrated probability in [0.0, 1.0]
plus the bookkeeping the predictions table needs (scorer_version,
feature_schema_version, feature_vector echo). The internal pipeline is
probability-native — convert to integer percent only at presentation
time in alert formatters.

Phase-1 weights are placeholders. They get calibrated empirically once
500–1000 prediction-outcome pairs have been collected; that's the
graduation trigger to start training a GBDT scorer in shadow mode.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from config import constants


def _clamp_unit(value: float) -> float:
    """Squash any number into [0.0, 1.0] for the calibrated-probability invariant."""
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True)
class ScoreResult:
    probability: float
    scorer_version: str
    feature_schema_version: str
    feature_vector: dict[str, Any] = field(default_factory=dict)

    @property
    def confidence_decimal(self) -> Decimal:
        """Persist-safe form for the predictions.confidence NUMERIC(5,4) column."""
        return Decimal(str(round(self.probability, 4)))


class CatalystScorer(abc.ABC):
    """Contract for every scorer that writes predictions.

    Subclasses set `version` and `feature_schema_version` class vars and
    implement score(features) → ScoreResult. The base class is responsible
    for nothing more — calibration, training, and feature engineering live
    above and below this interface.
    """

    version: str = ""
    feature_schema_version: str = constants.FEATURE_SCHEMA_VERSION

    @abc.abstractmethod
    def score(self, features: dict[str, Any]) -> ScoreResult: ...


# ---------------------------------------------------------------------------
# RulesV1Scorer — Phase-1 hand-coded weights.
# ---------------------------------------------------------------------------
# Placeholder weights. The whole point of the predictions / outcomes loop
# is to replace these with empirical values once we have the data. Don't
# tune these by hand — that defeats the calibration design.
_RULES_V1_WEIGHTS: dict[str, float] = {
    "edgar_priority_form": 0.20,        # filing matches EDGAR_PRIORITY_FORMS
    "ir_firm_engagement": 0.15,         # filing parser detected an IR firm
    "ir_firm_known_promoter": 0.20,     # the IR firm is in our promoter graph
    "underwriter_flagged": 0.15,        # known-flagged underwriter on an S-1/S-3
    "reverse_split_announced": 0.05,    # reverse split detected (mild signal)
    "form4_insider_buy": 0.10,          # Form 4 P-code transaction
    "social_velocity_spike": 0.10,      # Telegram/Reddit mention rate (Phase 1.5)
    "short_interest_high": 0.05,        # SI% > 20 (Phase 2 once Ortex wired)
}


class RulesV1Scorer(CatalystScorer):
    """Hand-coded scorer that sums weights for present feature flags.

    Inputs in `features` are expected to be booleans (or 0/1) keyed by the
    names in _RULES_V1_WEIGHTS. Missing keys default to 0. Output is
    clamped to [0.0, 1.0] so out-of-range weight changes can't produce
    invalid probabilities.
    """

    version: str = "rules-v1"

    def score(self, features: dict[str, Any]) -> ScoreResult:
        total = 0.0
        for feature_name, weight in _RULES_V1_WEIGHTS.items():
            if features.get(feature_name):
                total += weight
        prob = _clamp_unit(total)
        return ScoreResult(
            probability=prob,
            scorer_version=self.version,
            feature_schema_version=self.feature_schema_version,
            # Echo what the scorer actually saw. Inputs the scorer doesn't
            # know about (extras the caller passed) survive too — handy
            # for downstream feature-attribution work.
            feature_vector={"inputs": dict(features), "weights": dict(_RULES_V1_WEIGHTS)},
        )
