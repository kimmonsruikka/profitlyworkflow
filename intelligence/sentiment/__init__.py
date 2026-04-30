"""Hybrid sentiment classification.

Two-stage design:
  - FinBERT (local, free, fast)        — first pass on every collected post
  - Claude Haiku (Anthropic API, paid) — rich second pass on the filtered subset

This package only ships the abstract interface in this PR. Concrete
implementations land alongside the social ingestion PRs.
"""

from intelligence.sentiment.base import (
    ClaudeHaikuClassifier,
    FinBertClassifier,
    SentimentClassifier,
    SentimentResult,
)

__all__ = [
    "ClaudeHaikuClassifier",
    "FinBertClassifier",
    "SentimentClassifier",
    "SentimentResult",
]
