from __future__ import annotations

from ingestion.edgar.cik_universe import _normalize_cik, _is_small_exchange
from ingestion.edgar.rss_watcher import _build_rss_url, parse_atom_feed


SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Latest Filings</title>
  <entry>
    <title>8-K - APPLE INC (0000320193) (Filer)</title>
    <updated>2026-04-29T10:30:00-04:00</updated>
    <link rel="alternate" type="text/html"
          href="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm"/>
  </entry>
  <entry>
    <title>S-3 - SOMECO LTD (0001234567) (Filer)</title>
    <updated>2026-04-29T11:00:00-04:00</updated>
    <link rel="alternate" type="text/html"
          href="https://www.sec.gov/Archives/edgar/data/1234567/000123456724000045/0001234567-24-000045-index.htm"/>
  </entry>
  <entry>
    <title>malformed entry without parens</title>
    <updated>2026-04-29T11:30:00-04:00</updated>
  </entry>
</feed>
"""


def test_normalize_cik_pads_to_ten_digits() -> None:
    assert _normalize_cik(320193) == "0000320193"
    assert _normalize_cik("320193") == "0000320193"
    assert _normalize_cik("0000320193") == "0000320193"
    assert _normalize_cik(None) is None
    assert _normalize_cik("") is None


def test_is_small_exchange_matches_targets_case_insensitive() -> None:
    assert _is_small_exchange("OTC") is True
    assert _is_small_exchange("otc") is True
    assert _is_small_exchange("NYSE MKT") is True
    assert _is_small_exchange("Nasdaq") is False
    assert _is_small_exchange(None) is False


def test_build_rss_url_includes_form_and_count() -> None:
    url = _build_rss_url("8-K")
    assert "type=8-K" in url
    assert "action=getcurrent" in url
    assert "output=atom" in url


def test_parse_atom_feed_extracts_filings() -> None:
    filings = parse_atom_feed(SAMPLE_FEED)
    # malformed entry should be skipped, two valid ones remain
    assert len(filings) == 2

    apple = filings[0]
    assert apple["form_type"] == "8-K"
    assert apple["company_name"] == "APPLE INC"
    assert apple["cik"] == "0000320193"
    assert apple["accession_number"] == "0000320193-24-000123"
    assert apple["filed_at"] is not None
    assert apple["filed_at"].tzinfo is not None  # tz-aware

    someco = filings[1]
    assert someco["form_type"] == "S-3"
    assert someco["cik"] == "0001234567"
    assert someco["accession_number"] == "0001234567-24-000045"


def test_parse_atom_feed_handles_garbage() -> None:
    assert parse_atom_feed("not xml at all") == []
    assert parse_atom_feed("") == []
