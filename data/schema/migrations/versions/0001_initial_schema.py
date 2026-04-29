"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-28

Applies data/schema/schema.sql so the canonical schema (including the
TimescaleDB hypertable on price_data) is the single source of truth.
"""

from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schema.sql"

DROP_SQL = """
DROP TABLE IF EXISTS price_data CASCADE;
DROP TABLE IF EXISTS account_state CASCADE;
DROP TABLE IF EXISTS positions CASCADE;
DROP TABLE IF EXISTS trades CASCADE;
DROP TABLE IF EXISTS signals CASCADE;
DROP TABLE IF EXISTS sec_filings CASCADE;
DROP TABLE IF EXISTS promoter_network_edges CASCADE;
DROP TABLE IF EXISTS promoter_campaigns CASCADE;
DROP TABLE IF EXISTS promoter_entities CASCADE;
DROP TABLE IF EXISTS tickers CASCADE;
"""


def upgrade() -> None:
    sql = SCHEMA_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    op.execute(DROP_SQL)
