from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from ingestion.edgar import filing_parser as fp


# ---------------------------------------------------------------------------
# Fixtures — abbreviated but realistic 8-K / S-1 / Form 4 snippets
# ---------------------------------------------------------------------------
SAMPLE_8K_IR_FIRM = """
UNITED STATES SECURITIES AND EXCHANGE COMMISSION
FORM 8-K — CURRENT REPORT

Item 1.01 Entry into a Material Definitive Agreement.

On April 15, 2026, ABC Corp (the "Company") entered into an investor relations
consulting agreement with Hayden IR, LLC, a Nevada limited liability company
("Hayden IR"). Pursuant to the terms of the agreement, Hayden IR will provide
investor relations services to the Company in exchange for a monthly fee of
$10,000 in cash and 50,000 shares of restricted common stock.

Item 9.01 Financial Statements and Exhibits.

(d) Exhibits.
"""

SAMPLE_8K_REVERSE_SPLIT = """
Item 5.03 Amendments to Articles of Incorporation or Bylaws; Change in Fiscal
Year.

On May 1, 2026, the Company filed a Certificate of Amendment to its Articles
of Incorporation with the Secretary of State of Delaware to effect a 1-for-10
reverse stock split of the Company's outstanding common stock. Immediately
prior to the split, the Company had 50,000,000 shares of common stock
outstanding. The reverse stock split will be effective as of May 15, 2026.

Item 8.01 Other Events.
"""

SAMPLE_8K_TWO_ITEMS = """
Item 2.02 Results of Operations and Financial Condition.

The Company hereby furnishes the press release announcing first-quarter
results, attached as Exhibit 99.1.

Item 8.01 Other Events.

The Company announced today the receipt of FDA breakthrough designation for
its lead candidate.
"""

SAMPLE_S1_UNDERWRITER = """
PROSPECTUS

D. Boral Capital is acting as the lead underwriter for this offering.
Pursuant to the terms of the Underwriting Agreement, the Company has agreed
to pay D. Boral Capital a discount equal to seven percent (7%) of the gross
proceeds of the offering plus a non-accountable expense allowance.
"""

SAMPLE_FORM_4_BUY_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>Smith John A</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>4.25</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


# ---------------------------------------------------------------------------
# extract_8k_items
# ---------------------------------------------------------------------------
def test_extract_8k_items_finds_two_items():
    result = fp.extract_8k_items(SAMPLE_8K_TWO_ITEMS)
    assert result["items"] == ["2.02", "8.01"]
    assert "FDA breakthrough" in result["item_texts"]["8.01"]
    assert "first-quarter" in result["item_texts"]["2.02"]


def test_extract_8k_items_handles_empty_text():
    assert fp.extract_8k_items("") == {"items": [], "item_texts": {}}


def test_extract_8k_items_dedupes_when_item_appears_twice():
    text = (
        "Item 1.01 Title\nshort body\n"
        "Item 1.01 Title\nthe long fuller version of the body goes here\n"
    )
    result = fp.extract_8k_items(text)
    assert result["items"] == ["1.01"]
    # The longer body wins
    assert "long fuller version" in result["item_texts"]["1.01"]


# ---------------------------------------------------------------------------
# extract_ir_firm
# ---------------------------------------------------------------------------
def test_extract_ir_firm_detects_named_firm_and_compensation():
    result = fp.extract_ir_firm(SAMPLE_8K_IR_FIRM)
    assert result is not None
    assert "Hayden IR" in result["firm_name"]
    assert result["compensation_amount"] == 10000
    assert result["compensation_stock_shares"] == 50000
    assert result["compensation_type"] == "combination"
    assert "investor relations" in result["raw_excerpt"].lower()


def test_extract_ir_firm_returns_none_when_text_lacks_engagement():
    plain = "Item 8.01 Other Events. The Company announced FDA designation."
    assert fp.extract_ir_firm(plain) is None


def test_extract_ir_firm_known_match_flag_set_for_tracked_firm():
    result = fp.extract_ir_firm(SAMPLE_8K_IR_FIRM, known_firms=["Hayden IR"])
    assert result is not None
    # extract_ir_firm normalizes the firm name; the comparison is case-insensitive
    assert result["known_match"] is True


def test_extract_ir_firm_falls_back_to_known_name_when_pattern_misses():
    """Filing mentions a known firm without matching the engagement-pattern phrasing."""
    text = "The Company has retained MZ Group for ongoing communications support."
    result = fp.extract_ir_firm(text, known_firms=["MZ Group"])
    assert result is not None
    assert result["firm_name"].lower() == "mz group"
    assert result["known_match"] is True


def test_extract_ir_firm_handles_empty_text():
    assert fp.extract_ir_firm("") is None


