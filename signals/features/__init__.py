"""Feature extractors. Each lives next to the source it extracts from."""

from signals.features.edgar_features import extract_edgar_features

__all__ = ["extract_edgar_features"]
