"""EDGAR RSS watcher.

Polls SEC EDGAR for new filings every EDGAR_POLL_INTERVAL_MINUTES, weekdays
between EDGAR_POLL_HOUR_START_ET and EDGAR_POLL_HOUR_END_ET. For each priority
form type (8-K, S-1, S-3, Form 4) we fetch the latest-filings RSS, filter
client-side to our CIK universe, dedupe against sec_filings.accession_number,
insert a stub row, and enqueue a Celery task for downstream parsing.

Run the scheduler with:
    python -m ingestion.edgar.rss_watcher
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import constants
from config.settings import settings
from data.db import get_session
from data.models.sec_filing import SecFiling
from data.models.ticker import Ticker
from flows.celery_app import celery_app
from ingestion.edgar.cik_universe import _normalize_cik, load_universe_ciks


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# Title format: "8-K - APPLE INC (0000320193) (Filer)"
_TITLE_RE = re.compile(r"^([\w\-/]+)\s*-\s*(.+?)\s*\((\d{6,10})\)")
# Accession in updated/id link: e.g. /Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm
_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def _sec_headers() -> dict[str, str]:
    user_agent = settings.SEC_USER_AGENT or "trading-intelligence-system contact@example.com"
    return {"User-Agent": user_agent, "Accept": "application/atom+xml"}


def _build_rss_url(form_type: str) -> str:
    """EDGAR's getcurrent endpoint returns an Atom feed of recent filings.

    Form types like 'DEF 14A' and 'NT 10-K' contain spaces; quote() turns
    them into 'DEF%2014A' / 'NT%2010-K' which EDGAR's getcurrent accepts.
    """
    encoded_form = urllib.parse.quote(form_type)
    return (
        f"{constants.EDGAR_RSS_URL}"
        f"?action=getcurrent&type={encoded_form}"
        f"&company=&dateb=&owner=include"
        f"&count={constants.EDGAR_RSS_FETCH_COUNT}&output=atom"
    )


def parse_atom_feed(xml_text: str) -> list[dict]:
    """Extract filings from an EDGAR Atom feed.

    Returns dicts with: accession_number, form_type, company_name, cik,
    filed_at, link.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("EDGAR feed parse failed: {}", exc)
        return []

    out: list[dict] = []
    for entry in root.findall("a:entry", _ATOM_NS):
        title_el = entry.find("a:title", _ATOM_NS)
        updated_el = entry.find("a:updated", _ATOM_NS)
        link_el = entry.find("a:link", _ATOM_NS)
        if title_el is None or title_el.text is None:
            continue

        match = _TITLE_RE.match(title_el.text.strip())
        if not match:
            continue
        form_type = match.group(1).strip()
        company = match.group(2).strip()
        cik = _normalize_cik(match.group(3))

        link = link_el.get("href") if link_el is not None else None
        accession = None
        if link:
            acc_match = _ACCESSION_RE.search(link)
            if acc_match:
                accession = acc_match.group(1)

        filed_at: datetime | None = None
        if updated_el is not None and updated_el.text:
            try:
                filed_at = datetime.fromisoformat(updated_el.text.replace("Z", "+00:00"))
            except ValueError:
                filed_at = None

        out.append({
            "accession_number": accession,
            "form_type": form_type,
            "company_name": company,
            "cik": cik,
            "filed_at": filed_at,
            "link": link,
        })
    return out


# ---------------------------------------------------------------------------
# Fetch + filter + persist
# ---------------------------------------------------------------------------
async def fetch_form_feed(client: httpx.AsyncClient, form_type: str) -> list[dict]:
    url = _build_rss_url(form_type)
    last_error: Exception | None = None
    for attempt in range(constants.EDGAR_HTTP_RETRY):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return parse_atom_feed(resp.text)
        except httpx.HTTPError as exc:
            last_error = exc
            await asyncio.sleep(2 ** attempt)
    logger.error("EDGAR fetch {} failed after retries: {}", form_type, last_error)
    return []


async def _existing_accessions(
    session: AsyncSession, accessions: Iterable[str]
) -> set[str]:
    accs = [a for a in accessions if a]
    if not accs:
        return set()
    stmt = select(SecFiling.accession_number).where(SecFiling.accession_number.in_(accs))
    rows = (await session.execute(stmt)).scalars().all()
    return {a for a in rows if a}


async def _ticker_for_ciks(
    session: AsyncSession, ciks: Iterable[str]
) -> dict[str, str]:
    """Batch resolve CIKs to ticker symbols via the tickers table.

    Returns a mapping {cik: ticker}. CIKs without a tickers-table row are
    omitted from the result; callers should fall back to None.
    """
    cik_list = [c for c in {c for c in ciks if c}]
    if not cik_list:
        return {}
    stmt = select(Ticker.cik, Ticker.ticker).where(Ticker.cik.in_(cik_list))
    rows = (await session.execute(stmt)).all()
    return {cik: ticker for cik, ticker in rows if cik and ticker}


