from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

from app.database_models import (
    Base, QuestionAttempt, QuestionSnapshot, StoredInterviewSession,
    TranscriptionMeasurement, validate_submitted_answer_text,
)
from app.interviewer_personas import INTERVIEWER_PERSONAS
from app.sessions import QUESTIONS

ROOT = Path(__file__).resolve().parents[1]
DELIVERY_COLUMNS = {
    "delivery_measurement_version", "pause_count", "total_pause_duration_seconds",
    "longest_pause_seconds", "pause_unavailable_reason",
}
DELIVERY_CONSTRAINTS = {
    "ck_measurements_delivery_state", "ck_measurements_delivery_version",
    "ck_measurements_pause_reason", "ck_measurements_pause_count",
    "ck_measurements_finite_pause_total", "ck_measurements_finite_pause_longest",
    "ck_measurements_pause_durations", "ck_measurements_pause_word_bound",
}


@pytest.mark.parametrize("value", ["\x00", "hello\x00world", " \x00 "])
def test_persistence_answer_policy_rejects_nul_without_repair(value):
    with pytest.raises(ValueError, match=r"must not contain U\+0000"):
        QuestionAttempt(answer_text=value)


@pytest.mark.parametrize("value", ["", " \t\n ", "x" * 10001, None, 123, True])
def test_persistence_answer_policy_requires_current_trimmed_text_bounds(value):
    with pytest.raises(ValueError):
        validate_submitted_answer_text(value)


def test_answer_normalization_preserves_substantive_text():
    assert QuestionAttempt(answer_text="  Submitted answer.\n").answer_text == "Submitted answer."
    assert validate_submitted_answer_text("x" * 10000) == "x" * 10000


def test_question_snapshot_copies_input_and_returns_immutable_tuple():
    questions = list(QUESTIONS)
    record = StoredInterviewSession(questions=questions)
    questions[0] = "Changed elsewhere"
    assert record.questions == QUESTIONS
    value = QuestionSnapshot().process_result_value(list(QUESTIONS), postgresql.dialect())
    assert value == QUESTIONS
    with pytest.raises(TypeError):
        value[0] = "Changed"


@pytest.mark.parametrize("questions", [[], ["q"] * 6, [1] * 5, [""] * 5, [" "] * 5, ["\x00"] * 5])
def test_question_snapshot_validates_one_to_five_nonblank_text_questions(questions):
    with pytest.raises(ValueError):
        StoredInterviewSession(questions=questions)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_question_snapshot_copies_each_bounded_adaptive_prefix(count):
    supplied = list(QUESTIONS[:count])
    record = StoredInterviewSession(questions=supplied, question_engine="live-ai-roleplay-v1")
    supplied[0] = "Changed elsewhere"
    assert record.questions == QUESTIONS[:count]
    assert type(record.questions) is tuple


@pytest.mark.parametrize("engine", ["deterministic-v1", "live-ai-roleplay-v1"])
def test_question_engine_mapping_accepts_only_canonical_engines_with_legacy_defaults(engine):
    record = StoredInterviewSession(question_engine=engine)
    assert record.question_engine == engine
    column = StoredInterviewSession.__table__.c.question_engine
    assert column.nullable is False
    assert column.type.collation == "C"
    assert column.default.arg == "deterministic-v1"
    assert str(column.server_default.arg) == "deterministic-v1"


@pytest.mark.parametrize("engine", ["", "other", "LIVE-AI-ROLEPLAY-V1", None, True, 1])
def test_question_engine_mapping_rejects_unknown_values(engine):
    with pytest.raises(ValueError, match="^Unsupported question engine\\.$"):
        StoredInterviewSession(question_engine=engine)


@pytest.mark.parametrize("persona_id", ["recruiter", "manager", "hr"])
def test_interviewer_persona_mapping_accepts_only_fixed_persisted_ids(persona_id):
    record = StoredInterviewSession(questions=QUESTIONS, interviewer_persona_id=persona_id)
    assert record.interviewer_persona_id == persona_id
    column = StoredInterviewSession.__table__.c.interviewer_persona_id
    assert column.nullable is True
    assert column.default is None and column.server_default is None
    assert column.type.collation == "C"
    assert INTERVIEWER_PERSONAS[persona_id].id == persona_id


@pytest.mark.parametrize("persona_id", ["", "University Recruiter", "manager ", True, 1, {}])
def test_interviewer_persona_mapping_rejects_unknown_values(persona_id):
    with pytest.raises(ValueError, match="^Unsupported interviewer persona\\.$"):
        StoredInterviewSession(interviewer_persona_id=persona_id)


def test_interviewer_persona_null_is_reserved_for_legacy_sessions():
    assert StoredInterviewSession(interviewer_persona_id=None).interviewer_persona_id is None


@pytest.mark.parametrize("hours,linked,expected", [
    (23.999, False, False), (24, False, True), (25, False, True), (24, True, False), (100, True, False),
])
def test_unlinked_retention_is_exactly_24_hours_and_preserves_linked_measurements(hours, linked, expected):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = TranscriptionMeasurement(created_at=created)
    assert record.unlinked_deletion_eligible(linked=linked, now=created + timedelta(hours=hours)) is expected


