"""Filters that decide whether an event is prediction-worthy."""

from signals.filters.edgar_prediction_filter import is_prediction_worthy

__all__ = ["is_prediction_worthy"]
