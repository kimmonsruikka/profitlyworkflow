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

from config import constants


# FV-v2 narrows form-4 buy semantics to P-codes only (open-market purchase),
# matching the original RulesV1Scorer calibration intent. FV-v1 accepted
# P+A; A-codes (grant/award acceptance) are deliberately excluded in v2 —
# that's the calibration distinction the schema bump exists to permit. Old
# FV-v1 predictions remain valid under the broader interpretation; new
# FV-v2 predictions use this narrower one.
_FORM4_BUY_CODES = frozenset({"P"})

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
    """True when the filing represents a P-code (open-market) Form-4 buy.

    FV-v2 reads ONLY the explicit `form4_transaction_code`. The legacy
    `form4_insider_buy` boolean fallback is intentionally removed — that
    boolean is set by the parser without P/A discrimination, so falling
    back to it would silently broaden the signal back to FV-v1's P+A
    semantics and miscalibrate the weight. Until the Form-4 parser is
    enhanced to populate `form4_transaction_code` (separate follow-up
    PR), this flag stays False on filings that lack the explicit code.
    Calibration-honest by design.
    """
    code = (filing.get("form4_transaction_code") or "").strip().upper()
    return code in _FORM4_BUY_CODES


def _is_edgar_priority_form(filing: dict[str, Any]) -> bool:
    """True iff filing's form_type is in the EDGAR_PRIORITY_FORMS set.

    Mirrors the watcher's universe filter — these are the form types we
    consider worth scoring at all. The `edgar_priority_form` weight
    fires on any filing that cleared the universe gate.
    """
    form_type = (filing.get("form_type") or "").strip()
    return form_type in constants.EDGAR_PRIORITY_FORMS


def _ir_firm_engaged(filing: dict[str, Any]) -> bool:
    """True iff the parser detected an IR firm name on the filing."""
    name = filing.get("ir_firm_mentioned")
    return bool(name and str(name).strip())


def _has_reverse_split(filing: dict[str, Any]) -> bool:
    """True iff the parser extracted a reverse-split ratio.

    The parser stores the result of `extract_reverse_split(text)` under
    `sec_filings.full_text->>'reverse_split'` (see rss_watcher.py). Any
    non-null object here means a ratio was identified.
    """
    full_text = filing.get("full_text") or {}
    if not isinstance(full_text, dict):
        return False
    return bool(full_text.get("reverse_split"))


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
        # FV-v1 keys (unchanged in v2 except is_form4_buy semantics — see
        # _is_form4_buy docstring).
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
        # FV-v2 additions: scorer-vocabulary keys derived from existing
        # parser-stored fields and caller-resolved metadata.
        "edgar_priority_form": _is_edgar_priority_form(filing),
        "ir_firm_engaged": _ir_firm_engaged(filing),
        # ir_firm_known_promoter is NARROW by design — caller resolves the
        # IR firm name against promoter_entities filtered to type='ir_firm'.
        # Distinct from the broader has_known_promoter_match (which fires
        # on any promoter graph entity including underwriters / attorneys).
        "ir_firm_known_promoter": bool(ticker_metadata.get("ir_firm_known_promoter")),
        # underwriter_flagged requires JOIN underwriters ON underwriter_id
        # WHERE manipulation_flagged = TRUE. Caller pre-resolves it.
        "underwriter_flagged": bool(ticker_metadata.get("underwriter_flagged")),
        "reverse_split": _has_reverse_split(filing),
    }


# FV-v1 schema. Kept for historical reference and so test code can assert
# FV-v2 ⊇ FV-v1. Predictions written under the v1 vocabulary remain valid
# under their original feature_schema_version pin per CLAUDE.md.
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


# FV-v2 schema. Adds five keys to satisfy the rules-v1 scorer's vocabulary
# and bumps `is_form4_buy` semantics from P+A to P-only (see
# _is_form4_buy docstring). Bumping FEATURE_SCHEMA_VERSION is required
# whenever this list changes.
FEATURE_KEYS_FV_V2: tuple[str, ...] = FEATURE_KEYS_FV_V1 + (
    "edgar_priority_form",
    "ir_firm_engaged",
    "ir_firm_known_promoter",
    "underwriter_flagged",
    "reverse_split",
)
