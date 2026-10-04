"""Real PostgreSQL only. Destructive DDL is confined to an explicit test target.

    Missing local configuration skips with BLOCKED_BY_LOCAL_DB_ENV. CI requires
    PostgreSQL and must fail, rather than silently skip, if configuration is absent.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import delete, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import create_database_engine, get_test_database_url
from app.database_models import (
    Base, MEASUREMENT_VERSION, QuestionAttempt, StoredInterviewSession,
    TranscriptionMeasurement,
)
from app.sessions import QUESTIONS

ROOT = Path(__file__).resolve().parents[1]
TABLES = {"interview_sessions", "question_attempts", "transcription_measurements"}


def migration_config(connection):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes.update(connection=connection, skip_logging=True)
    return config


@pytest.fixture(scope="module")
def postgres_engine():
    if not os.environ.get("TEST_DATABASE_URL", "").strip():
        if os.environ.get("REHEARSE_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("CI requires explicit TEST_DATABASE_URL; PostgreSQL tests cannot be skipped.", pytrace=False)
        pytest.skip("BLOCKED_BY_LOCAL_DB_ENV: explicit isolated PostgreSQL TEST_DATABASE_URL is required")
    target = get_test_database_url()
    engine = create_database_engine(target)
    try:
        # Verify the server's actual database/role before any destructive DDL.
        try:
            with engine.connect() as connection:
                actual = connection.execute(text("SELECT current_database(), current_user")).one()
                if tuple(actual) != ("rehearse_test", "rehearse_test"):
                    pytest.fail("Connected target is not the dedicated test database and role; nothing removed.", pytrace=False)
        except SQLAlchemyError:
            pytest.fail("Configured PostgreSQL test connection failed; details omitted.", pytrace=False)
        with engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if tables - TABLES - {"alembic_version"}:
                pytest.fail("Test database contains unexpected tables; nothing removed.", pytrace=False)
            if "alembic_version" in tables:
                command.downgrade(migration_config(connection), "base")
            elif tables:
                pytest.fail("Test database contains unversioned tables; nothing removed.", pytrace=False)
            command.upgrade(migration_config(connection), "head")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def connection(postgres_engine):
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def add_session(connection, **changes):
    values = {"id": uuid4(), "questions": list(QUESTIONS), **changes}
    connection.execute(insert(StoredInterviewSession).values(**values))
    return values["id"]


def add_measurement(connection, session_id, **changes):
    span = 1.234567890123
    values = {
        "id": uuid4(), "session_id": session_id, "question_index": 0,
        "measurement_version": MEASUREMENT_VERSION, "measurement_source": "original_transcription",
        "recognized_word_count": 2, "um_count": 0, "uh_count": 0,
        "filler_unavailable_reason": None,
        "timed_utterance_span_seconds": span, "estimated_words_per_minute": 120 / span,
        "timing_unavailable_reason": None, **changes,
    }
    connection.execute(insert(TranscriptionMeasurement).values(**values))
    return values["id"]


def add_attempt(connection, session_id, **changes):
    values = {
        "id": uuid4(), "session_id": session_id, "question_index": 0,
        "attempt_number": 1, "answer_text": "Submitted answer.", **changes,
    }
    connection.execute(insert(QuestionAttempt).values(**values))
    return values["id"]


def rejected(connection, operation, sqlstate):
    with pytest.raises(IntegrityError) as caught:
        with connection.begin_nested():
            operation()
    # Only the stable SQLSTATE is inspected, never exception text or bound values.
    assert caught.value.orig.sqlstate == sqlstate


def test_initial_migration_upgrades_empty_database_and_downgrades_deterministically(postgres_engine):
    with postgres_engine.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) - {"alembic_version"} == set()
        assert connection.scalar(text(
            "SELECT count(*) FROM pg_proc WHERE proname IN "
            "('rehearse_preserve_measurement', 'rehearse_preserve_questions')"
        )) == 0
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) == TABLES | {"alembic_version"}
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_database_foundation"


def test_migrated_schema_matches_orm_metadata(connection):
    assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_uuid_keys_jsonb_and_default_session_state_round_trip(connection):
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        record = StoredInterviewSession(questions=list(QUESTIONS))
        session.add(record)
        session.flush()
        identifier = record.id
        session.expunge_all()
        loaded = session.get(StoredInterviewSession, identifier)
        assert isinstance(loaded.id, UUID)
        assert loaded.questions == QUESTIONS
        assert loaded.status == "active"
        assert loaded.current_question_index == 0
        assert loaded.created_at.tzinfo is not None
        assert loaded.completed_at is None


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("changes", [
    {"status": "pending"}, {"current_question_index": -1}, {"current_question_index": 5},
    {"completed_at": NOW}, {"status": "completed"},
    {"status": "completed", "current_question_index": 5},
    {"status": "completed", "current_question_index": 4, "completed_at": NOW},
    {"status": "completed", "current_question_index": 5, "created_at": NOW,
     "completed_at": NOW - timedelta(seconds=1)},
    {"questions": []}, {"questions": {"invalid": "shape"}}, {"questions": [1] * 5},
    {"questions": [""] * 5},
])
def test_invalid_session_state_or_snapshot_rejected_by_postgresql(connection, changes):
    rejected(connection, lambda: add_session(connection, **changes), "23514")


def test_valid_completed_session_state(connection):
    identifier = add_session(connection, status="completed", current_question_index=5,
                             created_at=NOW, completed_at=NOW + timedelta(seconds=1))
    row = connection.execute(select(StoredInterviewSession.__table__).where(
        StoredInterviewSession.id == identifier)).one()
    assert row.status == "completed" and row.completed_at is not None


def test_question_snapshot_is_immutable_in_database(connection):
    identifier = add_session(connection)
    changed = ["Changed question", *QUESTIONS[1:]]
    rejected(connection, lambda: connection.execute(update(StoredInterviewSession).where(
        StoredInterviewSession.id == identifier).values(questions=changed)), "23514")
    assert connection.scalar(select(StoredInterviewSession.questions).where(
        StoredInterviewSession.id == identifier)) == QUESTIONS


def test_attempt_text_numbering_and_uniqueness(connection):
    identifier = add_session(connection)
    first = add_attempt(connection, identifier)
    for number in (2, 3):
        add_attempt(connection, identifier, attempt_number=number)
    assert connection.scalar(select(QuestionAttempt.answer_text).where(
        QuestionAttempt.id == first)) == "Submitted answer."
    assert connection.execute(select(QuestionAttempt.attempt_number).order_by(
        QuestionAttempt.attempt_number)).scalars().all() == [1, 2, 3]
    rejected(connection, lambda: add_attempt(connection, identifier), "23505")


@pytest.mark.parametrize("changes", [
    {"attempt_number": 0}, {"attempt_number": -1}, {"question_index": -1},
    {"answer_text": ""}, {"answer_text": "x" * 10001},
])
def test_invalid_attempt_constraints(connection, changes):
    identifier = add_session(connection)
    rejected(connection, lambda: add_attempt(connection, identifier, **changes), "23514")


def test_available_metrics_preserve_unrounded_values_and_version(connection):
    identifier = add_session(connection)
    measurement = add_measurement(connection, identifier)
    row = connection.execute(select(TranscriptionMeasurement.__table__).where(
        TranscriptionMeasurement.id == measurement)).one()
    assert isinstance(row.id, UUID)
    assert row.measurement_version == MEASUREMENT_VERSION
    assert row.measurement_source == "original_transcription"
    assert row.timed_utterance_span_seconds == 1.234567890123
    assert row.estimated_words_per_minute == 120 / 1.234567890123
    assert row.created_at.tzinfo is not None


@pytest.mark.parametrize("reason", [
    "missing_timings", "timing_coverage_mismatch", "invalid_timing", "invalid_timing_order", "unusable_span",
])
def test_unavailable_metrics_round_trip_as_null_not_zero(connection, reason):
    identifier = add_session(connection)
    measurement = add_measurement(
        connection, identifier, um_count=None, uh_count=None, filler_unavailable_reason="unsupported_language",
        timed_utterance_span_seconds=None, estimated_words_per_minute=None, timing_unavailable_reason=reason,
    )
    row = connection.execute(select(TranscriptionMeasurement.__table__).where(
        TranscriptionMeasurement.id == measurement)).one()
    assert row.um_count is row.uh_count is None
    assert row.timed_utterance_span_seconds is row.estimated_words_per_minute is None
    assert row.filler_unavailable_reason == "unsupported_language" and row.timing_unavailable_reason == reason


@pytest.mark.parametrize("changes", [
    {"question_index": -1}, {"recognized_word_count": -1}, {"um_count": -1}, {"uh_count": -1},
    {"measurement_source": "edited_answer"}, {"measurement_version": " "},
    {"filler_unavailable_reason": "unknown"}, {"um_count": None},
    {"um_count": None, "uh_count": None}, {"filler_unavailable_reason": "unsupported_language"},
    {"timing_unavailable_reason": "unknown"}, {"timing_unavailable_reason": "missing_timings"},
    {"timed_utterance_span_seconds": None},
    {"timed_utterance_span_seconds": None, "estimated_words_per_minute": None},
    *({field: value} for field in ("timed_utterance_span_seconds", "estimated_words_per_minute")
      for value in (0, -1, float("inf"), float("-inf"), float("nan"))),
])
def test_invalid_measurements_rejected_without_null_check_loopholes(connection, changes):
    identifier = add_session(connection)
    rejected(connection, lambda: add_measurement(connection, identifier, **changes), "23514")


def test_measurement_is_immutable_and_can_attach_only_once(connection):
    identifier = add_session(connection)
    measurement = add_measurement(connection, identifier)
    rejected(connection, lambda: connection.execute(update(TranscriptionMeasurement).where(
        TranscriptionMeasurement.id == measurement).values(recognized_word_count=3)), "23514")
    add_attempt(connection, identifier, measurement_id=measurement)
    rejected(connection, lambda: add_attempt(connection, identifier, attempt_number=2,
                                             measurement_id=measurement), "23505")


def test_measurement_context_and_all_foreign_keys_are_enforced(connection):
    first, second = add_session(connection), add_session(connection)
    measurement = add_measurement(connection, first)
    rejected(connection, lambda: add_attempt(connection, second, measurement_id=measurement), "23503")
    rejected(connection, lambda: add_attempt(connection, first, question_index=1,
                                             measurement_id=measurement), "23503")
    rejected(connection, lambda: add_attempt(connection, first, measurement_id=uuid4()), "23503")
    rejected(connection, lambda: add_attempt(connection, uuid4()), "23503")
    rejected(connection, lambda: add_measurement(connection, uuid4()), "23503")


def test_session_deletion_cascades_but_linked_measurement_cannot_be_deleted_alone(connection):
    identifier = add_session(connection)
    measurement = add_measurement(connection, identifier)
    add_attempt(connection, identifier, measurement_id=measurement)
    rejected(connection, lambda: connection.execute(delete(TranscriptionMeasurement).where(
        TranscriptionMeasurement.id == measurement)), "23503")
    connection.execute(delete(StoredInterviewSession).where(StoredInterviewSession.id == identifier))
    for model in (QuestionAttempt, TranscriptionMeasurement):
        assert connection.execute(select(model.id).where(model.session_id == identifier)).first() is None


def test_data_survives_engine_disposal_and_reconstruction(postgres_engine):
    with postgres_engine.begin() as connection:
        identifier = add_session(connection)
        measurement = add_measurement(connection, identifier)
        attempt = add_attempt(connection, identifier, measurement_id=measurement)
    postgres_engine.dispose()
    rebuilt = create_database_engine(get_test_database_url())
    try:
        with Session(rebuilt) as session:
            assert session.get(StoredInterviewSession, identifier).questions == QUESTIONS
            assert session.get(QuestionAttempt, attempt).answer_text == "Submitted answer."
            assert session.get(QuestionAttempt, attempt).measurement_id == measurement
            assert session.get(TranscriptionMeasurement, measurement).recognized_word_count == 2
    finally:
        with rebuilt.begin() as connection:
            connection.execute(delete(StoredInterviewSession).where(StoredInterviewSession.id == identifier))
        rebuilt.dispose()
