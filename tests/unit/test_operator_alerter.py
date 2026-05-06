"""Unit tests for scripts/operator_alerter.py.

Heavy use of dependency injection — systemctl, journalctl, DB, and
Telegram sender are all parameters of run_alerter() so tests don't
need monkeypatch gymnastics.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import operator_alerter as oa  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — fakes for the IO surfaces.
# ---------------------------------------------------------------------------
@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "alert_state.json"


def _systemctl_factory(states: dict[str, str]):
    def runner(service: str) -> str:
        return states.get(service, "active")
    return runner


def _last_active_factory(stamps: dict[str, str] | None = None):
    stamps = stamps or {}

    def runner(service: str) -> str:
        return stamps.get(service, "unknown")
    return runner


def _journal_factory(text: str):
    def runner(_minutes: int) -> str:
        return text
    return runner


def _db_factory(*, backlog: int = 0, last_refresh: datetime | None = None,
                raise_exc: Exception | None = None):
    """Build a DB session factory whose count/last queries return
    canned values. raise_exc forces the meta-alert path."""
    @asynccontextmanager
    async def factory():
        if raise_exc is not None:
            raise raise_exc
        session = MagicMock()

        async def execute(stmt):
            stmt_text = str(stmt).lower()
            result = MagicMock()
            if "count" in stmt_text:
                result.scalar_one = MagicMock(return_value=backlog)
            else:
                # last_completed_float_refresh path
                row = MagicMock()
                row.completed_at = last_refresh
                row.started_at = last_refresh
                result.scalar_one_or_none = MagicMock(
                    return_value=row if last_refresh is not None else None
                )
            return result

        session.execute = AsyncMock_(side_effect=execute)
        yield session
    return factory


# AsyncMock-shim that pickles cleanly across Python 3.11/3.12 mock differences
class AsyncMock_:
    def __init__(self, side_effect):
        self._side_effect = side_effect

    async def __call__(self, *args, **kwargs):
        return await self._side_effect(*args, **kwargs)


def _telegram_collector():
    sent: list[tuple[str, str, str]] = []

    def sender(token: str, chat_id: str, text: str) -> None:
        sent.append((token, chat_id, text))
    return sender, sent


def _seed_state(state_path: Path, now_utc: datetime | None = None, **overrides):
    """Write a baseline state file (so the run isn't first-run-suppressed)
    with optional per-condition overrides.

    When now_utc is provided, defaults condition 5's `last_checked_date`
    to that day so the float-refresh check is throttled — keeps tests
    that target other conditions from being cross-fired by an unrelated
    float-refresh alert.
    """
    state = oa.default_state()
    if now_utc is not None:
        state["float_refresh_schedule"]["last_checked_date"] = now_utc.date().isoformat()
    for k, v in overrides.items():
        state[k].update(v)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f)


def _read_state(state_path: Path) -> dict:
    with open(state_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# default_state shape
# ---------------------------------------------------------------------------
def test_default_state_has_all_six_keys():
    s = oa.default_state()
    assert set(s.keys()) == {
        "celery_worker_alive",
        "edgar_watcher_alive",
        "backlog_threshold",
        "celery_failure_rate",
        "float_refresh_schedule",
        "alerter_db_unreachable",
    }
    assert s["float_refresh_schedule"]["last_checked_date"] is None


# ---------------------------------------------------------------------------
# First-run suppression
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_first_run_initializes_state_and_fires_no_alerts(state_path):
    sender, sent = _telegram_collector()
    rc = await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({"celery-worker": "inactive"}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(),
        token="t", chat_id="c",
    )
    assert rc == 0
    assert sent == [], "no alerts on the very first invocation"
    assert state_path.exists()
    state = _read_state(state_path)
    # Even though celery is inactive, breached stays False (first-run baseline).
    assert state["celery_worker_alive"]["breached"] is False


# ---------------------------------------------------------------------------
# Each condition: clear → breached fires; breached → breached suppresses
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_celery_worker_inactive_fires_once(state_path):
    _seed_state(state_path, now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
    sender, sent = _telegram_collector()
    args = dict(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({
            "celery-worker": "inactive", "edgar-watcher": "active",
        }),
        last_active_runner=_last_active_factory({
            "celery-worker": "2026-05-06T11:14:23+0000"
        }),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(),
        token="t", chat_id="c",
    )
    rc = await oa.run_alerter(**args)
    assert rc == 0
    assert len(sent) == 1
    text = sent[0][2]
    assert "⚠ celery-worker not running" in text
    assert "Status: inactive" in text
    assert "2026-05-06T11:14:23+0000" in text

    # Second invocation: still inactive → suppress.
    sender2, sent2 = _telegram_collector()
    args["telegram_sender"] = sender2
    rc = await oa.run_alerter(**args)
    assert rc == 0
    assert sent2 == [], "breached → breached must not re-alert"


@pytest.mark.asyncio
async def test_edgar_watcher_inactive_fires(state_path):
    _seed_state(state_path, now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
    sender, sent = _telegram_collector()
    rc = await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({
            "celery-worker": "active", "edgar-watcher": "failed",
        }),
        last_active_runner=_last_active_factory(
            {"edgar-watcher": "2026-05-05T22:11:00+0000"}
        ),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(),
        token="t", chat_id="c",
    )
    assert rc == 0
    assert len(sent) == 1
    text = sent[0][2]
    assert "edgar-watcher not running" in text
    assert "Status: failed" in text


@pytest.mark.asyncio
async def test_backlog_threshold_fires(state_path):
    _seed_state(state_path, now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
    sender, sent = _telegram_collector()
    rc = await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(backlog=15),
        token="t", chat_id="c",
    )
    assert rc == 0
    assert len(sent) == 1
    text = sent[0][2]
    assert "⚠ Backlog threshold breached" in text
    assert "15 unprocessed filings" in text


@pytest.mark.asyncio
async def test_backlog_below_threshold_does_not_fire(state_path):
    _seed_state(state_path, now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
    sender, sent = _telegram_collector()
    await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(backlog=9),
        token="t", chat_id="c",
    )
    assert sent == []


@pytest.mark.asyncio
async def test_celery_failure_rate_fires_on_three_distinct_task_ids(state_path):
    _seed_state(state_path, now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
    journal = """\
May 06 11:14:23 host celery[123]: Task ingestion.edgar.process_filing[aaa-1] raised unexpected: ValueError(boom)
May 06 11:15:23 host celery[123]: Task ingestion.edgar.process_filing[aaa-1] raised unexpected: ValueError(boom)
May 06 11:16:23 host celery[123]: Task ingestion.edgar.process_filing[bbb-2] raised unexpected: KeyError('x')
May 06 11:17:23 host celery[123]: Task ingestion.edgar.process_filing[ccc-3] raised unexpected: RuntimeError('thing went wrong with the database connection pool')
"""
    sender, sent = _telegram_collector()
    rc = await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(journal),
        db_session_factory=_db_factory(),
        token="t", chat_id="c",
    )
    assert rc == 0
    assert len(sent) == 1
    text = sent[0][2]
    assert "⚠ Celery task failures detected" in text
    assert "3 task crashes" in text
    # Most recent error truncated to 80 chars
    assert "RuntimeError" in text


@pytest.mark.asyncio
async def test_celery_failure_below_threshold_does_not_fire(state_path):
    _seed_state(state_path, now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
    journal = "Task foo[a] raised unexpected: X\nTask foo[b] raised unexpected: Y\n"
    sender, sent = _telegram_collector()
    await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(journal),
        db_session_factory=_db_factory(),
        token="t", chat_id="c",
    )
    assert sent == []


# ---------------------------------------------------------------------------
# Condition 5 — float refresh + daily throttling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_float_refresh_stale_fires(state_path):
    _seed_state(state_path)
    sender, sent = _telegram_collector()
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    last_refresh = datetime(2026, 5, 4, 6, 0, tzinfo=timezone.utc)  # 10 days ago
    rc = await oa.run_alerter(
        state_path=state_path,
        now_utc=now,
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(last_refresh=last_refresh),
        token="t", chat_id="c",
    )
    assert rc == 0
    assert len(sent) == 1
    text = sent[0][2]
    assert "⚠ Float refresh schedule missed" in text
    assert "2026-05-04" in text
    assert "10 days ago" in text
    assert "ET" in text  # next-Sunday line


@pytest.mark.asyncio
async def test_float_refresh_recent_does_not_fire(state_path):
    _seed_state(state_path)
    sender, sent = _telegram_collector()
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    # 3 days ago — within the 8-day window
    last_refresh = datetime(2026, 5, 3, 6, 0, tzinfo=timezone.utc)
    await oa.run_alerter(
        state_path=state_path,
        now_utc=now,
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(last_refresh=last_refresh),
        token="t", chat_id="c",
    )
    assert sent == []


@pytest.mark.asyncio
async def test_float_refresh_throttled_within_same_utc_day(state_path):
    """Once condition 5 has been checked today, subsequent invocations
    on the same UTC day skip the check entirely (no DB call counted
    against it, no re-firing)."""
    # Pre-seed state as already-checked-today, no breach.
    today = "2026-05-14"
    _seed_state(state_path, float_refresh_schedule={
        "breached": False,
        "last_checked_date": today,
    })
    sender, sent = _telegram_collector()
    now = datetime(2026, 5, 14, 23, 30, tzinfo=timezone.utc)
    # Force a stale refresh in the DB; throttling should suppress the alert.
    last_refresh = datetime(2026, 5, 4, tzinfo=timezone.utc)
    await oa.run_alerter(
        state_path=state_path,
        now_utc=now,
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(last_refresh=last_refresh),
        token="t", chat_id="c",
    )
    assert sent == [], "throttled within the same UTC day"


# ---------------------------------------------------------------------------
# Multiple conditions in one invocation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiple_conditions_each_fire_independently(state_path):
    _seed_state(state_path, now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
    journal = (
        "Task f[a] raised unexpected: X\n"
        "Task f[b] raised unexpected: Y\n"
        "Task f[c] raised unexpected: Z\n"
    )
    sender, sent = _telegram_collector()
    rc = await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({
            "celery-worker": "inactive", "edgar-watcher": "active",
        }),
        last_active_runner=_last_active_factory({
            "celery-worker": "2026-05-06T10:00:00+0000",
        }),
        journal_runner=_journal_factory(journal),
        db_session_factory=_db_factory(backlog=20),
        token="t", chat_id="c",
    )
    assert rc == 0
    # Three independent alerts: celery worker, backlog, failure rate.
    texts = [t[2] for t in sent]
    assert any("celery-worker not running" in t for t in texts)
    assert any("Backlog threshold" in t for t in texts)
    assert any("task crashes" in t for t in texts)
    assert len(sent) == 3


# ---------------------------------------------------------------------------
# DB unreachable — meta-alert
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_db_unreachable_fires_meta_alert(state_path):
    _seed_state(state_path, now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
    sender, sent = _telegram_collector()
    rc = await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(
            raise_exc=ConnectionError("no route to host")
        ),
        token="t", chat_id="c",
    )
    assert rc == 0
    assert len(sent) == 1
    assert "⚠ Alerter cannot reach database" in sent[0][2]
    assert "no route to host" in sent[0][2]
    state = _read_state(state_path)
    assert state["alerter_db_unreachable"]["breached"] is True


# ---------------------------------------------------------------------------
# Recovery — clears state silently
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_breached_then_clear_clears_state_without_recovery_message(state_path):
    _seed_state(
        state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        celery_worker_alive={
            "breached": True,
            "last_alerted_at": "2026-05-06T11:00:00+00:00",
        },
    )
    sender, sent = _telegram_collector()
    await oa.run_alerter(
        state_path=state_path,
        now_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        telegram_sender=sender,
        systemctl_runner=_systemctl_factory({"celery-worker": "active"}),
        last_active_runner=_last_active_factory(),
        journal_runner=_journal_factory(""),
        db_session_factory=_db_factory(),
        token="t", chat_id="c",
    )
    assert sent == [], "v1 does not send recovery messages"
    state = _read_state(state_path)
    assert state["celery_worker_alive"]["breached"] is False
    assert state["celery_worker_alive"]["last_alerted_at"] is None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_parse_celery_failures_counts_distinct_task_ids():
    text = """\
Task foo[id-1] raised unexpected: A
Task foo[id-1] raised unexpected: A
Task foo[id-2] raised unexpected: B
"""
    n, last = oa.parse_celery_failures(text)
    assert n == 2
    assert last == "B"


def test_parse_celery_failures_returns_zero_on_empty():
    n, last = oa.parse_celery_failures("")
    assert n == 0
    assert last is None


def test_next_sunday_6am_et_from_weekday():
    # Wed 2026-05-06 12:00 UTC → next Sunday is 2026-05-10
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    nxt = oa.next_sunday_6am_et(now)
    nxt_et = nxt.astimezone(oa.ZoneInfo("America/New_York"))
    assert nxt_et.weekday() == 6  # Sunday
    assert nxt_et.hour == 6
    assert nxt_et.date() == date(2026, 5, 10)


def test_next_sunday_6am_et_from_sunday_after_six_picks_following_week():
    # Sun 2026-05-10 14:00 UTC = 10:00 ET (past 6am) → next Sunday 5/17
    now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    nxt = oa.next_sunday_6am_et(now)
    nxt_et = nxt.astimezone(oa.ZoneInfo("America/New_York"))
    assert nxt_et.date() == date(2026, 5, 17)


def test_format_backlog_alert_shape():
    text = oa.format_backlog_alert(15)
    assert text == (
        "⚠ Backlog threshold breached\n"
        "15 unprocessed filings in sec_filings\n"
        "Threshold: 10"
    )


def test_format_failures_alert_truncates_to_80_chars():
    long_err = "x" * 200
    text = oa.format_failures_alert(5, long_err)
    # The error portion in the text must be ≤80 chars
    err_line = [ln for ln in text.split("\n") if ln.startswith("Most recent error: ")][0]
    err_value = err_line[len("Most recent error: "):]
    assert len(err_value) == 80


def test_format_db_unreachable_truncates():
    text = oa.format_db_unreachable_alert("y" * 500)
    assert "⚠ Alerter cannot reach database" in text
    # 200-char cap on the error text portion
    body = text.split("\n", 1)[1]
    assert len(body) <= 200


# ---------------------------------------------------------------------------
# Env-var validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_main_exits_1_when_token_missing(monkeypatch, capsys, state_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    rc = await oa.run_alerter(state_path=state_path)
    assert rc == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_main_exits_1_when_chat_id_missing(monkeypatch, capsys, state_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    rc = await oa.run_alerter(state_path=state_path)
    assert rc == 1
    assert "TELEGRAM_CHAT_ID" in capsys.readouterr().err
