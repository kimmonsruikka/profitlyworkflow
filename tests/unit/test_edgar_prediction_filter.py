from __future__ import annotations

from signals.filters.edgar_prediction_filter import (
    FORM4_MIN_VALUE_USD,
    is_prediction_worthy,
)


def _filing(**overrides):
    base = {
        "form_type": "8-K",
        "ticker": "ABCD",
        "item_numbers": [],
        "s3_effective": False,
        "form4_transaction_code": None,
        "form4_value_usd": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 8-K
# ---------------------------------------------------------------------------
def test_8k_with_material_item_is_worthy():
    worthy, reason = is_prediction_worthy(_filing(item_numbers=["8.01"]))
    assert worthy is True
    assert reason is None


def test_8k_with_only_item_9_01_is_skipped():
    """9.01 (Exhibits) is not material on its own."""
    worthy, reason = is_prediction_worthy(_filing(item_numbers=["9.01"]))
    assert worthy is False
    assert reason == "non_material_items"


def test_8k_with_material_plus_boilerplate_is_worthy():
    worthy, reason = is_prediction_worthy(_filing(item_numbers=["1.01", "9.01"]))
    assert worthy is True
    assert reason is None


def test_8k_with_no_items_extracted_is_skipped():
    worthy, reason = is_prediction_worthy(_filing(item_numbers=[]))
    assert worthy is False
    assert reason == "non_material_items"


# ---------------------------------------------------------------------------
# S-3
# ---------------------------------------------------------------------------
def test_s3_effective_is_worthy():
    worthy, reason = is_prediction_worthy(_filing(form_type="S-3", s3_effective=True))
    assert worthy is True
    assert reason is None


def test_s3_not_effective_is_skipped():
    worthy, reason = is_prediction_worthy(_filing(form_type="S-3", s3_effective=False))
    assert worthy is False
    assert reason == "s3_not_effective"


# ---------------------------------------------------------------------------
# Form 4
# ---------------------------------------------------------------------------
def test_form4_buy_above_threshold_is_worthy():
    worthy, reason = is_prediction_worthy(_filing(
        form_type="4", form4_transaction_code="P", form4_value_usd=75_000.0,
    ))
    assert worthy is True
    assert reason is None


def test_form4_buy_below_threshold_is_skipped():
    worthy, reason = is_prediction_worthy(_filing(
        form_type="4", form4_transaction_code="P", form4_value_usd=20_000.0,
    ))
    assert worthy is False
    assert reason == "value_below_threshold"


def test_form4_sell_is_skipped():
    worthy, reason = is_prediction_worthy(_filing(
        form_type="4", form4_transaction_code="S", form4_value_usd=200_000.0,
    ))
    assert worthy is False
    assert reason == "form4_sell"


def test_form4_buy_no_value_is_skipped():
    """If we couldn't compute the dollar value, treat as below-threshold."""
    worthy, reason = is_prediction_worthy(_filing(
        form_type="4", form4_transaction_code="P", form4_value_usd=None,
    ))
    assert worthy is False
    assert reason == "value_below_threshold"


def test_form4_unknown_code_is_skipped():
    worthy, reason = is_prediction_worthy(_filing(
        form_type="4", form4_transaction_code="X", form4_value_usd=99_999.0,
    ))
    assert worthy is False
    assert reason == "form4_other_code"


# ---------------------------------------------------------------------------
# Routine forms
# ---------------------------------------------------------------------------
def test_10q_is_skipped():
    worthy, reason = is_prediction_worthy(_filing(form_type="10-Q"))
    assert worthy is False
    assert reason == "non_predictive_form_type"


def test_def_14a_is_skipped_without_promoter_match():
    worthy, reason = is_prediction_worthy(_filing(form_type="DEF 14A"))
    assert worthy is False
    assert reason == "non_predictive_form_type"


# ---------------------------------------------------------------------------
# Promoter-network override
# ---------------------------------------------------------------------------
def test_promoter_match_overrides_routine_form_skip():
    """A 10-Q on a known promoter ticker IS worth a prediction."""
    worthy, reason = is_prediction_worthy(
        _filing(form_type="10-Q"),
        has_promoter_match=True,
    )
    assert worthy is True
    assert reason is None


def test_promoter_match_overrides_form4_sell():
    """Even Form 4 sells are worth predicting if the issuer is a known
    promoter ticker — sells from network insiders are signal too."""
    worthy, reason = is_prediction_worthy(
        _filing(form_type="4", form4_transaction_code="S"),
        has_promoter_match=True,
    )
    assert worthy is True


def test_promoter_match_overrides_8k_with_only_boilerplate():
    worthy, reason = is_prediction_worthy(
        _filing(item_numbers=["9.01"]),
        has_promoter_match=True,
    )
    assert worthy is True


# ---------------------------------------------------------------------------
# Threshold sanity
# ---------------------------------------------------------------------------
def test_form4_threshold_constant_is_50k():
    """Documenting the placeholder value via a test so future tuning
    surfaces in the diff."""
    assert FORM4_MIN_VALUE_USD == 50_000.0
