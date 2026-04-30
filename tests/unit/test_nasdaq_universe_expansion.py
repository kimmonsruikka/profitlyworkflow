from __future__ import annotations

from config import constants
from ingestion.edgar.rss_watcher import _build_rss_url


def test_nasdaq_in_universe_exchanges() -> None:
    """Nasdaq must be a default-seeded exchange now."""
    assert "Nasdaq" in constants.EDGAR_SMALL_EXCHANGES


def test_universe_target_size_accommodates_nasdaq() -> None:
    """Target must be large enough to fit Nasdaq's ~3.5k listings + small caps."""
    assert constants.EDGAR_UNIVERSE_TARGET_SIZE >= 4000


def test_priority_forms_include_phase_1_additions() -> None:
    forms = constants.EDGAR_PRIORITY_FORMS
    for required in ("8-K", "S-1", "S-3", "4", "DEF 14A", "SC 13G", "NT 10-K", "NT 10-Q"):
        assert required in forms, f"missing form {required}"


def test_8k_priority_items_include_phase_1_additions() -> None:
    items = constants.EDGAR_8K_PRIORITY_ITEMS
    for required in ("8.01", "2.02", "5.03", "3.02", "1.01", "7.01"):
        assert required in items, f"missing 8-K item {required}"


def test_build_rss_url_encodes_spaces() -> None:
    """EDGAR's getcurrent endpoint requires URL-encoded form types."""
    url = _build_rss_url("DEF 14A")
    assert "DEF%2014A" in url
    assert "DEF 14A" not in url  # raw space would break the request

    url = _build_rss_url("NT 10-K")
    assert "NT%2010-K" in url


def test_build_rss_url_passes_simple_forms_unchanged() -> None:
    """Forms without special chars should not be mangled."""
    url = _build_rss_url("8-K")
    assert "type=8-K" in url


def test_underwriter_model_registers_on_metadata() -> None:
    from data.models import Base, Underwriter

    assert Underwriter.__tablename__ == "underwriters"
    assert "underwriters" in Base.metadata.tables
    cols = {c.name for c in Underwriter.__table__.columns}
    assert {
        "underwriter_id", "name", "type", "first_seen_edgar",
        "ncm_listing_count", "manipulation_flagged", "flag_source",
        "notes", "created_at",
    } <= cols


def test_sec_filings_has_underwriter_fk() -> None:
    from data.models import SecFiling

    cols = {c.name for c in SecFiling.__table__.columns}
    assert "underwriter_id" in cols
    fks = list(SecFiling.__table__.foreign_keys)
    assert any(fk.target_fullname == "underwriters.underwriter_id" for fk in fks)


def test_underwriter_schema_round_trips() -> None:
    """Pydantic schema should accept the ORM-equivalent payload."""
    import uuid as uuidmod
    from data.repositories.schemas import UnderwriterSchema

    s = UnderwriterSchema(
        underwriter_id=uuidmod.uuid4(),
        name="D. Boral Capital",
        type="underwriter",
        manipulation_flagged=True,
        flag_source="bloomberg",
    )
    assert s.manipulation_flagged is True
    assert s.flag_source == "bloomberg"
    assert s.ncm_listing_count == 0  # default


def test_migration_0005_seeds_four_flagged_underwriters() -> None:
    """The migration's seed SQL must cover the four Bloomberg-named names."""
    from pathlib import Path

    mig = Path("data/schema/migrations/versions/0005_add_underwriters_and_filings_fk.py").read_text()
    for name in ("D. Boral Capital", "R.F. Lafferty", "Dominari Securities", "Revere Securities"):
        assert name in mig, f"migration 0005 is missing seed entry: {name}"
    assert "manipulation_flagged" in mig
    assert "bloomberg" in mig
