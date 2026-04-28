from __future__ import annotations

from config import constants
from config.settings import Settings, settings


def test_settings_loads_in_test_environment() -> None:
    assert settings.ENVIRONMENT in {"development", "staging", "production"}
    assert settings.BROKER_MODE in {"paper", "live"}


def test_is_live_trading_false_when_paper() -> None:
    s = Settings(ENVIRONMENT="production", BROKER_MODE="paper")
    assert s.is_live_trading is False


def test_is_live_trading_false_in_staging_even_with_live_broker() -> None:
    s = Settings(ENVIRONMENT="staging", BROKER_MODE="live")
    assert s.is_live_trading is False


def test_is_live_trading_true_only_in_production_live() -> None:
    s = Settings(ENVIRONMENT="production", BROKER_MODE="live")
    assert s.is_live_trading is True


POSITIVE_NUMERIC_CONSTANTS = [
    "FLOAT_MAX",
    "FLOAT_MICRO",
    "PRICE_MIN",
    "PRICE_MAX",
    "AVG_DAILY_VOLUME_MIN",
    "CATALYST_SCORE_MINIMUM",
    "CONFIDENCE_THRESHOLD_S1",
    "CONFIDENCE_THRESHOLD_S2",
    "CONFIDENCE_THRESHOLD_S1_WITH_S2_OPEN",
    "LIQUIDITY_SCORE_MINIMUM",
    "LIQUIDITY_SCORE_FULL_SIZE",
    "LIQUIDITY_SCORE_EXCELLENT",
    "MIN_RR_RATIO",
    "RISK_PCT_S1",
    "RISK_PCT_S2_MIN",
    "RISK_PCT_S2_MAX",
    "MAX_DAILY_LOSS_PCT",
    "MAX_DAILY_LOSS_S1",
    "MAX_DAILY_LOSS_S2",
    "MAX_POSITION_PCT",
    "MAX_POSITION_PCT_SUB2",
    "S2_MAX_CONCURRENT",
    "S2_MAX_EXPOSURE_PCT",
    "COMBINED_EXPOSURE_MAX",
    "CASH_BUFFER_MIN",
    "CONSECUTIVE_LOSS_THRESHOLD",
    "CONSECUTIVE_LOSS_SIZE_REDUCTION",
    "S1_VOLUME_MULTIPLIER",
    "S1_SPREAD_MAX_PCT",
    "S1_ENTRY_WINDOW_MINUTES",
    "S2_TIME_STOP_CATEGORY_A",
    "S2_TIME_STOP_CATEGORY_B",
    "S2_TIME_STOP_CATEGORY_C",
    "S1_TARGET_1_R",
    "S1_TARGET_1_SELL_PCT",
    "S1_TARGET_2_R",
    "S1_TIME_STOP_NO_MOVEMENT_MINS",
    "S1_TIME_STOP_FULL_EXIT_MINS",
    "VIX_REDUCE_THRESHOLD",
    "VIX_SUPPRESS_THRESHOLD",
    "SPREAD_LIMIT_ABSOLUTE",
    "BRACKET_ORDER_TIMEOUT_SECONDS",
    "PDT_ACCOUNT_THRESHOLD",
    "PDT_MAX_DAY_TRADES",
    "PDT_ROLLING_DAYS",
    "WEIGHT_0_6_MONTHS",
    "WEIGHT_6_12_MONTHS",
    "WEIGHT_12_24_MONTHS",
    "WEIGHT_24_36_MONTHS",
]


def test_positive_constants_are_positive() -> None:
    for name in POSITIVE_NUMERIC_CONSTANTS:
        value = getattr(constants, name)
        assert value > 0, f"{name} must be positive, got {value!r}"


def test_allocation_budget_is_consistent() -> None:
    """Cash buffer + max swing exposure + a single max position must fit within 100%."""
    total = (
        constants.CASH_BUFFER_MIN
        + constants.S2_MAX_EXPOSURE_PCT
        + 0.20
    )
    assert total <= 1.0, (
        f"CASH_BUFFER_MIN ({constants.CASH_BUFFER_MIN}) + "
        f"S2_MAX_EXPOSURE_PCT ({constants.S2_MAX_EXPOSURE_PCT}) + 0.20 = {total} > 1.0"
    )
