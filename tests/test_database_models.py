from datetime import datetime, timedelta, timezone
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
from app.sessions import QUESTIONS

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize("questions", [[], ["q"], [1] * 5, [""] * 5, ["\x00"] * 5])
def test_question_snapshot_validates_five_nonempty_text_questions(questions):
    with pytest.raises(ValueError):
        StoredInterviewSession(questions=questions)


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


def test_alembic_has_one_initial_revision_and_emits_postgresql_ddl_offline(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://offline@localhost/rehearse_dev")
    output = StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    config.attributes["skip_logging"] = True
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0001_database_foundation"]
    assert scripts.get_revision("head").down_revision is None
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    for table in Base.metadata.tables:
        assert f"CREATE TABLE {table}" in sql
    assert "JSONB" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "FOREIGN KEY(measurement_id, session_id, question_index)" in sql
    assert "CREATE TRIGGER preserve_measurement" in sql
    output.seek(0)
    output.truncate(0)
    command.downgrade(config, "head:base", sql=True)
    sql = output.getvalue()
    assert sql.index("DROP TABLE question_attempts") < sql.index("DROP TABLE transcription_measurements")
    assert "DROP FUNCTION rehearse_preserve_measurement()" in sql
