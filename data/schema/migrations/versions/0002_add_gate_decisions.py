"""add gate_decisions table

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-29
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS gate_decisions (
    decision_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id       UUID REFERENCES signals(signal_id),
    rule_triggered  VARCHAR(64) NOT NULL,
    approved        BOOLEAN NOT NULL,
    message         VARCHAR(500),
    timestamp       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gate_decisions_signal    ON gate_decisions(signal_id);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_timestamp ON gate_decisions(timestamp);
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gate_decisions CASCADE;")
