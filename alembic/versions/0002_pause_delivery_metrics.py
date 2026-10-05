"""Nullable pause aggregates on the existing immutable measurement snapshot.

Revision ID: 0002_pause_delivery_metrics
Revises: 0001_database_foundation

Historical rows remain legacy/null. This DDL is self-contained and never updates
snapshots or changes the existing whole-row immutability trigger.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_pause_delivery_metrics"
down_revision = "0001_database_foundation"
branch_labels = None
depends_on = None


_DELIVERY_CHECKS = (
    (
        "ck_measurements_delivery_state",
        "CASE WHEN delivery_measurement_version IS NULL THEN "
        "(pause_count IS NULL AND total_pause_duration_seconds IS NULL "
        "AND longest_pause_seconds IS NULL AND pause_unavailable_reason IS NULL) "
        "WHEN pause_unavailable_reason IS NULL THEN "
        "(pause_count IS NOT NULL AND total_pause_duration_seconds IS NOT NULL "
        "AND longest_pause_seconds IS NOT NULL) ELSE "
        "(pause_count IS NULL AND total_pause_duration_seconds IS NULL "
        "AND longest_pause_seconds IS NULL) END",
    ),
    (
        "ck_measurements_delivery_version",
        "delivery_measurement_version IS NULL OR "
        "delivery_measurement_version ~ '[^[:space:]]'",
    ),
    (
        "ck_measurements_pause_reason",
        "pause_unavailable_reason IS NULL OR pause_unavailable_reason IN "
        "('missing_timings', 'timing_coverage_mismatch', 'invalid_timing', "
        "'invalid_timing_order', 'unusable_span')",
    ),
    (
        "ck_measurements_pause_count",
        "pause_count IS NULL OR pause_count >= 0",
    ),
    (
        "ck_measurements_finite_pause_total",
        "total_pause_duration_seconds IS NULL OR (total_pause_duration_seconds >= 0 "
        "AND total_pause_duration_seconds < 'Infinity'::double precision)",
    ),
    (
        "ck_measurements_finite_pause_longest",
        "longest_pause_seconds IS NULL OR (longest_pause_seconds >= 0 "
        "AND longest_pause_seconds < 'Infinity'::double precision)",
    ),
    (
        "ck_measurements_pause_durations",
        "CASE WHEN pause_count IS NULL THEN true ELSE "
        "(total_pause_duration_seconds IS NOT NULL AND longest_pause_seconds IS NOT NULL AND "
        "((pause_count = 0 AND total_pause_duration_seconds = 0 AND longest_pause_seconds = 0) OR "
        "(pause_count > 0 AND total_pause_duration_seconds > 0 AND longest_pause_seconds > 0 "
        "AND longest_pause_seconds <= total_pause_duration_seconds))) END",
    ),
    (
        "ck_measurements_pause_word_bound",
        "CASE WHEN pause_count IS NULL THEN true "
        "WHEN recognized_word_count >= 1 THEN pause_count <= recognized_word_count - 1 "
        "ELSE false END",
    ),
)


def upgrade():
    # Nullable columns without defaults preserve populated 0001 rows as legacy.
    op.add_column("transcription_measurements", sa.Column("delivery_measurement_version", sa.Text(), nullable=True))
    op.add_column("transcription_measurements", sa.Column("pause_count", sa.Integer(), nullable=True))
    op.add_column("transcription_measurements", sa.Column("total_pause_duration_seconds", sa.Double(), nullable=True))
    op.add_column("transcription_measurements", sa.Column("longest_pause_seconds", sa.Double(), nullable=True))
    op.add_column("transcription_measurements", sa.Column("pause_unavailable_reason", sa.Text(), nullable=True))
    for name, condition in _DELIVERY_CHECKS:
        op.create_check_constraint(name, "transcription_measurements", condition)


def downgrade():
    # Only this extension is removed; parent facts, linkage and triggers remain.
    for name, _ in reversed(_DELIVERY_CHECKS):
        op.drop_constraint(name, "transcription_measurements", type_="check")
    for name in (
        "pause_unavailable_reason", "longest_pause_seconds", "total_pause_duration_seconds",
        "pause_count", "delivery_measurement_version",
    ):
        op.drop_column("transcription_measurements", name)
