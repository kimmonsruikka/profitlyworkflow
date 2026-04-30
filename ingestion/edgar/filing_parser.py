"""Full-text parser for SEC filings.

Two responsibilities:

  1. Fetch raw filing text via SEC-API.io's RenderApi (with an in-process
     cache so re-processing the same accession doesn't burn API quota).
  2. Extract structured signals from that text — 8-K item numbers and
     section bodies, IR firm engagements, reverse-split ratios,
     underwriter names, and Form-4 insider transactions.

The extractors are pure (text in, dict out) so they're easy to unit test
with sample fixtures. The fetcher is async and isolated so callers can
substitute a fake in tests.

Note: SEC_API_KEY must be set in .env.production before fetch_filing_text
will return real text. Without it the fetcher logs a warning and returns
an empty string; extractors handle the empty case gracefully.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Iterable

from loguru import logger

from config.settings import settings


# ---------------------------------------------------------------------------
# fetch_filing_text — SEC-API.io RenderApi wrapper with in-process cache
# ---------------------------------------------------------------------------
_TEXT_CACHE: dict[str, str] = {}


async def fetch_filing_text(
    accession_number: str,
    link: str | None = None,
) -> str:
    """Return the full plain-text body of the filing.

    Caches by accession_number so re-processing during the same Celery
    worker process never re-hits the API. `link` is the EDGAR filing-index
    URL from the RSS feed; SEC-API needs it (or a derived ArchiveLink) to
    fetch the document.
    """
    cached = _TEXT_CACHE.get(accession_number)
    if cached is not None:
        return cached

    if not settings.SEC_API_KEY:
        logger.warning(
            "fetch_filing_text({}): SEC_API_KEY not set — returning empty text",
            accession_number,
        )
        _TEXT_CACHE[accession_number] = ""
        return ""

    if not link:
        logger.warning(
            "fetch_filing_text({}): no filing link supplied", accession_number,
        )
        _TEXT_CACHE[accession_number] = ""
        return ""

    try:
        from sec_api import RenderApi

        render = RenderApi(api_key=settings.SEC_API_KEY)
        text = await asyncio.to_thread(render.get_filing, link)
    except Exception:
        logger.exception("SEC-API fetch failed for {}", accession_number)
        return ""

    text = text or ""
    _TEXT_CACHE[accession_number] = text
    return text


def _clear_cache() -> None:
    """Test hook — reset the module cache between cases."""
    _TEXT_CACHE.clear()


# ---------------------------------------------------------------------------
# 8-K item extractor
# ---------------------------------------------------------------------------
# Matches "Item 1.01", "Item 8.01.", "Item 5.03 — Title…" at the start of a
# line. The lookahead bounds the section text at the next item or end-of-text.
_8K_ITEM_RE = re.compile(
    r"^[\s\xa0]*Item\s+(\d+\.\d{2})\s*[\.\-—:]?\s*(.*?)(?=^[\s\xa0]*Item\s+\d+\.\d{2}|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


def extract_8k_items(text: str) -> dict:
    """Parse 8-K text into {items: [...], item_texts: {item: body}}.

    Returns empty structure when text is empty or contains no items.
    Item numbers come back as strings ("1.01", "5.03") to match the
    sec_filings.item_numbers JSONB shape.
    """
    if not text:
        return {"items": [], "item_texts": {}}

    items: list[str] = []
    item_texts: dict[str, str] = {}
    for match in _8K_ITEM_RE.finditer(text):
        item_num = match.group(1)
        body = match.group(2).strip()
        # An item may appear twice (TOC + body); keep the longer body.
        existing = item_texts.get(item_num, "")
        if len(body) > len(existing):
            item_texts[item_num] = body
        if item_num not in items:
            items.append(item_num)

    return {"items": items, "item_texts": item_texts}


# ---------------------------------------------------------------------------
# IR / PR firm extractor
# ---------------------------------------------------------------------------
# "entered into [an] investor relations [consulting] agreement with [Firm]"
_IR_AGREEMENT_RE = re.compile(
    r"entered\s+into\s+(?:an?\s+)?(?:[\w\s\-]{0,40})?"
    r"(?:investor\s+relations?|public\s+relations?|IR|PR)\s+"
    r"(?:[\w\s\-]{0,40})?"
    r"(?:agreement|consulting\s+agreement|engagement|services\s+agreement)\s+"
    r"with\s+([A-Z][A-Za-z0-9&\.,\s\-\']{2,80}?)(?:[,\.\(]|\s+(?:to|pursuant|whereby|in\s+exchange))",
    re.IGNORECASE | re.DOTALL,
)
# Bare "engaged Firm Name as our investor relations" (alternate phrasing)
_IR_ENGAGED_RE = re.compile(
    r"engaged\s+([A-Z][A-Za-z0-9&\.,\s\-\']{2,80}?)\s+"
    r"(?:as|to\s+(?:provide|act))\s+(?:[\w\s,]{0,40})?"
    r"(?:investor\s+relations?|public\s+relations?|IR|PR)",
    re.IGNORECASE | re.DOTALL,
)
# Generic dollar-amount near "fee" / "compensation" — e.g. "fee of $10,000"
_DOLLAR_NEAR_FEE_RE = re.compile(
    r"(?:monthly\s+|annual\s+|one[\-\s]time\s+)?(?:fee|compensation|retainer)\s+"
    r"(?:of\s+)?\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_STOCK_COMPENSATION_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*)\s+shares?\s+of\s+(?:restricted\s+)?common\s+stock",
    re.IGNORECASE,
)


def _normalize_firm_name(name: str) -> str:
    """Trim trailing legal-entity suffixes and stray punctuation."""
    cleaned = name.strip().rstrip(",.;:")
    return re.sub(r"\s+", " ", cleaned)


def extract_ir_firm(
    text: str,
    known_firms: Iterable[str] | None = None,
) -> dict | None:
    """Detect IR/PR firm engagement. Returns None if nothing found.

    If `known_firms` is supplied, the result also flags `known_match=True`
    when the extracted name matches one of them (case-insensitive). That
    lets the Celery task increase confidence on filings that name a
    promoter we already track.
    """
    if not text:
        return None

    known_lower = {f.strip().lower() for f in (known_firms or []) if f and f.strip()}

    firm_name: str | None = None
    raw_excerpt: str | None = None

    for pattern in (_IR_AGREEMENT_RE, _IR_ENGAGED_RE):
        m = pattern.search(text)
        if m:
            firm_name = _normalize_firm_name(m.group(1))
            # 200-char excerpt centered on the match
            start = max(0, m.start() - 60)
            end = min(len(text), m.end() + 100)
            raw_excerpt = text[start:end].replace("\n", " ").strip()
            break

    # Fallback: any known firm name appearing anywhere in the text.
    if firm_name is None and known_lower:
        for known in known_lower:
            if known in text.lower():
                firm_name = known
                idx = text.lower().find(known)
                raw_excerpt = text[max(0, idx - 60): idx + 200].replace("\n", " ").strip()
                break

    if firm_name is None:
        return None

    cash = None
    fee_match = _DOLLAR_NEAR_FEE_RE.search(text)
    if fee_match:
        try:
            cash = int(fee_match.group(1).replace(",", "").split(".")[0])
        except (ValueError, IndexError):
            cash = None

    stock_shares = None
    stock_match = _STOCK_COMPENSATION_RE.search(text)
    if stock_match:
        try:
            stock_shares = int(stock_match.group(1).replace(",", ""))
        except ValueError:
            stock_shares = None

    if cash is not None and stock_shares is not None:
        comp_type = "combination"
    elif stock_shares is not None:
        comp_type = "stock"
    elif cash is not None:
        comp_type = "cash"
    else:
        comp_type = None

    return {
        "firm_name": firm_name,
        "compensation_amount": cash,
        "compensation_stock_shares": stock_shares,
        "compensation_type": comp_type,
        "raw_excerpt": raw_excerpt,
        "known_match": firm_name.lower() in known_lower if known_lower else False,
    }


# ---------------------------------------------------------------------------
# Reverse-split extractor
# ---------------------------------------------------------------------------
# "1-for-10 reverse stock split" / "1:10 reverse split" / "1 for 10" / "1 to 10"
# Whitespace handling lives inside each alternative so a greedy \s* on the
# outside doesn't eat the space the alternatives need.
_RATIO_RE = re.compile(
    r"(?:ratio\s+of\s+)?"
    r"(\d{1,3})(?:-for-|\s+for\s+|\s+to\s+|\s*[-:]\s*)(\d{1,3})"
    r"\s+reverse\s+(?:stock\s+)?split",
    re.IGNORECASE,
)
_EFFECTIVE_DATE_RE = re.compile(
    r"effective(?:\s+(?:as\s+)?of)?\s+"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_SHARE_COUNT_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*)\s+shares?\s+(?:of\s+)?(?:common\s+stock\s+)?"
    r"(?:outstanding|issued)",
    re.IGNORECASE,
)


def extract_reverse_split(text: str) -> dict | None:
    """Extract reverse-split announcement details. None if not present."""
    if not text:
        return None

    ratio_match = _RATIO_RE.search(text)
    if not ratio_match:
        return None

    n, m = ratio_match.group(1), ratio_match.group(2)
    ratio = f"{n}-for-{m}"

    effective_date = None
    date_match = _EFFECTIVE_DATE_RE.search(text)
    if date_match:
        effective_date = date_match.group(1).strip().rstrip(",")

    pre_split = None
    share_match = _SHARE_COUNT_RE.search(text)
    if share_match:
        try:
            pre_split = int(share_match.group(1).replace(",", ""))
        except ValueError:
            pre_split = None

    post_split = None
    if pre_split is not None:
        try:
            post_split = pre_split * int(n) // int(m)
        except (ValueError, ZeroDivisionError):
            post_split = None

    return {
        "ratio": ratio,
        "effective_date": effective_date,
        "pre_split_shares": pre_split,
        "post_split_shares": post_split,
    }


# ---------------------------------------------------------------------------
# Underwriter extractor — string match against known names
# ---------------------------------------------------------------------------
def extract_underwriter(
    text: str, known_names: Iterable[str]
) -> str | None:
    """Return the first known underwriter name found in the text, else None.

    String-match by case-insensitive substring against the supplied names.
    Caller is responsible for pulling the `underwriters` table contents.
    """
    if not text or not known_names:
        return None
    lower = text.lower()
    for name in known_names:
        if not name:
            continue
        if name.strip().lower() in lower:
            return name
    return None


# ---------------------------------------------------------------------------
# Form 4 insider transaction extractor
# ---------------------------------------------------------------------------
# Form 4 is XML-structured. We look for the standard derivative/non-derivative
# transaction blocks. This is a coarse extraction good enough for "who bought
# vs sold and how much"; full XML parsing is a refinement task.
_F4_NAME_RE = re.compile(r"<rptOwnerName>\s*([^<]+?)\s*</rptOwnerName>", re.IGNORECASE)
# Form 4 ships isOfficer / isDirector / isTenPercentOwner side-by-side, often
# with the false ones first. We have to scan every flag and pick the one
# that's set; matching only the first one would falsely report None.
_F4_RELATIONSHIP_FLAG_RE = re.compile(
    r"<is(Officer|Director|TenPercentOwner)>\s*(\d)\s*</is\1>",
    re.IGNORECASE,
)
_F4_TXN_CODE_RE = re.compile(
    r"<transactionCode>\s*([A-Z])\s*</transactionCode>", re.IGNORECASE,
)
_F4_SHARES_RE = re.compile(
    r"<transactionShares>\s*<value>\s*([\d.,]+)\s*</value>", re.IGNORECASE | re.DOTALL,
)
_F4_PRICE_RE = re.compile(
    r"<transactionPricePerShare>\s*<value>\s*([\d.,]+)\s*</value>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class InsiderTransaction:
    name: str | None
    relationship: str | None
    transaction_type: str | None  # "buy" / "sell" / "other"
    shares: float | None
    price: float | None


def _txn_code_to_type(code: str | None) -> str | None:
    if not code:
        return None
    # SEC Form 4 transaction codes — full table at
    # https://www.sec.gov/about/forms/form4data.pdf
    if code.upper() in {"P", "A", "L"}:  # P = open-market buy, A = grant, L = small acquisition
        return "buy"
    if code.upper() in {"S", "D"}:       # S = open-market sell, D = sale to issuer
        return "sell"
    return "other"


def extract_insider_context(text: str) -> dict | None:
    """Pull buyer/seller/share/price info from Form 4 XML body."""
    if not text:
        return None

    name_m = _F4_NAME_RE.search(text)
    code_m = _F4_TXN_CODE_RE.search(text)
    shares_m = _F4_SHARES_RE.search(text)
    price_m = _F4_PRICE_RE.search(text)
    if not (name_m or code_m or shares_m):
        return None

    relationship = None
    for flag in _F4_RELATIONSHIP_FLAG_RE.finditer(text):
        if flag.group(2) == "1":
            tag = flag.group(1).lower()
            if tag == "tenpercentowner":
                relationship = "ten_percent_owner"
            else:
                relationship = tag  # "officer" or "director"
            break

    def _num(match) -> float | None:
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None

    return {
        "name": name_m.group(1).strip() if name_m else None,
        "relationship": relationship,
        "transaction_type": _txn_code_to_type(code_m.group(1) if code_m else None),
        "shares": _num(shares_m),
        "price": _num(price_m),
    }
