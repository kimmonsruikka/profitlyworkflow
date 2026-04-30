"""Re-run the 8-K extractor against existing sec_filings stubs.

These are rows that the watcher inserted before the filing parser
existed (or before SEC_API_KEY was set), so they're marked
processed=True but their item_numbers / ir_firm_mentioned /
underwriter_id columns are still empty. This script walks them in
order and re-invokes the same async pipeline the Celery task uses,
so the extraction logic stays in one place.

Run on the droplet:

    sudo -u trading /app/profitlyworkflow/venv/bin/python \\
        /app/profitlyworkflow/scripts/reprocess_filings.py

Idempotent — safe to re-run; rows that successfully populated on a
prior pass no longer match the "stale stub" filter.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env.production BEFORE any imports that touch config.settings.
# Operator scripts run via `sudo -u trading python scripts/foo.py` don't
# inherit shell env vars (sudo strips them), so we have to load the file
# explicitly here. override=True so values in the file beat any stale
# os.environ leftovers.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env.production"
if env_file.exists():
    load_dotenv(env_file, override=True)

from loguru import logger  # noqa: E402
from sqlalchemy import func, or_, select  # noqa: E402

from data.db import get_session  # noqa: E402
from data.models.sec_filing import SecFiling  # noqa: E402
from ingestion.edgar.rss_watcher import _process_filing_async  # noqa: E402


def _reconstruct_archive_link(cik: str | None, accession: str) -> str | None:
    """EDGAR's filing-index URL is deterministic from CIK + accession.

    Example:  cik=320193, accession=0000320193-26-000001
              -> https://www.sec.gov/Archives/edgar/data/320193/
                   000032019326000001/0000320193-26-000001-index.htm
    """
    if not cik or not accession:
        return None
    cik_int = int(cik)  # strip leading zeros
    acc_no_dashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{acc_no_dashes}/{accession}-index.htm"
    )


async def _load_stale_stubs() -> list[SecFiling]:
    """Filings that look like un-extracted 8-K stubs."""
    async with get_session() as session:
        stmt = (
            select(SecFiling)
            .where(SecFiling.processed.is_(True))
            .where(
                or_(
                    SecFiling.item_numbers.is_(None),
                    func.jsonb_array_length(SecFiling.item_numbers) == 0,
                )
            )
            .order_by(SecFiling.filed_at)
        )
        return list((await session.execute(stmt)).scalars().all())


async def main() -> int:
    rows = await _load_stale_stubs()
    total = len(rows)
    if total == 0:
        print("No stale filings to reprocess — exiting.")
        return 0

    print(f"Reprocessing {total} stale filings...")

    ir_count = 0
    rs_count = 0
    uw_count = 0
    no_text = 0
    errors = 0

    for idx, row in enumerate(rows, start=1):
        payload = {
            "accession_number": row.accession_number,
            "form_type": row.form_type,
            "cik": row.cik,
            "link": _reconstruct_archive_link(row.cik, row.accession_number or ""),
        }
        try:
            findings = await _process_filing_async(payload)
        except Exception:
            logger.exception("reprocess failed for {}", row.accession_number)
            errors += 1
            continue

        if findings.get("status") == "no_text":
            no_text += 1
        if findings.get("ir_firm"):
            ir_count += 1
        if findings.get("reverse_split"):
            rs_count += 1
        if findings.get("underwriter"):
            uw_count += 1

        if idx % 10 == 0 or idx == total:
            print(f"  [{idx:>4}/{total}] last={row.accession_number} form={row.form_type}")

    print()
    print("Reprocess complete.")
    print(f"  total reprocessed:     {total}")
    print(f"  IR firms found:        {ir_count}")
    print(f"  reverse splits found:  {rs_count}")
    print(f"  underwriters found:    {uw_count}")
    print(f"  empty text (no fetch): {no_text}")
    print(f"  errors:                {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
