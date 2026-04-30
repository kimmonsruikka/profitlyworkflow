"""add underwriters table + sec_filings.underwriter_id + seed flagged underwriters

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-29

The Nasdaq Capital Market equivalent of promoter_entities. Seeds the four
high-activity NCM underwriters named in Bloomberg's 2026 investigation
into small-cap listings as initial flagged records — operator can add
more via the underwriter repository as the watchlist grows.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS underwriters (
    underwriter_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT NOT NULL,
    type                    VARCHAR(50),
    first_seen_edgar        TIMESTAMPTZ,
    ncm_listing_count       INTEGER DEFAULT 0,
    manipulation_flagged    BOOLEAN DEFAULT FALSE,
    flag_source             TEXT,
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_underwriters_name    ON underwriters(name);
CREATE INDEX IF NOT EXISTS idx_underwriters_flagged ON underwriters(manipulation_flagged);

ALTER TABLE sec_filings
    ADD COLUMN IF NOT EXISTS underwriter_id UUID
    REFERENCES underwriters(underwriter_id);

CREATE INDEX IF NOT EXISTS idx_sec_filings_underwriter ON sec_filings(underwriter_id);
"""

# Seed the four NCM underwriters from Bloomberg's 2026 investigation as
# initial flagged records. ON CONFLICT DO NOTHING keeps the migration
# idempotent if it's re-run.
SEED_SQL = """
INSERT INTO underwriters (name, type, manipulation_flagged, flag_source, notes)
VALUES
    ('D. Boral Capital',     'underwriter',       TRUE, 'bloomberg',
     'Named in Bloomberg 2026 NCM listing investigation'),
    ('R.F. Lafferty',        'underwriter',       TRUE, 'bloomberg',
     'Named in Bloomberg 2026 NCM listing investigation'),
    ('Dominari Securities',  'underwriter',       TRUE, 'bloomberg',
     'Named in Bloomberg 2026 NCM listing investigation'),
    ('Revere Securities',    'underwriter',       TRUE, 'bloomberg',
     'Named in Bloomberg 2026 NCM listing investigation')
ON CONFLICT DO NOTHING;
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS idx_sec_filings_underwriter;
ALTER TABLE sec_filings DROP COLUMN IF EXISTS underwriter_id;
DROP INDEX IF EXISTS idx_underwriters_flagged;
DROP INDEX IF EXISTS idx_underwriters_name;
DROP TABLE IF EXISTS underwriters;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)
    op.execute(SEED_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