async def persist_and_queue(session: AsyncSession, filings: list[dict]) -> int:
    """Insert new filings and enqueue Celery tasks. Returns count enqueued."""
    if not filings:
        return 0
    seen = await _existing_accessions(
        session, (f["accession_number"] for f in filings if f.get("accession_number"))
    )
    cik_to_ticker = await _ticker_for_ciks(
        session, (f["cik"] for f in filings if f.get("cik"))
    )

    queued = 0
    for f in filings:
        acc = f.get("accession_number")
        if not acc or acc in seen:
            continue
        if f.get("filed_at") is None or f.get("form_type") is None:
            continue

        cik = f.get("cik")
        ticker = cik_to_ticker.get(cik) if cik else None
        if cik and ticker is None:
            logger.debug(
                "no ticker for cik={cik} on filing {acc} ({form}) — storing NULL",
                cik=cik, acc=acc, form=f.get("form_type"),
            )

        stmt = (
            pg_insert(SecFiling)
            .values(
                ticker=ticker,
                cik=cik,
                filed_at=f["filed_at"],
                form_type=f["form_type"],
                accession_number=acc,
                processed=False,
            )
            .on_conflict_do_nothing(index_elements=[SecFiling.accession_number])
        )
        await session.execute(stmt)

        process_filing.delay({
            "accession_number": acc,
            "cik": f["cik"],
            "form_type": f["form_type"],
            "company_name": f.get("company_name"),
            "link": f.get("link"),
            "filed_at": f["filed_at"].isoformat(),
        })
        queued += 1

    await session.flush()
    return queued


async def poll_once() -> dict[str, int]:
    """One polling cycle: fetch every priority form, filter, persist, queue.

    Returns a per-form count dict for logging/monitoring.
    """
    async with get_session() as session:
        universe = await load_universe_ciks(session)
        if not universe:
            logger.warning("poll_once: empty CIK universe — seed_universe never run?")
            return {}

        stats: dict[str, int] = {}
        async with httpx.AsyncClient(
            timeout=constants.EDGAR_HTTP_TIMEOUT_SECONDS,
            headers=_sec_headers(),
        ) as client:
            for form in constants.EDGAR_PRIORITY_FORMS:
                feed = await fetch_form_feed(client, form)
                in_universe = [f for f in feed if f.get("cik") and f["cik"] in universe]
                queued = await persist_and_queue(session, in_universe)
                stats[form] = queued
                logger.info(
                    "poll {form}: feed={n} in_universe={u} queued={q}",
                    form=form, n=len(feed), u=len(in_universe), q=queued,
                )
        return stats


# ---------------------------------------------------------------------------
# Celery task — fetch filing text, run extractors, persist findings
# ---------------------------------------------------------------------------
@celery_app.task(name="ingestion.edgar.process_filing", bind=True, max_retries=3)
def process_filing(self, payload: dict) -> dict:
    """Fetch the filing body, run form-appropriate extractors, update the
    sec_filings row, and upsert any new IR firms or underwriters we found.

    Runs the async pipeline in a fresh event loop per task — Celery's
    prefork worker model isolates each task in its own process so this is
    safe.
    """
    return asyncio.run(_process_filing_async(payload))


