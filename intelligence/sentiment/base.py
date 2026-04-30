"""Sentiment classifier interfaces. Implementations land in later PRs."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal


SentimentLabel = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True)
class SentimentResult:
    label: SentimentLabel
    conviction: float                       # 0.0–1.0; calibrated like predictions.confidence
    pump_language_detected: bool
    unique_signals: dict[str, Any] = field(default_factory=dict)


class SentimentClassifier(abc.ABC):
    """Stateless batch classifier interface.

    Implementations must accept any iterable of strings and return one
    SentimentResult per input, in the same order. No partial returns —
    if a single text fails, the implementation handles the failure
    inline and emits a neutral result rather than dropping the row.
    """

    @abc.abstractmethod
    def classify_batch(self, texts: Iterable[str]) -> list[SentimentResult]: ...


# ---------------------------------------------------------------------------
# Future implementations — class shells only in this PR.
# Concrete bodies land alongside the social ingestion PRs.
# ---------------------------------------------------------------------------
class FinBertClassifier(SentimentClassifier):
    """Cheap first-pass classifier on every collected post.

    Local-only — the FinBERT weights are downloaded at process startup
    and inference runs on CPU. No API costs, single-digit-ms per text.
    """

    def classify_batch(self, texts: Iterable[str]) -> list[SentimentResult]:
        raise NotImplementedError("FinBertClassifier lands in a later PR")


class ClaudeHaikuClassifier(SentimentClassifier):
    """Rich second-pass classifier on the filtered subset.

    Uses the Anthropic API. Reserved for posts FinBERT flagged as either
    a strong sentiment signal OR ambiguous — the goal is to keep the
    Haiku spend low while extracting nuance the smaller model can't.
    """

    def classify_batch(self, texts: Iterable[str]) -> list[SentimentResult]:
        raise NotImplementedError("ClaudeHaikuClassifier lands in a later PR")
