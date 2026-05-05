from __future__ import annotations

from signals.features.edgar_features import (
    FEATURE_KEYS_FV_V1,
    extract_edgar_features,
)


def _meta(**overrides):
    base = {
        "ticker": "ABCD",
        "exchange": "OTC",
        "float_shares": 4_100_000,
        "market_cap_usd": 22_000_000.0,
        "promoter_match_count": 0,
        "promoter_match_reliability_scores": [],
        "days_since_last_filing": 14,
        "days_since_last_promoter_filing": None,
    }
    base.update(overrides)
    return base


def _filing(**overrides):
    base = {
        "ticker": "ABCD",
        "form_type": "8-K",
        "item_numbers": [],
        "s3_effective": False,
        "form4_insider_buy": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Required-key contract
# ---------------------------------------------------------------------------
def test_all_required_keys_present_for_8k_material():
    out = extract_edgar_features(
        _filing(item_numbers=["8.01"]),
        _meta(),
    )
    for key in FEATURE_KEYS_FV_V1:
        assert key in out, f"missing required feature key {key}"


def test_required_keys_present_even_when_metadata_missing():
    """Empty metadata → None values, not exceptions."""
    out = extract_edgar_features(_filing(), _meta(
        exchange=None,
        float_shares=None,
        market_cap_usd=None,
        promoter_match_count=None,
        promoter_match_reliability_scores=None,
        days_since_last_filing=None,
    ))
    for key in FEATURE_KEYS_FV_V1:
        assert key in out
    assert out["issuer_float_shares"] is None
    assert out["issuer_market_cap_usd"] is None


# ---------------------------------------------------------------------------
# S-3 effective flag
# ---------------------------------------------------------------------------
def test_s3_effective_sets_flag():
    out = extract_edgar_features(
        _filing(form_type="S-3", s3_effective=True), _meta(),
    )
    assert out["is_s3_effective"] is True


def test_s3_not_effective_clears_flag():
    out = extract_edgar_features(
        _filing(form_type="S-3", s3_effective=False), _meta(),
    )
    assert out["is_s3_effective"] is False


# ---------------------------------------------------------------------------
# Form 4
# ---------------------------------------------------------------------------
def test_form4_insider_buy_bool_alone_does_not_fire_in_fv_v2():
    """FV-v2 narrowed is_form4_buy to require an explicit P-code. The
    legacy form4_insider_buy boolean fallback was removed because the
    parser sets that boolean without P/A discrimination — falling back
    to it would silently broaden the signal back to FV-v1 semantics."""
    out = extract_edgar_features(
        _filing(form_type="4", form4_insider_buy=True), _meta(),
    )
    assert out["is_form4_buy"] is False


def test_form4_p_code_fires_is_form4_buy():
    """Explicit P-code (open-market purchase) is the only thing that
    fires the FV-v2 form-4 buy flag."""
    out = extract_edgar_features(
        _filing(
            form_type="4",
            form4_insider_buy=True,
            form4_transaction_code="P",
        ),
        _meta(),
    )
    assert out["is_form4_buy"] is True


def test_form4_a_code_does_not_fire_is_form4_buy():
    """A-code (grant/award) is excluded in FV-v2. This narrowing is the
    calibration intent the schema bump exists to permit."""
    out = extract_edgar_features(
        _filing(
            form_type="4",
            form4_insider_buy=True,
            form4_transaction_code="A",
        ),
        _meta(),
    )
    assert out["is_form4_buy"] is False


def test_form4_value_computed_from_shares_times_price():
    """form4_value_usd is gated on _is_form4_buy returning True — under
    FV-v2 that requires the explicit P-code."""
    out = extract_edgar_features(
        _filing(
            form_type="4", form4_insider_buy=True,
            form4_transaction_code="P",
            form4_shares=10_000, form4_price_per_share=4.25,
        ),
        _meta(),
    )
    assert out["form4_value_usd"] == 42_500.0


def test_form4_value_unset_when_not_buy():
    out = extract_edgar_features(
        _filing(form_type="4", form4_insider_buy=False), _meta(),
    )
    assert out["form4_value_usd"] is None


# ---------------------------------------------------------------------------
# Promoter match
# ---------------------------------------------------------------------------
def test_promoter_match_present_sets_flag_and_count():
    out = extract_edgar_features(
        _filing(),
        _meta(promoter_match_count=2,
              promoter_match_reliability_scores=[0.78, 0.62]),
    )
    assert out["has_known_promoter_match"] is True
    assert out["promoter_match_count"] == 2
    assert out["promoter_match_reliability_avg"] == 0.7


def test_promoter_match_absent_clears_flag():
    out = extract_edgar_features(_filing(), _meta(promoter_match_count=0))
    assert out["has_known_promoter_match"] is False
    assert out["promoter_match_count"] == 0
    assert out["promoter_match_reliability_avg"] is None


def test_promoter_reliability_avg_skips_none_values():
    out = extract_edgar_features(_filing(), _meta(
        promoter_match_count=3,
        promoter_match_reliability_scores=[0.8, None, 0.6],
    ))
    assert out["promoter_match_reliability_avg"] == 0.7


# ---------------------------------------------------------------------------
# Exchange flags
# ---------------------------------------------------------------------------
def test_otc_exchange_sets_otc_flag():
    out = extract_edgar_features(_filing(), _meta(exchange="OTC"))
    assert out["issuer_is_otc"] is True
    assert out["issuer_is_nasdaq_cm"] is False


def test_nasdaq_exchange_sets_nasdaq_cm_flag():
    out = extract_edgar_features(_filing(), _meta(exchange="Nasdaq"))
    assert out["issuer_is_nasdaq_cm"] is True
    assert out["issuer_is_otc"] is False


def test_unknown_exchange_clears_both_flags():
    out = extract_edgar_features(_filing(), _meta(exchange="LSE"))
    assert out["issuer_is_otc"] is False
    assert out["issuer_is_nasdaq_cm"] is False
