"""add predictions, outcomes, model_versions tables

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-30

Foundation for the learning architecture: every signal evaluation writes
a predictions row, the outcome_resolution flow closes them out with
matured outcome rows, and model_versions records what scorer wrote each
prediction so we can A/B rules-v1 against future GBDT models.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
-- ---------------------------------------------------------------------------
-- model_versions  (predictions + outcomes both reference this implicitly
-- via scorer_version; defined first so the seed row is available)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_versions (
    version_id              VARCHAR(40) PRIMARY KEY,
    model_class             VARCHAR(40) NOT NULL,
    feature_schema_version  VARCHAR(20) NOT NULL,
    trained_at              TIMESTAMPTZ,
    training_set_size       INTEGER,
    calibration_metrics     JSONB DEFAULT '{}'::jsonb,
    in_production           BOOLEAN NOT NULL DEFAULT FALSE,
    in_shadow               BOOLEAN NOT NULL DEFAULT FALSE,
    artifact_path           TEXT,
    notes                   TEXT
);

INSERT INTO model_versions (
    version_id, model_class, feature_schema_version, trained_at,
    training_set_size, calibration_metrics, in_production, in_shadow,
    artifact_path, notes
)
VALUES (
    'rules-v1', 'rules', 'fv-v1', NOW(),
    0, '{}'::jsonb, TRUE, FALSE,
    NULL, 'Initial hand-coded rules scorer'
)
ON CONFLICT (version_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- predictions  (immutable; every signal evaluation writes one row)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker                     VARCHAR(10) NOT NULL,
    signal_type                VARCHAR(40) NOT NULL,
    feature_vector             JSONB NOT NULL,
    feature_schema_version     VARCHAR(20) NOT NULL,
    scorer_version             VARCHAR(40) NOT NULL,
    confidence                 NUMERIC(5,4) NOT NULL,
    predicted_window_minutes   INTEGER NOT NULL,
    predicted_target_pct       NUMERIC(6,3),
    alert_sent                 BOOLEAN NOT NULL DEFAULT FALSE,
    user_decision              VARCHAR(20),
    decision_reason            TEXT,
    trade_id                   UUID,
    outcome_id                 UUID
);

CREATE INDEX IF NOT EXISTS idx_predictions_ticker_time
    ON predictions(ticker, created_at);
-- Partial index — outcome_resolution flow only ever queries unresolved rows.
CREATE INDEX IF NOT EXISTS idx_predictions_unresolved
    ON predictions(created_at)
    WHERE outcome_id IS NULL;

-- ---------------------------------------------------------------------------
-- outcomes  (one per resolved prediction; UNIQUE(prediction_id) enforces 1:1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prediction_id                 UUID NOT NULL REFERENCES predictions(prediction_id),
    resolved_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    window_close_at               TIMESTAMPTZ NOT NULL,
    max_favorable_excursion_pct   NUMERIC(6,3),
    max_adverse_excursion_pct     NUMERIC(6,3),
    realized_return_pct           NUMERIC(6,3),
    paper_return_pct              NUMERIC(6,3),
    hit_target                    BOOLEAN,
    hit_stop                      BOOLEAN,
    outcome_label                 VARCHAR(20) NOT NULL,
    price_data_source             VARCHAR(40) NOT NULL,
    UNIQUE (prediction_id)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_label
    ON outcomes(outcome_label);
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS idx_outcomes_label;
DROP TABLE IF EXISTS outcomes;
DROP INDEX IF EXISTS idx_predictions_unresolved;
DROP INDEX IF EXISTS idx_predictions_ticker_time;
DROP TABLE IF EXISTS predictions;
DROP TABLE IF EXISTS model_versions;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
