"""Decides whether an EDGAR filing is prediction-worthy.

Out-of-scope to fire predictions on every filing — that floods the
predictions table with noise. The filter restricts to filings that
carry actual signal: material 8-K items, S-3 effective, sizable Form-4
buys, or any filing on a ticker that matches our promoter network.

Pure function — takes a filing dict and an optional promoter-match
flag (caller does the lookup), returns (is_worthy, skip_reason).
"""

from __future__ import annotations

from typing import Any


# 8-K item codes treated as material — fire a prediction even on their own.
# 7.01 (Reg FD) and 9.01 (Exhibits) explicitly excluded: they're catalyst-
# adjacent boilerplate that produces too much noise on their own. They
# count if they appear ALONGSIDE a material item, but don't trigger one.
_MATERIAL_8K_ITEMS = frozenset({
    "1.01",  # Entry into a Material Definitive Agreement
    "2.01",  # Completion of Acquisition or Disposition of Assets
    "2.02",  # Results of Operations and Financial Condition
    "5.02",  # Departure / Election / Appointment of Directors / Officers
    "5.03",  # Amendments to Articles (reverse splits)
    "8.01",  # Other Events (catch-all that's actually used for catalysts)
    "3.02",  # Unregistered Sales of Equity Securities (private placements)
})

# Form 4 transaction codes treated as buying activity. P (open-market
# purchase) and A (grant) are the two strong-signal codes; M / G / J /
# others are noise for our purposes.
_FORM4_BUY_CODES = frozenset({"P", "A"})

# Form 4 sell codes — explicitly recorded as "skip with sell reason" so
# the dashboard can count how many filings were skipped for this reason.
_FORM4_SELL_CODES = frozenset({"S", "D", "F", "M"})

# Minimum dollar value of a Form 4 buy to fire a prediction. Round
# threshold; tune empirically once we have outcome data.
FORM4_MIN_VALUE_USD = 50_000.0

# Forms that don't trigger predictions on their own. Promoter-network
# match still overrides this (caller decides).
_NON_PREDICTIVE_FORMS = frozenset({
    "10-K", "10-K/A",
    "10-Q", "10-Q/A",
    "10-KT", "10-QT",
    "DEF 14A",  # proxies — wait for the 8-K Item 5.03 if there's a vote
    "SC 13G", "SC 13G/A",
    "NT 10-K", "NT 10-Q",
    "144",
})


def _items_of(filing: dict[str, Any]) -> list[str]:
    """Return 8-K item numbers as a list of strings, regardless of how
    the caller packaged them (JSONB list, set, single value, missing)."""
    raw = filing.get("item_numbers") or filing.get("items") or []
    if isinstance(raw, (str, int, float)):
        return [str(raw)]
    return [str(x) for x in raw]


def is_prediction_worthy(
    filing: dict[str, Any],
    *,
    has_promoter_match: bool = False,
) -> tuple[bool, str | None]:
    """Decide whether this filing should fire a prediction.

    Returns (True, None) if it should, or (False, reason) if not. The
    reason string is also a label the caller can log / aggregate so we
    know WHY most filings get skipped without scanning logs by hand.

    `has_promoter_match` is set by the caller after looking up the
    issuer ticker against promoter_entities / underwriters — any
    promoter match overrides the form-type filter (a 10-K from a known
    pump-and-dump operator IS worth a prediction).
    """
    form_raw = (filing.get("form_type") or "").strip()
    form_upper = form_raw.upper()

    # 4) Promoter-network match overrides every other rule.
    if has_promoter_match:
        return True, None

    # 1) 8-K with at least one material item.
    if form_upper.startswith("8-K"):
        items = _items_of(filing)
        material_hits = [i for i in items if i in _MATERIAL_8K_ITEMS]
        if material_hits:
            return True, None
        if items:
            return False, "non_material_items"
        # 8-K with no items extracted — could be a parser miss; treat as
        # non-material rather than firing on noise.
        return False, "non_material_items"

    # 2) S-3 effective.
    if form_upper.startswith("S-3"):
        if filing.get("s3_effective"):
            return True, None
        return False, "s3_not_effective"

    # 3) Form 4 buy with size threshold.
    if form_upper == "4":
        code = (filing.get("form4_transaction_code") or "").strip().upper()
        if code in _FORM4_SELL_CODES:
            return False, "form4_sell"
        if code not in _FORM4_BUY_CODES:
            return False, "form4_other_code"
        value = filing.get("form4_value_usd")
        if value is None or float(value) < FORM4_MIN_VALUE_USD:
            return False, "value_below_threshold"
        return True, None

    # Default: routine filings without unusual content.
    if form_upper in _NON_PREDICTIVE_FORMS:
        return False, "non_predictive_form_type"

    # Anything we don't have a rule for — be conservative, skip.
    return False, "non_predictive_form_type"
