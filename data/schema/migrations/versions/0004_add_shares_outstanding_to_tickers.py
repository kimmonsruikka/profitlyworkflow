"""add shares_outstanding to tickers

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-29
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
ALTER TABLE tickers ADD COLUMN IF NOT EXISTS shares_outstanding BIGINT;
"""

DOWNGRADE_SQL = """
ALTER TABLE tickers DROP COLUMN IF EXISTS shares_outstanding;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
