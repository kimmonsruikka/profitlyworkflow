"""Smoke checks for the systemd unit files shipped from deploy/.

These don't validate runtime behavior — that needs a live droplet —
but they pin invariants that would otherwise silently break in CI:
the unit files have to keep declaring the environment we depend on.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"


def _unit_text(name: str) -> str:
    path = DEPLOY_DIR / name
    assert path.exists(), f"{name} missing from deploy/"
    return path.read_text()


# ---------------------------------------------------------------------------
# celery-worker.service — must set PYTHONPATH so subprocess workers can
# resolve project packages (signals/, ingestion/, data/, etc.). This is
# the regression guard for the production outage where every filing logged
# ModuleNotFoundError: No module named 'signals' inside the task body.
# ---------------------------------------------------------------------------
def test_celery_worker_unit_declares_pythonpath():
    unit = _unit_text("celery-worker.service")
    assert 'Environment="PYTHONPATH=/app/profitlyworkflow"' in unit, (
        "celery-worker.service must export PYTHONPATH so prefork "
        "subprocess workers can import project packages (signals.engine, "
        "ingestion.edgar.*, etc.). Without it, every filing's signal-eval "
        "step fails with ModuleNotFoundError."
    )


def test_celery_worker_unit_runs_as_trading_user():
    """Hardening regression — trading user, never root."""
    unit = _unit_text("celery-worker.service")
    assert "User=trading" in unit
    assert "Group=trading" in unit


def test_celery_worker_unit_uses_repo_managed_venv():
    unit = _unit_text("celery-worker.service")
    assert (
        "/app/profitlyworkflow/venv/bin/celery" in unit
    ), "ExecStart must run from the repo-managed venv"


# ---------------------------------------------------------------------------
# edgar-watcher.service — sanity: same hardening, no PYTHONPATH needed
# (python -m auto-adds cwd to sys.path, so the watcher is unaffected).
# ---------------------------------------------------------------------------
def test_edgar_watcher_unit_runs_as_trading_user():
    unit = _unit_text("edgar-watcher.service")
    assert "User=trading" in unit
    assert "Group=trading" in unit


def test_edgar_watcher_unit_invokes_python_dash_m():
    """`python -m ingestion.edgar.rss_watcher` adds cwd to sys.path
    automatically — that's why the watcher doesn't need an explicit
    PYTHONPATH like celery-worker does."""
    unit = _unit_text("edgar-watcher.service")
    assert "python -m ingestion.edgar.rss_watcher" in unit