async def _process_filing_async(payload: dict) -> dict:
    from sqlalchemy import select as _select
    from sqlalchemy import update as _update

    from data.db import session_from, task_local_session_factory
    from data.models.promoter_entity import PromoterEntity
    from data.models.underwriter import Underwriter
    from data.repositories.promoter_repo import PromoterRepository
    from ingestion.edgar import filing_parser

    accession = payload.get("accession_number")
    form_type = payload.get("form_type") or ""
    link = payload.get("link")

    if not accession:
        logger.warning("process_filing: missing accession_number, skipping")
        return {"status": "skipped", "reason": "no_accession"}

    text = await filing_parser.fetch_filing_text(accession, link=link)

    # Single task-local engine for the whole task body. Disposed in the
    # finally branch of task_local_session_factory so every Celery task
    # gets a fresh connection on its own asyncio.run() loop. The
    # module-level `engine` is NOT used inside Celery — its event-loop
    # binding outlives a single asyncio.run() and produces
    #   RuntimeError: got Future attached to a different loop
    # on the second task. See data.db.task_local_session_factory.
    async with task_local_session_factory() as factory:
        if not text:
            logger.info(
                "process_filing {acc}: empty text — marking processed without extraction",
                acc=accession,
            )
            async with session_from(factory) as session:
                await session.execute(
                    _update(SecFiling)
                    .where(SecFiling.accession_number == accession)
                    .values(processed=True)
                )
            return {"status": "no_text", "accession": accession}

        update_values: dict = {"processed": True}
        findings: dict = {"accession": accession, "form": form_type}

        async with session_from(factory) as session:
            # Pull lookup tables for the cross-references the extractors need.
            ir_firms = (
                await session.execute(
                    _select(PromoterEntity.name).where(PromoterEntity.type == "ir_firm")
                )
            ).scalars().all()
            underwriter_rows = (
                await session.execute(
                    _select(Underwriter.underwriter_id, Underwriter.name)
                )
            ).all()
            underwriter_by_lower_name = {
                (row.name or "").strip().lower(): row.underwriter_id
                for row in underwriter_rows
            }
    
            # ---- 8-K branch
            if form_type.upper().startswith("8-K"):
                items = filing_parser.extract_8k_items(text)
                update_values["item_numbers"] = items["items"]
                update_values["full_text"] = {
                    "items": items["items"],
                    "item_texts": items["item_texts"],
                }
                findings["items"] = items["items"]
    
                ir = filing_parser.extract_ir_firm(text, known_firms=ir_firms)
                if ir:
                    update_values["ir_firm_mentioned"] = ir["firm_name"]
                    update_values["compensation_disclosed"] = (
                        ir["compensation_amount"] is not None
                        or ir["compensation_stock_shares"] is not None
                    )
                    if ir["compensation_amount"] is not None:
                        update_values["compensation_amount"] = ir["compensation_amount"]
                    findings["ir_firm"] = ir["firm_name"]
                    findings["ir_compensation"] = ir["compensation_amount"]
    
                    # Upsert into promoter_entities so the network graph grows
                    # automatically as we discover new IR firms.
                    if not ir["known_match"]:
                        repo = PromoterRepository(session)
                        await repo.upsert_entity({
                            "name": ir["firm_name"],
                            "type": "ir_firm",
                            "first_seen_edgar": payload.get("filed_at_dt") or None,
                            "notes": f"Discovered via filing {accession}",
                        })
    
                rs = filing_parser.extract_reverse_split(text)
                if rs:
                    # Reverse split signals live in full_text alongside item bodies.
                    update_values["full_text"] = {
                        **update_values.get("full_text", {}),
                        "reverse_split": rs,
                    }
                    findings["reverse_split"] = rs["ratio"]
    
            # ---- S-1 / S-3 / 424B-series branch
            elif form_type.upper().startswith(("S-1", "S-3", "424B")):
                uw = filing_parser.extract_underwriter(
                    text, known_names=[r.name for r in underwriter_rows]
                )
                if uw:
                    uw_id = underwriter_by_lower_name.get(uw.strip().lower())
                    if uw_id is not None:
                        update_values["underwriter_id"] = uw_id
                        findings["underwriter"] = uw
    
                if form_type.upper().startswith("S-3"):
                    update_values["s3_effective"] = True
                    findings["s3_effective"] = True
    
            # ---- Form 4 branch
            elif form_type.strip() == "4":
                insider = filing_parser.extract_insider_context(text)
                if insider:
                    if insider.get("transaction_type") == "buy":
                        update_values["form4_insider_buy"] = True
                    update_values["full_text"] = {"insider": insider}
                    findings["insider"] = {
                        "type": insider.get("transaction_type"),
                        "shares": insider.get("shares"),
                        "relationship": insider.get("relationship"),
                    }
    
            # Persist whatever we extracted.
            # DIAGNOSTIC: temporary logging to investigate Bug B
            logger.info(
                "update_values pre-UPDATE for {acc}: {vals}",
                acc=accession,
                vals=update_values,
            )
            result = await session.execute(
                _update(SecFiling)
                .where(SecFiling.accession_number == accession)
                .values(**update_values)
            )
            logger.info(
                "UPDATE rowcount for {acc}: {rc}",
                acc=accession,
                rc=result.rowcount,
            )
            # Signal evaluation snapshot — built inside this session so we
            # see the just-written values, but the eval itself runs in a
            # FRESH session below so a signal-eval failure doesn't roll
            # back the filing update.
            signal_payload = await _build_signal_payload(
                session, accession, payload, update_values, findings
            )
    
        # Filing-persistence transaction committed when session_from()
        # exited cleanly above. Signal evaluation runs in a FRESH session
        # from the SAME task-local factory — best-effort, errors logged
        # and swallowed at this boundary so a noisy scorer can't make
        # filings look unprocessed.
        if signal_payload is not None:
            try:
                from signals.engine import SignalEngine
                from signals.scoring.catalyst_scorer import RulesV1Scorer

                async with session_from(factory) as eval_session:
                    engine = SignalEngine(scorer=RulesV1Scorer(), session=eval_session)
                    prediction = await engine.evaluate_edgar_filing(
                        signal_payload["filing"], signal_payload["ticker_metadata"],
                    )
                    if prediction is not None:
                        findings["prediction_id"] = str(prediction.prediction_id)
            except Exception:
                logger.exception(
                    "signal evaluation failed for {acc} — filing was still persisted",
                    acc=accession,
                )

    # Engine disposed by task_local_session_factory's finally clause.
    logger.info("process_filing complete: {}", findings)
    return findings