def test_retention_rejects_ambiguous_naive_timestamps():
    record = TranscriptionMeasurement(created_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        record.unlinked_deletion_eligible(linked=False, now=datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_alembic_has_roleplay_head_after_scenarios_and_emits_postgresql_ddl_offline(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://offline@localhost/rehearse_dev")
    output = StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    config.attributes["skip_logging"] = True
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0007_interviewer_personas"]
    assert scripts.get_revision("head").down_revision == "0006_live_ai_roleplay"
    assert scripts.get_revision("0005_session_scenarios").down_revision == "0004_oidc_login_transactions"
    assert scripts.get_revision("0004_oidc_login_transactions").down_revision == "0003_auth_user_ownership"
    assert scripts.get_revision("0003_auth_user_ownership").down_revision == "0002_pause_delivery_metrics"
    assert scripts.get_revision("0002_pause_delivery_metrics").down_revision == "0001_database_foundation"
    assert scripts.get_revision("0001_database_foundation").down_revision is None
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    for table in Base.metadata.tables:
        assert f"CREATE TABLE {table}" in sql
    assert "JSONB" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "FOREIGN KEY(measurement_id, session_id, question_index)" in sql
    assert "CREATE TRIGGER preserve_measurement" in sql
    assert "ADD COLUMN scenario_type TEXT COLLATE \"C\" DEFAULT 'job_interview' NOT NULL" in sql
    assert "ADD CONSTRAINT ck_sessions_scenario_type" in sql
    assert "ADD COLUMN question_engine TEXT COLLATE \"C\" DEFAULT 'deterministic-v1' NOT NULL" in sql
    assert "ADD CONSTRAINT ck_sessions_question_engine" in sql
    assert "CREATE OR REPLACE FUNCTION rehearse_preserve_questions()" in sql
    assert "Question engine is immutable." in sql
    assert "Adaptive questions must append with one advancement." in sql
    assert "ADD COLUMN interviewer_persona_id TEXT COLLATE \"C\"" in sql
    assert "ADD CONSTRAINT ck_sessions_interviewer_persona" in sql
    assert "CREATE TRIGGER preserve_interviewer_persona" in sql
    assert "Interviewer persona is immutable." in sql
    for column in DELIVERY_COLUMNS:
        assert f"ADD COLUMN {column}" in sql
    for constraint in DELIVERY_CONSTRAINTS:
        assert f"ADD CONSTRAINT {constraint}" in sql
    output.seek(0)
    output.truncate(0)
    command.downgrade(config, "head:base", sql=True)
    sql = output.getvalue()
    assert "Adaptive sessions prevent this downgrade." in sql
    assert sql.index("DROP COLUMN question_engine") < sql.index("DROP COLUMN scenario_type")
    assert sql.index("DROP COLUMN scenario_type") < sql.index("DROP TABLE oidc_login_transactions")
    for column in DELIVERY_COLUMNS:
        assert f"DROP COLUMN {column}" in sql
    assert sql.index("DROP COLUMN delivery_measurement_version") < sql.index("DROP TABLE question_attempts")
    assert sql.index("DROP INDEX ix_oidc_login_transactions_expires_at") < sql.index("DROP TABLE oidc_login_transactions")
    assert sql.index("DROP TABLE oidc_login_transactions") < sql.index("DROP TABLE auth_sessions")
    assert sql.index("DROP TABLE question_attempts") < sql.index("DROP TABLE transcription_measurements")
    assert "DROP FUNCTION rehearse_preserve_measurement()" in sql


def test_delivery_orm_columns_are_nullable_without_historical_defaults():
    columns = TranscriptionMeasurement.__table__.columns
    for name in DELIVERY_COLUMNS:
        column = columns[name]
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None
    record = TranscriptionMeasurement()
    assert all(getattr(record, name) is None for name in DELIVERY_COLUMNS)


def test_delivery_orm_declares_only_the_approved_extension_and_constraints():
    assert set(TranscriptionMeasurement.__table__.columns.keys()) == {
        "id", "session_id", "question_index", "created_at", "measurement_version",
        "measurement_source", "recognized_word_count", "um_count", "uh_count",
        "filler_unavailable_reason", "timed_utterance_span_seconds",
        "estimated_words_per_minute", "timing_unavailable_reason",
        *DELIVERY_COLUMNS,
    }
    names = {constraint.name for constraint in TranscriptionMeasurement.__table__.constraints}
    assert DELIVERY_CONSTRAINTS <= names
    assert set(Base.metadata.tables) == {
        "interview_sessions", "question_attempts", "transcription_measurements",
        "users", "auth_sessions", "oidc_login_transactions",
    }


def test_initial_migration_remains_identical_to_the_main_foundation_bytes():
    path = ROOT / "alembic" / "versions" / "0001_database_foundation.py"
    assert sha256(path.read_bytes()).hexdigest() == "fe6eb90cac67dd1703fced254a18f890ce91fbb38edc435a2a3691cc9861acdb"
