"""price_data granularity + outcomes.invalid_reason

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-30

price_data is a TimescaleDB hypertable that already exists from earlier
Phase 0 work. The new PriceSource layer needs a granularity dimension
(1m vs 5m bars for the same ticker / time range), so this migration:

  - Adds price_data.granularity (default '1m' so existing rows backfill
    cleanly, then drops the default to force future inserts to specify)
  - Drops the old (ticker, timestamp) PK and re-adds it as
    (ticker, granularity, timestamp) — TimescaleDB requires the
    partitioning column (timestamp) be in any UNIQUE constraint, which
    this satisfies
  - Adds idx_price_data_lookup (ticker, granularity, timestamp DESC) —
    DESC ordering for cache 'most-recent first' lookups, distinct from
    the PK btree

Also adds outcomes.invalid_reason — populated only when
outcome_label='INVALID' so the dashboard can group by why a prediction
couldn't be resolved.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
-- ---------------------------------------------------------------------------
-- price_data — add granularity dimension
-- ---------------------------------------------------------------------------
ALTER TABLE price_data
    ADD COLUMN IF NOT EXISTS granularity VARCHAR(10) DEFAULT '1m';

UPDATE price_data SET granularity = '1m' WHERE granularity IS NULL;

ALTER TABLE price_data DROP CONSTRAINT IF EXISTS price_data_pkey;
ALTER TABLE price_data ADD PRIMARY KEY (ticker, granularity, "timestamp");
ALTER TABLE price_data ALTER COLUMN granularity DROP DEFAULT;

CREATE INDEX IF NOT EXISTS idx_price_data_lookup
    ON price_data (ticker, granularity, "timestamp" DESC);

-- ---------------------------------------------------------------------------
-- outcomes — add invalid_reason
-- ---------------------------------------------------------------------------
ALTER TABLE outcomes
    ADD COLUMN IF NOT EXISTS invalid_reason VARCHAR(50);
"""

DOWNGRADE_SQL = """
ALTER TABLE outcomes DROP COLUMN IF EXISTS invalid_reason;

DROP INDEX IF EXISTS idx_price_data_lookup;
ALTER TABLE price_data DROP CONSTRAINT IF EXISTS price_data_pkey;
ALTER TABLE price_data ADD PRIMARY KEY (ticker, "timestamp");
ALTER TABLE price_data DROP COLUMN IF EXISTS granularity;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
