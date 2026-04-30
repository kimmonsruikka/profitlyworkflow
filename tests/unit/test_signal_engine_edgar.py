from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import constants
from signals.engine import SignalEngine, _signal_type_for_filing
from signals.scoring.catalyst_scorer import RulesV1Scorer


def _filing(**overrides):
    base = {
        "ticker": "ABCD",
        "form_type": "8-K",
        "cik": "0001234567",
        "accession_number": "0001234567-26-000001",
        "item_numbers": ["8.01"],
        "ir_firm_mentioned": None,
        "s3_effective": False,
        "form4_insider_buy": False,
        "form4_value_usd": None,
        "form4_transaction_code": None,
        "underwriter_id": None,
    }
    base.update(overrides)
    return base


def _meta(**overrides):
    base = {
        "ticker": "ABCD",
        "exchange": "OTC",
        "float_shares": 4_000_000,
        "market_cap_usd": 18_000_000.0,
        "promoter_match_count": 0,
        "promoter_match_reliability_scores": [],
        "days_since_last_filing": 30,
        "days_since_last_promoter_filing": None,
    }
    base.update(overrides)
    return base


def _mock_session():
    """Mock session that fakes server-default population on refresh."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    fake_pid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async def refresh(row):
        row.prediction_id = fake_pid
        row.created_at = now
    session.refresh = AsyncMock(side_effect=refresh)
    session._fake_pid = fake_pid
    return session


# ---------------------------------------------------------------------------
# evaluate_edgar_filing — happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worthy_filing_writes_prediction():
    session = _mock_session()
    engine = SignalEngine(scorer=RulesV1Scorer(), session=session)

    prediction = await engine.evaluate_edgar_filing(_filing(), _meta())

    assert prediction is not None
    assert prediction.ticker == "ABCD"
    assert prediction.signal_type == "S1_CATALYST"
    assert prediction.scorer_version == "rules-v1"
    assert prediction.feature_schema_version == constants.FEATURE_SCHEMA_VERSION
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_unworthy_filing_returns_none():
    session = _mock_session()
    engine = SignalEngine(scorer=RulesV1Scorer(), session=session)

    # 10-Q with no promoter match → filter rejects.
    prediction = await engine.evaluate_edgar_filing(
        _filing(form_type="10-Q", item_numbers=[]),
        _meta(),
    )
    assert prediction is None
    session.add.assert_not_called()


# ---------------------------------------------------------------------------
# Exception propagation contract
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scorer_exception_propagates():
    """The Celery task is the only catch boundary; SignalEngine itself
    must surface scorer errors so they're visible in operator logs."""
    session = _mock_session()
    bad_scorer = MagicMock()
    bad_scorer.score = MagicMock(side_effect=RuntimeError("scorer broke"))
    engine = SignalEngine(scorer=bad_scorer, session=session)

    with pytest.raises(RuntimeError, match="scorer broke"):
        await engine.evaluate_edgar_filing(_filing(), _meta())


@pytest.mark.asyncio
async def test_predictions_repo_exception_propagates(monkeypatch):
    session = _mock_session()
    engine = SignalEngine(scorer=RulesV1Scorer(), session=session)

    from signals import engine as engine_mod

    class BoomRepo:
        def __init__(self, _session): pass
        async def create(self, _payload):
            raise RuntimeError("repo broke")

    monkeypatch.setattr(engine_mod, "PredictionsRepository", BoomRepo)

    with pytest.raises(RuntimeError, match="repo broke"):
        await engine.evaluate_edgar_filing(_filing(), _meta())


# ---------------------------------------------------------------------------
# Signal-type mapper
# ---------------------------------------------------------------------------
def test_signal_type_8k_material_is_s1_catalyst():
    assert _signal_type_for_filing(
        _filing(form_type="8-K", item_numbers=["8.01"]), _meta(),
    ) == "S1_CATALYST"


def test_signal_type_s3_effective_is_dilution_risk():
    assert _signal_type_for_filing(
        _filing(form_type="S-3", s3_effective=True), _meta(),
    ) == "S2_DILUTION_RISK"


def test_signal_type_form4_buy_is_category_d():
    assert _signal_type_for_filing(
        _filing(form_type="4", form4_insider_buy=True), _meta(),
    ) == "S2_CATEGORY_D"


def test_signal_type_promoter_only_is_category_a():
    assert _signal_type_for_filing(
        _filing(form_type="10-Q"),
        _meta(promoter_match_count=2),
    ) == "S2_CATEGORY_A"


# ---------------------------------------------------------------------------
# SIGNAL_TYPE_DEFAULTS values flow through
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_window_and_target_pulled_from_signal_type_defaults():
    session = _mock_session()
    engine = SignalEngine(scorer=RulesV1Scorer(), session=session)

    pred = await engine.evaluate_edgar_filing(
        _filing(form_type="S-3", s3_effective=True), _meta(),
    )
    assert pred is not None
    expected = constants.SIGNAL_TYPE_DEFAULTS["S2_DILUTION_RISK"]
    assert pred.predicted_window_minutes == int(expected["window_minutes"])
    # predicted_target_pct stored as Decimal in PredictionRead
    assert float(pred.predicted_target_pct) == float(expected["target_pct"])


@pytest.mark.asyncio
async def test_negative_target_pct_persisted_for_short_predictions():
    """S-3 effective should store a NEGATIVE target_pct so the resolution
    flow's classify_outcome can flip direction correctly."""
    session = _mock_session()
    engine = SignalEngine(scorer=RulesV1Scorer(), session=session)

    pred = await engine.evaluate_edgar_filing(
        _filing(form_type="S-3", s3_effective=True), _meta(),
    )
    assert pred is not None
    assert float(pred.predicted_target_pct) < 0
