"""EDGAR feature extractor.

Maps a processed filing + ticker metadata → a feature dict that the
catalyst scorer consumes. Output shape is the FEATURE_SCHEMA_VERSION
contract — adding a key requires bumping FEATURE_SCHEMA_VERSION in
config/constants.py so old predictions stay valid under their original
schema.

Pure function. The feature extractor does NOT query the database — the
caller assembles `ticker_metadata` from existing repositories and
passes everything in. That keeps the function unit-testable without
mocking SQLAlchemy and keeps DB access concentrated in the Celery
task boundary.
"""

from __future__ import annotations

from typing import Any


# Codes the Form-4 parser already emits to classify a transaction. P/A
# are buy-side; the existing form4_insider_buy bool on sec_filings is
# also accepted for backward-compat.
_FORM4_BUY_CODES = frozenset({"P", "A"})

# Exchanges treated as small-cap venues for the feature flags. Mirrors
# EDGAR_SMALL_EXCHANGES in constants.py but kept local so the feature
# extractor is self-contained.
_OTC_EXCHANGES = frozenset({"OTC", "PINK", "OTCBB"})
_NASDAQ_CAPITAL_MARKET = frozenset({"NASDAQ", "NASDAQ-CM", "XNAS"})


def _items_of(filing: dict[str, Any]) -> list[str]:
    raw = filing.get("item_numbers") or filing.get("items") or []
    if isinstance(raw, (str, int, float)):
        return [str(raw)]
    return [str(x) for x in raw]


def _is_form4_buy(filing: dict[str, Any]) -> bool:
    """True when the filing represents a buy-side Form-4 transaction.

    Accepts either the explicit `form4_transaction_code` (P / A / S /
    etc.) or the boolean `form4_insider_buy` the parser sets on the
    sec_filings row. Either path → buy.
    """
    if filing.get("form4_insider_buy"):
        return True
    code = (filing.get("form4_transaction_code") or "").strip().upper()
    return code in _FORM4_BUY_CODES


def _form4_value_usd(filing: dict[str, Any]) -> float | None:
    raw = filing.get("form4_value_usd")
    if raw is None:
        # Compute from shares × price if both present.
        shares = filing.get("form4_shares")
        price = filing.get("form4_price_per_share")
        if shares and price:
            try:
                return float(shares) * float(price)
            except (TypeError, ValueError):
                return None
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _exchange_flags(meta: dict[str, Any]) -> tuple[bool, bool]:
    exch = (meta.get("exchange") or "").upper().strip().replace(" ", "")
    is_otc = any(name.replace("-", "") in exch.replace("-", "") for name in _OTC_EXCHANGES)
    is_nasdaq_cm = any(name.replace("-", "") in exch.replace("-", "") for name in _NASDAQ_CAPITAL_MARKET)
    return is_otc, is_nasdaq_cm


def _avg_reliability(scores: Any) -> float | None:
    """`scores` is a list[float | None] of reliability scores from the
    matched promoter entities. Average non-None values; return None if
    the list is empty or all None."""
    if not scores:
        return None
    values = [float(s) for s in scores if s is not None]
    if not values:
        return None
    return sum(values) / len(values)


def extract_edgar_features(
    filing: dict[str, Any],
    ticker_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build the feature dict the rules-v1 scorer consumes.

    All keys in the returned dict are required by the FEATURE_SCHEMA_VERSION
    contract — None is a valid value when a piece of metadata is unknown,
    but the key itself is always present.
    """
    items = _items_of(filing)
    is_form4_buy = _is_form4_buy(filing)
    form4_val = _form4_value_usd(filing) if is_form4_buy else None

    promoter_match_count = int(ticker_metadata.get("promoter_match_count") or 0)
    has_known_promoter_match = promoter_match_count > 0

    is_otc, is_nasdaq_cm = _exchange_flags(ticker_metadata)

    return {
        "filing_form_type": filing.get("form_type"),
        "filing_items": items,
        "is_s3_effective": bool(filing.get("s3_effective")),
        "is_form4_buy": is_form4_buy,
        "form4_value_usd": form4_val,
        "has_known_promoter_match": has_known_promoter_match,
        "promoter_match_count": promoter_match_count,
        "promoter_match_reliability_avg": _avg_reliability(
            ticker_metadata.get("promoter_match_reliability_scores")
        ),
        "issuer_float_shares": ticker_metadata.get("float_shares"),
        "issuer_market_cap_usd": ticker_metadata.get("market_cap_usd"),
        "issuer_is_otc": is_otc,
        "issuer_is_nasdaq_cm": is_nasdaq_cm,
        "days_since_last_filing": ticker_metadata.get("days_since_last_filing"),
        "days_since_last_promoter_filing": ticker_metadata.get("days_since_last_promoter_filing"),
    }


# All keys the scorer (and downstream feature-attribution work) expect to
# find in every feature_vector. Bumping FEATURE_SCHEMA_VERSION is required
# whenever this list changes.
FEATURE_KEYS_FV_V1: tuple[str, ...] = (
    "filing_form_type",
    "filing_items",
    "is_s3_effective",
    "is_form4_buy",
    "form4_value_usd",
    "has_known_promoter_match",
    "promoter_match_count",
    "promoter_match_reliability_avg",
    "issuer_float_shares",
    "issuer_market_cap_usd",
    "issuer_is_otc",
    "issuer_is_nasdaq_cm",
    "days_since_last_filing",
    "days_since_last_promoter_filing",
)
