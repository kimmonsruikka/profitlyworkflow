"""flow_run_log table for operational history

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-01

Generic operational-history table for Prefect flows. Each flow run
writes a row on start, updates it on completion/failure. Useful for:

  - 'Did the float-refresh flow run last week?'
  - 'When was the last successful outcome-resolution sweep?'
  - 'How long did the last sweep take, and how many rows did it touch?'

The summary JSONB column lets each flow store its own metric shape
without forcing a schema migration per flow. Keep summary keys
flat-ish — no deeply nested objects — so SQL ad-hoc queries stay
ergonomic.

Retention: rows accumulate forever. A retention sweep (delete
> 1 year) is a follow-up; current volume is a few rows per week.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS flow_run_log (
    flow_run_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flow_name     VARCHAR(80) NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    status        VARCHAR(20) NOT NULL,
    summary       JSONB,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_flow_run_log_name_started
    ON flow_run_log(flow_name, started_at DESC);
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS idx_flow_run_log_name_started;
DROP TABLE IF EXISTS flow_run_log;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
