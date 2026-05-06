"""Unit tests for scripts/pre_market_brief.py.

Heavy reliance on the script's pure-formatting helpers — DB and
Telegram I/O are mocked at the orchestrator boundary so the helpers
can be tested independently.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import pre_market_brief as m  # noqa: E402


# ---------------------------------------------------------------------------
# format_brief_number
# ---------------------------------------------------------------------------
def test_brief_number_is_one_on_anchor_day():
    assert m.format_brief_number(date(2026, 5, 6)) == 1


def test_brief_number_increments_each_day():
    assert m.format_brief_number(date(2026, 5, 7)) == 2
    assert m.format_brief_number(date(2026, 5, 8)) == 3
    assert m.format_brief_number(date(2026, 5, 13)) == 8


def test_brief_number_clamps_to_one_before_anchor():
    """Sanity: a date earlier than the anchor returns 1, not a negative."""
    assert m.format_brief_number(date(2026, 5, 5)) == 1
    assert m.format_brief_number(date(2026, 1, 1)) == 1


# ---------------------------------------------------------------------------
# format_header
# ---------------------------------------------------------------------------
def test_header_uses_dynamic_schema_version():
    out = m.format_header(date(2026, 5, 6), "fv-v2")
    assert out == "Pre-market 2026-05-06 · brief #1 · fv-v2"


def test_header_picks_up_future_schema_versions_without_code_change():
    """Schema version is parameterized — no hardcoded 'fv-v2' check."""
    out = m.format_header(date(2026, 5, 7), "fv-v3")
    assert "fv-v3" in out
    assert "brief #2" in out


# ---------------------------------------------------------------------------
# format_human_int
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "n,expected",
    [
        (None, "—"),
        (0, "0"),
        (1, "1"),
        (999, "999"),
        (1000, "1K"),
        (1499, "1K"),
        (1500, "2K"),
        (938_133, "938K"),
        (999_499, "999K"),
        (999_999, "1000K"),  # rounds up to 1000K, just under the M boundary
        (1_000_000, "1.0M"),
        (1_174_718, "1.2M"),
        (9_381_344, "9.4M"),
        (10_000_000, "10.0M"),
    ],
)
def test_format_human_int_boundaries(n, expected):
    assert m.format_human_int(n) == expected


# ---------------------------------------------------------------------------
# classify_signal_pattern
# ---------------------------------------------------------------------------
def _fv(inputs: dict, weight_keys: tuple[str, ...] | None = None) -> dict:
    """Build a feature_vector dict matching what the scorer stores.

    weight_keys defaults to all FV-v2 weight names so any truthy input
    that matches a weight name fires.
    """
    from signals.scoring.catalyst_scorer import _RULES_V1_WEIGHTS

    if weight_keys is None:
        weights = dict(_RULES_V1_WEIGHTS)
    else:
        weights = {k: _RULES_V1_WEIGHTS.get(k, 0.0) for k in weight_keys}
    return {"inputs": inputs, "weights": weights}


def test_classify_pure_s3_only():
    fv = _fv({
        "filing_form_type": "S-3",
        "edgar_priority_form": True,
        "is_s3_effective": True,  # NOT a weight key — doesn't count as firing
    })
    assert m.classify_signal_pattern(fv) == "S-3-only"


def test_classify_form4_buy():
    fv = _fv({
        "filing_form_type": "4",
        "edgar_priority_form": True,
        "is_form4_buy": True,
    })
    assert m.classify_signal_pattern(fv) == "Form 4 buy"


def test_classify_8k_material():
    fv = _fv({
        "filing_form_type": "8-K",
        "edgar_priority_form": True,
        "filing_items": ["1.01"],
    })
    assert m.classify_signal_pattern(fv) == "8-K material"


def test_classify_multi_signal_three_weights():
    """3+ firing weights → multi-signal regardless of more-specific cases."""
    fv = _fv({
        "filing_form_type": "8-K",
        "edgar_priority_form": True,
        "ir_firm_engaged": True,
        "ir_firm_known_promoter": True,
    })
    assert m.classify_signal_pattern(fv) == "multi-signal"


def test_classify_other_fallback():
    fv = _fv({
        "filing_form_type": "DEF 14A",
        # nothing fires
    })
    assert m.classify_signal_pattern(fv) == "other"


# ---------------------------------------------------------------------------
# format_filing_context
# ---------------------------------------------------------------------------
def test_filing_context_s3_effective():
    fv = _fv({"filing_form_type": "S-3", "is_s3_effective": True})
    assert m.format_filing_context(fv) == "S-3 effective"


def test_filing_context_form4_with_value():
    fv = _fv({
        "filing_form_type": "4",
        "is_form4_buy": True,
        "form4_value_usd": 12_500.0,
    })
    assert m.format_filing_context(fv) == "Form 4 P-buy $12.5K"


def test_filing_context_form4_without_value():
    fv = _fv({"filing_form_type": "4", "is_form4_buy": True})
    assert m.format_filing_context(fv) == "Form 4 P-buy"


def test_filing_context_8k_with_item():
    fv = _fv({"filing_form_type": "8-K", "filing_items": ["1.01"]})
    assert m.format_filing_context(fv) == "8-K item 1.01"


def test_filing_context_8k_without_item():
    fv = _fv({"filing_form_type": "8-K", "filing_items": []})
    assert m.format_filing_context(fv) == "8-K"


def test_filing_context_em_dash_when_no_form_type():
    fv = _fv({})
    assert m.format_filing_context(fv) == "—"


# ---------------------------------------------------------------------------
# format_calibration_line
# ---------------------------------------------------------------------------
def test_calibration_zero_resolved():
    out = m.format_calibration_line([])
    assert out == "Calibration: 0 outcomes resolved · need ~50 for signal"


def test_calibration_low_n_below_threshold():
    points = [m.OutcomePoint(0.5, True)] * 5
    out = m.format_calibration_line(points)
    assert out == "Calibration: 5 outcomes resolved · need ~50 for signal"


def test_calibration_high_n_high_conf_wins():
    """High-conf bucket has higher hit rate → spread leads with positive sign.
    Format: 'Calibration: +Zpp spread (last N) · high-conf X% (n=A) · low-conf Y% (n=B)'."""
    high = [m.OutcomePoint(0.6, True)] * 4 + [m.OutcomePoint(0.6, False)] * 1
    low = [m.OutcomePoint(0.2, True)] * 1 + [m.OutcomePoint(0.2, False)] * 4
    out = m.format_calibration_line(high + low)
    # 5 high (80% hit) + 5 low (20% hit) → spread +60pp
    assert out.startswith("Calibration: +60pp spread (last 10) · ")
    assert "high-conf 80% (n=5)" in out
    assert "low-conf 20% (n=5)" in out


def test_calibration_high_n_low_conf_wins():
    """Low-conf bucket has higher hit rate → spread leads with negative sign.
    Sign must be explicit, not silently dropped."""
    high = [m.OutcomePoint(0.6, False)] * 4 + [m.OutcomePoint(0.6, True)] * 1
    low = [m.OutcomePoint(0.2, True)] * 4 + [m.OutcomePoint(0.2, False)] * 1
    out = m.format_calibration_line(high + low)
    assert out.startswith("Calibration: -60pp spread (last 10) · ")
    assert "high-conf 20% (n=5)" in out
    assert "low-conf 80% (n=5)" in out


def test_calibration_handles_median_tie_gracefully():
    """All confidences equal → median split yields empty 'low' bucket;
    fall back to plain hit rate without crashing."""
    points = [m.OutcomePoint(0.5, True)] * 7 + [m.OutcomePoint(0.5, False)] * 3
    out = m.format_calibration_line(points)
    assert "70%" in out
    assert "median confidence ties" in out


# ---------------------------------------------------------------------------
# format_signal_pattern_line
# ---------------------------------------------------------------------------
def _row(
    fv: dict,
    *,
    ticker: str = "ABCD",
    signal_type: str = "S2_DILUTION_RISK",
    confidence: Decimal = Decimal("0.2000"),
    latest_price: Decimal | None = Decimal("3.45"),
    float_shares: int | None = 938_133,
    created_at: datetime | None = None,
) -> m.PredictionRow:
    return m.PredictionRow(
        ticker=ticker,
        signal_type=signal_type,
        confidence=confidence,
        feature_vector=fv,
        created_at=created_at or datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        latest_price=latest_price,
        float_shares=float_shares,
    )


def test_signal_pattern_line_zero_predictions():
    assert m.format_signal_pattern_line([]) == "This week: 0 predictions."


def test_signal_pattern_line_omits_zero_count_categories():
    rows = [
        _row(_fv({"filing_form_type": "S-3", "edgar_priority_form": True})),
        _row(_fv({"filing_form_type": "S-3", "edgar_priority_form": True})),
        _row(_fv({
            "filing_form_type": "4",
            "edgar_priority_form": True,
            "is_form4_buy": True,
        })),
    ]
    out = m.format_signal_pattern_line(rows)
    assert "This week: 3 predictions" in out
    assert "S-3-only (2)" in out
    assert "Form 4 buys (1)" in out
    assert "8-K material" not in out  # zero count → omitted
    assert "multi-signal" not in out
    assert "other" not in out


# ---------------------------------------------------------------------------
# format_prediction_block
# ---------------------------------------------------------------------------
def test_prediction_block_full_fields():
    fv = _fv({
        "filing_form_type": "S-3",
        "is_s3_effective": True,
        "edgar_priority_form": True,
    })
    row = _row(
        fv,
        ticker="ARTL",
        signal_type="S2_DILUTION_RISK",
        confidence=Decimal("0.2000"),
        latest_price=Decimal("3.45"),
        float_shares=938_133,
    )
    block = m.format_prediction_block(row)
    lines = block.split("\n")
    assert lines[0] == "ARTL · S2_DILUTION_RISK · 0.2000"
    assert "938K" in lines[1]
    assert "$3.45" in lines[1]
    assert "S-3 effective" in lines[1]
    assert lines[2].startswith("   weights: ")
    assert "edgar_priority_form" in lines[2]


def test_prediction_block_missing_price_uses_em_dash():
    fv = _fv({"filing_form_type": "S-3", "is_s3_effective": True})
    row = _row(fv, latest_price=None)
    block = m.format_prediction_block(row)
    assert "$—" in block
    # NOT a hyphen-minus
    assert "$-" not in block.replace("$—", "")


def test_prediction_block_missing_float_uses_em_dash():
    fv = _fv({"filing_form_type": "S-3", "is_s3_effective": True})
    row = _row(fv, float_shares=None)
    block = m.format_prediction_block(row)
    # Second line starts with FLOAT_HUMAN — should be em dash.
    second_line = block.split("\n")[1]
    assert second_line.lstrip().startswith("—")


def test_prediction_block_no_firing_weights_says_none():
    fv = _fv({})  # nothing truthy
    row = _row(fv)
    block = m.format_prediction_block(row)
    assert "weights: (none)" in block


# ---------------------------------------------------------------------------
# format_new_predictions_section
# ---------------------------------------------------------------------------
def test_new_predictions_section_zero_predictions():
    assert m.format_new_predictions_section([]) == "No new predictions in last 24h."


def test_new_predictions_section_blocks_separated_by_blank_line():
    rows = [
        _row(_fv({"filing_form_type": "S-3", "is_s3_effective": True}), ticker="A"),
        _row(_fv({"filing_form_type": "S-3", "is_s3_effective": True}), ticker="B"),
    ]
    out = m.format_new_predictions_section(rows)
    # Two blocks (3 lines each) separated by ONE blank line = "\n\n"
    blocks = out.split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].startswith("A · ")
    assert blocks[1].startswith("B · ")


# ---------------------------------------------------------------------------
# build_message — composition
# ---------------------------------------------------------------------------
def test_build_message_has_all_four_sections():
    msg = m.build_message(
        date(2026, 5, 6),
        "fv-v2",
        outcomes=[],
        week_predictions=[],
        new_predictions=[],
    )
    sections = msg.split("\n\n")
    # 4 sections: header, calibration, weekly, new
    assert len(sections) == 4
    assert sections[0].startswith("Pre-market 2026-05-06")
    assert sections[1].startswith("Calibration:")
    assert sections[2].startswith("This week:")
    assert sections[3] == "No new predictions in last 24h."


# ---------------------------------------------------------------------------
# split_for_telegram
# ---------------------------------------------------------------------------
def test_split_for_telegram_passes_short_messages_through():
    msg = "tiny\n\nbrief"
    assert m.split_for_telegram(msg) == [msg]


def test_split_for_telegram_splits_on_block_boundaries():
    block = "X" * 1500
    msg = "\n\n".join([block, block, block])  # ~4500 chars
    parts = m.split_for_telegram(msg, max_len=3500)
    assert len(parts) >= 2
    # Each part stays under the cap.
    assert all(len(p) <= 3500 for p in parts)


# ---------------------------------------------------------------------------
# main — env-var validation (NO Telegram call)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_main_exits_1_when_token_missing(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    rc = await m.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err


@pytest.mark.asyncio
async def test_main_exits_1_when_chat_id_missing(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    rc = await m.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "TELEGRAM_CHAT_ID" in err


@pytest.mark.asyncio
async def test_main_exits_1_when_token_blank(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    rc = await m.main()
    assert rc == 1


# ---------------------------------------------------------------------------
# send_telegram — mocked httpx
# ---------------------------------------------------------------------------
def test_send_telegram_posts_to_correct_url(monkeypatch):
    posted: list = []

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def fake_post(url, json, timeout):
        posted.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse()

    fake_httpx = MagicMock()
    fake_httpx.post = fake_post
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    m.send_telegram("token-X", "chat-Y", "hello world")

    assert len(posted) == 1
    assert posted[0]["url"] == "https://api.telegram.org/bottoken-X/sendMessage"
    assert posted[0]["json"]["chat_id"] == "chat-Y"
    assert posted[0]["json"]["text"] == "hello world"
    assert posted[0]["json"]["parse_mode"] == "Markdown"