# ---------------------------------------------------------------------------
# extract_reverse_split
# ---------------------------------------------------------------------------
def test_extract_reverse_split_parses_ratio_and_dates():
    result = fp.extract_reverse_split(SAMPLE_8K_REVERSE_SPLIT)
    assert result is not None
    assert result["ratio"] == "1-for-10"
    assert result["effective_date"] is not None
    assert "May 15" in result["effective_date"]
    assert result["pre_split_shares"] == 50_000_000
    assert result["post_split_shares"] == 5_000_000


def test_extract_reverse_split_returns_none_for_non_split_text():
    assert fp.extract_reverse_split(SAMPLE_8K_TWO_ITEMS) is None


def test_extract_reverse_split_alt_phrasing():
    text = "ratio of 1 to 5 reverse split, effective June 1, 2026"
    result = fp.extract_reverse_split(text)
    assert result is not None
    assert result["ratio"] == "1-for-5"


# ---------------------------------------------------------------------------
# extract_underwriter
# ---------------------------------------------------------------------------
def test_extract_underwriter_matches_known_name():
    result = fp.extract_underwriter(
        SAMPLE_S1_UNDERWRITER,
        known_names=["D. Boral Capital", "R.F. Lafferty"],
    )
    assert result == "D. Boral Capital"


def test_extract_underwriter_returns_none_when_no_known_match():
    result = fp.extract_underwriter(
        SAMPLE_S1_UNDERWRITER, known_names=["Goldman Sachs"]
    )
    assert result is None


def test_extract_underwriter_empty_text_returns_none():
    assert fp.extract_underwriter("", known_names=["D. Boral Capital"]) is None


# ---------------------------------------------------------------------------
# extract_insider_context
# ---------------------------------------------------------------------------
def test_extract_insider_context_buy_transaction():
    result = fp.extract_insider_context(SAMPLE_FORM_4_BUY_XML)
    assert result is not None
    assert result["name"] == "Smith John A"
    assert result["relationship"] == "officer"
    assert result["transaction_type"] == "buy"
    assert result["shares"] == 10000.0
    assert result["price"] == 4.25


def test_extract_insider_context_returns_none_for_plain_text():
    assert fp.extract_insider_context("This is just narrative text.") is None


# ---------------------------------------------------------------------------
# fetch_filing_text — mocked SEC-API
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_cache():
    fp._clear_cache()
    yield
    fp._clear_cache()


def _stub_sec_api_module(return_text: str) -> MagicMock:
    if "sec_api" in sys.modules:
        sec_api = sys.modules["sec_api"]
    else:
        sec_api = types.ModuleType("sec_api")
        sys.modules["sec_api"] = sec_api

    render_instance = MagicMock()
    render_instance.get_filing = MagicMock(return_value=return_text)
    render_class = MagicMock(return_value=render_instance)
    sec_api.RenderApi = render_class
    return render_instance


@pytest.mark.asyncio
async def test_fetch_filing_text_returns_empty_when_api_key_missing(monkeypatch):
    monkeypatch.setattr(fp.settings, "SEC_API_KEY", "")
    text = await fp.fetch_filing_text("0000000000-26-000001", link="https://x")
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_filing_text_calls_sec_api_and_caches(monkeypatch):
    monkeypatch.setattr(fp.settings, "SEC_API_KEY", "test-key")
    render = _stub_sec_api_module("<html>filing body</html>")

    text1 = await fp.fetch_filing_text("ACC-1", link="https://example/filing")
    text2 = await fp.fetch_filing_text("ACC-1", link="https://example/filing")
    assert text1 == "<html>filing body</html>"
    assert text2 == text1
    # cache hit on the second call → only one underlying API invocation
    assert render.get_filing.call_count == 1


@pytest.mark.asyncio
async def test_fetch_filing_text_returns_empty_when_link_missing(monkeypatch):
    monkeypatch.setattr(fp.settings, "SEC_API_KEY", "test-key")
    text = await fp.fetch_filing_text("ACC-2", link=None)
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_filing_text_swallows_api_exceptions(monkeypatch):
    monkeypatch.setattr(fp.settings, "SEC_API_KEY", "test-key")

    if "sec_api" in sys.modules:
        sec_api = sys.modules["sec_api"]
    else:
        sec_api = types.ModuleType("sec_api")
        sys.modules["sec_api"] = sec_api

    render_instance = MagicMock()
    render_instance.get_filing = MagicMock(side_effect=ConnectionError("boom"))
    sec_api.RenderApi = MagicMock(return_value=render_instance)

    text = await fp.fetch_filing_text("ACC-3", link="https://example/x")
    assert text == ""