async def _build_signal_payload(
    session,
    accession: str,
    payload: dict,
    update_values: dict,
    findings: dict,
) -> dict | None:
    """Assemble the (filing, ticker_metadata) snapshot the signal engine needs.

    Builds a plain-dict view of the just-updated sec_filings row plus
    metadata pulled from the tickers / promoter_entities / sec_filings
    tables. Returns None if anything required is missing — callers
    treat None as "skip signal evaluation" without surfacing an error.
    """
    from sqlalchemy import select as _select
    from sqlalchemy import func as _func

    from data.models.promoter_entity import PromoterEntity
    from data.models.ticker import Ticker

    ticker = update_values.get("ticker") or payload.get("ticker") or findings.get("ticker")
    cik = payload.get("cik")

    # Try to resolve ticker from cik if we don't have one yet.
    if not ticker and cik:
        cik_row = (
            await session.execute(_select(Ticker).where(Ticker.cik == cik))
        ).scalar_one_or_none()
        if cik_row:
            ticker = cik_row.ticker

    ticker_row = None
    if ticker:
        ticker_row = (
            await session.execute(_select(Ticker).where(Ticker.ticker == ticker))
        ).scalar_one_or_none()

    # Promoter-network match: how many entities match the ir_firm_mentioned
    # text on this filing?
    ir_firm = update_values.get("ir_firm_mentioned") or findings.get("ir_firm")
    promoter_match_count = 0
    if ir_firm:
        promoter_match_count = (
            await session.execute(
                _select(_func.count())
                .select_from(PromoterEntity)
                .where(_func.lower(PromoterEntity.name) == ir_firm.strip().lower())
            )
        ).scalar_one() or 0

    # Underwriter match contributes to the count too.
    if update_values.get("underwriter_id"):
        promoter_match_count += 1

    filing_view = {
        "ticker": ticker,
        "cik": cik,
        "accession_number": accession,
        "form_type": payload.get("form_type"),
        "item_numbers": update_values.get("item_numbers", []),
        "ir_firm_mentioned": ir_firm,
        "s3_effective": bool(update_values.get("s3_effective")),
        "form4_insider_buy": bool(update_values.get("form4_insider_buy")),
        # form4_value_usd / form4_transaction_code aren't currently
        # populated by the parser; stay None until the Form-4 extractor
        # is enhanced. Filter still gates on form4_insider_buy.
        "form4_value_usd": None,
        "form4_transaction_code": None,
        "underwriter_id": update_values.get("underwriter_id"),
    }
    ticker_metadata = {
        "ticker": ticker,
        "exchange": ticker_row.exchange if ticker_row else None,
        "float_shares": ticker_row.float_shares if ticker_row else None,
        "market_cap_usd": None,  # not stored on tickers; add when available
        "promoter_match_count": promoter_match_count,
        "promoter_match_reliability_scores": [],
        "days_since_last_filing": None,
        "days_since_last_promoter_filing": None,
    }
    return {"filing": filing_view, "ticker_metadata": ticker_metadata}


# ---------------------------------------------------------------------------
# APScheduler entry
# ---------------------------------------------------------------------------
def build_scheduler() -> AsyncIOScheduler:
    """Configure APScheduler with the polling cadence and gating."""
    tz = ZoneInfo(settings.TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    trigger = CronTrigger(
        day_of_week="mon-fri",
        hour=f"{constants.EDGAR_POLL_HOUR_START_ET}-{constants.EDGAR_POLL_HOUR_END_ET}",
        minute=f"*/{constants.EDGAR_POLL_INTERVAL_MINUTES}",
        timezone=tz,
    )

    async def _job() -> None:
        try:
            stats = await poll_once()
            logger.info("EDGAR poll done: {}", stats)
        except Exception:
            logger.exception("EDGAR poll cycle failed")

    scheduler.add_job(_job, trigger, id="edgar_rss_poll", replace_existing=True)
    return scheduler


async def _run_forever() -> None:
    scheduler = build_scheduler()
    scheduler.start()
    logger.info(
        "EDGAR RSS watcher started (every {m}m, weekdays {s}-{e} ET, forms={f})",
        m=constants.EDGAR_POLL_INTERVAL_MINUTES,
        s=constants.EDGAR_POLL_HOUR_START_ET,
        e=constants.EDGAR_POLL_HOUR_END_ET,
        f=constants.EDGAR_PRIORITY_FORMS,
    )
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(_run_forever())
    except (KeyboardInterrupt, SystemExit):
        pass
