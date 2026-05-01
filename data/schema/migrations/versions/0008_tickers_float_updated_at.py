"""tickers.float_updated_at staleness column

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-01

Adds tickers.float_updated_at so the float-refresh flow can order
its sweep oldest-stale-first and downstream features can flag stale
floats. Existing rows are intentionally left NULL — they'll get
populated as the next sweep refreshes them. NULLS FIRST ordering on
the index means brand-new and never-refreshed rows get touched
before slightly-stale ones.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS float_updated_at TIMESTAMPTZ;

-- Partial index — only active tickers participate in the float sweep,
-- so a covering index on the inactive ones would just waste pages.
CREATE INDEX IF NOT EXISTS idx_tickers_float_updated_at
    ON tickers(float_updated_at)
    WHERE active = TRUE;
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS idx_tickers_float_updated_at;
ALTER TABLE tickers DROP COLUMN IF EXISTS float_updated_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
