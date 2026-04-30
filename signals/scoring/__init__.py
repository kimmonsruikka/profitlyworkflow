"""Catalyst scoring layer.

Public surface:
    CatalystScorer        — abstract base
    ScoreResult           — the value every scorer returns
    RulesV1Scorer         — Phase 1 hand-coded scorer (the in-production one)
"""

from signals.scoring.catalyst_scorer import (
    CatalystScorer,
    RulesV1Scorer,
    ScoreResult,
)

__all__ = ["CatalystScorer", "RulesV1Scorer", "ScoreResult"]
