"""add cik column and indexes to tickers

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-29
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS cik VARCHAR(20);
CREATE INDEX IF NOT EXISTS idx_tickers_cik    ON tickers(cik);
CREATE INDEX IF NOT EXISTS idx_tickers_active ON tickers(active);
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS idx_tickers_active;
DROP INDEX IF EXISTS idx_tickers_cik;
ALTER TABLE tickers DROP COLUMN IF EXISTS cik;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
