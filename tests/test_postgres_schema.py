"""Real PostgreSQL only. Destructive DDL is confined to an explicit test target.

    Missing local configuration skips with BLOCKED_BY_LOCAL_DB_ENV. CI requires
    PostgreSQL and must fail, rather than silently skip, if configuration is absent.
"""
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, MetaData, Table, delete, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import create_database_engine, get_test_database_url
from app.database_models import (
    Base, MEASUREMENT_VERSION, QuestionAttempt, StoredInterviewSession,
    TranscriptionMeasurement,
)
from app.sessions import QUESTIONS

ROOT = Path(__file__).resolve().parents[1]
TABLES = {"interview_sessions", "question_attempts", "transcription_measurements"}
DELIVERY_FIELDS = (
    "delivery_measurement_version", "pause_count", "total_pause_duration_seconds",
    "longest_pause_seconds", "pause_unavailable_reason",
)
DELIVERY_CONSTRAINTS = {
    "ck_measurements_delivery_state", "ck_measurements_delivery_version",
    "ck_measurements_pause_reason", "ck_measurements_pause_count",
    "ck_measurements_finite_pause_total", "ck_measurements_finite_pause_longest",
    "ck_measurements_pause_durations", "ck_measurements_pause_word_bound",
}
AVAILABLE_DELIVERY = {
    "delivery_measurement_version": "pause-metrics-v1", "pause_count": 1,
    "total_pause_duration_seconds": 1.234567890123,
    "longest_pause_seconds": 1.234567890123, "pause_unavailable_reason": None,
}
TIMING_REASONS = (
    "missing_timings", "timing_coverage_mismatch", "invalid_timing",
    "invalid_timing_order", "unusable_span",
)


def migration_config(connection):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes.update(connection=connection, skip_logging=True)
    return config


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


def test_initial_and_delivery_migrations_upgrade_empty_database_and_downgrade_deterministically(postgres_engine):
    with postgres_engine.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) - {"alembic_version"} == set()
        assert connection.scalar(text(
            "SELECT count(*) FROM pg_proc WHERE proname IN "
            "('rehearse_preserve_measurement', 'rehearse_preserve_questions')"
        )) == 0
        command.upgrade(config, "0001_database_foundation")
        initial_columns = {column["name"] for column in inspect(connection).get_columns("transcription_measurements")}
        assert initial_columns.isdisjoint(DELIVERY_FIELDS)
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) == TABLES | {"alembic_version"}
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_pause_delivery_metrics"
        assert {column["name"] for column in inspect(connection).get_columns("transcription_measurements")} == initial_columns | set(DELIVERY_FIELDS)
        command.downgrade(config, "0001_database_foundation")
        assert {column["name"] for column in inspect(connection).get_columns("transcription_measurements")} == initial_columns
        assert set(inspect(connection).get_table_names()) == TABLES | {"alembic_version"}
        command.upgrade(config, "head")


def test_migrated_schema_matches_orm_metadata(connection):
    assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    actual_constraints = {constraint["name"] for constraint in inspect(connection).get_check_constraints("transcription_measurements")}
    expected_constraints = {constraint.name for constraint in TranscriptionMeasurement.__table__.constraints
                            if isinstance(constraint, CheckConstraint)}
    assert actual_constraints == expected_constraints
    assert DELIVERY_CONSTRAINTS <= actual_constraints
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_pause_delivery_metrics"


def test_populated_foundation_upgrade_preserves_legacy_rows_links_and_speaking_facts(connection):
    config = migration_config(connection)
    command.downgrade(config, "0001_database_foundation")
    baseline = MetaData()
    measurements = Table("transcription_measurements", baseline, autoload_with=connection)
    attempts = Table("question_attempts", baseline, autoload_with=connection)
    sessions = Table("interview_sessions", baseline, autoload_with=connection)
    identifier = add_session(connection)
    available_id, unavailable_id = uuid4(), uuid4()
    connection.execute(insert(measurements), [
        {
            "id": available_id, "session_id": identifier, "question_index": 0,
            "measurement_version": MEASUREMENT_VERSION, "measurement_source": "original_transcription",
            "recognized_word_count": 2, "um_count": 0, "uh_count": 1,
            "filler_unavailable_reason": None, "timed_utterance_span_seconds": 1.234567890123,
            "estimated_words_per_minute": 97.20000087480599, "timing_unavailable_reason": None,
        },
        {
            "id": unavailable_id, "session_id": identifier, "question_index": 1,
            "measurement_version": MEASUREMENT_VERSION, "measurement_source": "original_transcription",
            "recognized_word_count": 0, "um_count": None, "uh_count": None,
            "filler_unavailable_reason": "unsupported_language", "timed_utterance_span_seconds": None,
            "estimated_words_per_minute": None, "timing_unavailable_reason": "missing_timings",
        },
    ])
    first_attempt = add_attempt(connection, identifier, measurement_id=available_id)
    second_attempt = add_attempt(connection, identifier, question_index=1, measurement_id=unavailable_id)
    prior_measurements = {row["id"]: dict(row) for row in connection.execute(select(measurements)).mappings()}
    prior_attempts = {row["id"]: dict(row) for row in connection.execute(select(attempts)).mappings()}
    prior_sessions = {row["id"]: dict(row) for row in connection.execute(select(sessions)).mappings()}
    prior_trigger = connection.scalar(text(
        "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = 'rehearse_preserve_measurement'"
    ))

    command.upgrade(config, "head")

    migrated = {row["id"]: dict(row) for row in connection.execute(select(TranscriptionMeasurement.__table__)).mappings()}
    assert set(migrated) == set(prior_measurements)
    for measurement_id, old in prior_measurements.items():
        assert {field: migrated[measurement_id][field] for field in old} == old
        assert all(migrated[measurement_id][field] is None for field in DELIVERY_FIELDS)
    assert {row["id"]: dict(row) for row in connection.execute(select(attempts)).mappings()} == prior_attempts
    assert {row["id"]: dict(row) for row in connection.execute(select(sessions)).mappings()} == prior_sessions
    assert prior_attempts[first_attempt]["measurement_id"] == available_id
    assert prior_attempts[second_attempt]["measurement_id"] == unavailable_id
    assert connection.scalar(text(
        "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = 'rehearse_preserve_measurement'"
    )) == prior_trigger
    assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []

    fresh_id = add_measurement(connection, identifier, **AVAILABLE_DELIVERY)
    fresh_attempt = add_attempt(connection, identifier, attempt_number=2, measurement_id=fresh_id)
    new_old_projection = dict(connection.execute(select(measurements).where(measurements.c.id == fresh_id)).mappings().one())
    fresh_attempt_projection = dict(connection.execute(select(attempts).where(attempts.c.id == fresh_attempt)).mappings().one())
    command.downgrade(config, "0001_database_foundation")
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_database_foundation"
    assert {column["name"] for column in inspect(connection).get_columns("transcription_measurements")} == set(measurements.columns.keys())
    assert {constraint["name"] for constraint in inspect(connection).get_check_constraints("transcription_measurements")}.isdisjoint(DELIVERY_CONSTRAINTS)
    assert {row["id"]: dict(row) for row in connection.execute(select(measurements)).mappings()} == {
        **prior_measurements, fresh_id: new_old_projection,
    }
    assert {row["id"]: dict(row) for row in connection.execute(select(attempts)).mappings()} == {
        **prior_attempts, fresh_attempt: fresh_attempt_projection,
    }
    assert connection.scalar(text(
        "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = 'rehearse_preserve_measurement'"
    )) == prior_trigger
    rejected(connection, lambda: connection.execute(update(measurements).where(
        measurements.c.id == fresh_id).values(recognized_word_count=3)), "23514")

    command.upgrade(config, "head")
    # Downgrade intentionally removes delivery facts, so re-upgrade creates truthful
    # legacy NULLs; it cannot fabricate the former analysis from stored speech facts.
    assert all(connection.scalar(select(TranscriptionMeasurement.__table__.c[field]).where(
        TranscriptionMeasurement.id == fresh_id)) is None for field in DELIVERY_FIELDS)


@pytest.mark.parametrize("delivery,recognized", [
    ({field: None for field in DELIVERY_FIELDS}, 2),
    ({**AVAILABLE_DELIVERY, "pause_count": 0, "total_pause_duration_seconds": 0.0,
      "longest_pause_seconds": 0.0}, 1),
    (AVAILABLE_DELIVERY, 2),
    ({**AVAILABLE_DELIVERY, "pause_count": 2, "total_pause_duration_seconds": 1.987654321234,
      "longest_pause_seconds": 1.111111111111}, 3),
    *(({"delivery_measurement_version": "pause-metrics-v1", "pause_count": None,
       "total_pause_duration_seconds": None, "longest_pause_seconds": None,
       "pause_unavailable_reason": reason}, 2) for reason in TIMING_REASONS),
])
def test_delivery_available_unavailable_and_legacy_states_round_trip_exactly(connection, delivery, recognized):
    identifier = add_session(connection)
    measurement = add_measurement(connection, identifier, recognized_word_count=recognized, **delivery)
    row = connection.execute(select(TranscriptionMeasurement.__table__).where(
        TranscriptionMeasurement.id == measurement)).one()
    assert {field: getattr(row, field) for field in DELIVERY_FIELDS} == delivery
    assert row.measurement_version == MEASUREMENT_VERSION
    assert row.measurement_source == "original_transcription"
    assert row.recognized_word_count == recognized
    assert row.timed_utterance_span_seconds == 1.234567890123


_ALLOWED_DELIVERY_NULL_PATTERNS = {
    (False, False, False, False, False),  # Historical analysis absent.
    (True, True, True, True, False),  # Available.
    (True, False, False, False, True),  # Unavailable.
}
_INVALID_DELIVERY_NULL_PATTERNS = [
    pattern for pattern in product((False, True), repeat=5)
    if pattern not in _ALLOWED_DELIVERY_NULL_PATTERNS
]


@pytest.mark.parametrize("nonnull", _INVALID_DELIVERY_NULL_PATTERNS,
                         ids=["".join("1" if present else "0" for present in pattern)
                              for pattern in _INVALID_DELIVERY_NULL_PATTERNS])
def test_every_other_delivery_null_combination_is_rejected(connection, nonnull):
    identifier = add_session(connection)
    available = ("pause-metrics-v1", 1, 1.25, 1.25, "missing_timings")
    values = {field: value if present else None for field, value, present in zip(DELIVERY_FIELDS, available, nonnull)}
    rejected(connection, lambda: add_measurement(connection, identifier, **values), "23514")


@pytest.mark.parametrize("version", ["", " ", "\t", "\n", "\r\n", " \t \n "])
@pytest.mark.parametrize("unavailable", [False, True])
def test_blank_delivery_version_rejected_for_both_recorded_states(connection, version, unavailable):
    identifier = add_session(connection)
    values = ({"delivery_measurement_version": version, "pause_count": None,
               "total_pause_duration_seconds": None, "longest_pause_seconds": None,
               "pause_unavailable_reason": "missing_timings"} if unavailable
              else {**AVAILABLE_DELIVERY, "delivery_measurement_version": version})
    rejected(connection, lambda: add_measurement(connection, identifier, **values), "23514")


@pytest.mark.parametrize("reason", ["", "unknown", "no_pauses", "insufficient_words", "pause_error", "delivery_error"])
def test_pause_unavailable_reason_is_closed_to_existing_timing_reasons(connection, reason):
    identifier = add_session(connection)
    values = {"delivery_measurement_version": "pause-metrics-v1", "pause_count": None,
              "total_pause_duration_seconds": None, "longest_pause_seconds": None,
              "pause_unavailable_reason": reason}
    rejected(connection, lambda: add_measurement(connection, identifier, **values), "23514")


@pytest.mark.parametrize("changes", [
    {"pause_count": -1}, {"total_pause_duration_seconds": -1.0}, {"longest_pause_seconds": -1.0},
    *({field: value} for field in ("total_pause_duration_seconds", "longest_pause_seconds")
      for value in (float("nan"), float("inf"), float("-inf"))),
    {"pause_count": 0, "total_pause_duration_seconds": 1.0, "longest_pause_seconds": 0.0},
    {"pause_count": 0, "total_pause_duration_seconds": 0.0, "longest_pause_seconds": 1.0},
    {"total_pause_duration_seconds": 0.0}, {"longest_pause_seconds": 0.0},
    {"total_pause_duration_seconds": 0.5, "longest_pause_seconds": 0.6},
    {"recognized_word_count": 0, "pause_count": 0, "total_pause_duration_seconds": 0.0,
     "longest_pause_seconds": 0.0},
    {"pause_count": 2},
])
def test_invalid_delivery_numbers_and_word_bounds_rejected(connection, changes):
    identifier = add_session(connection)
    rejected(connection, lambda: add_measurement(connection, identifier, **{**AVAILABLE_DELIVERY, **changes}), "23514")


def test_database_delivery_constraints_do_not_recalculate_approximate_threshold_arithmetic(connection):
    identifier = add_session(connection)
    # Application Decimal timing arithmetic owns the operational threshold.
    # Database checks enforce scalar state/coherence without a floating-point
    # total >= count * threshold expression or an unapproved span comparison.
    measurement = add_measurement(connection, identifier, **{
        **AVAILABLE_DELIVERY, "total_pause_duration_seconds": 0.499999999999,
        "longest_pause_seconds": 0.499999999999,
    })
    row = connection.execute(select(TranscriptionMeasurement.__table__).where(
        TranscriptionMeasurement.id == measurement)).one()
    assert row.pause_count == 1
    assert row.total_pause_duration_seconds == row.longest_pause_seconds == 0.499999999999


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("field,replacement", [
    ("delivery_measurement_version", "pause-metrics-future"), ("pause_count", 2),
    ("total_pause_duration_seconds", 2.0), ("longest_pause_seconds", 0.5),
    ("pause_unavailable_reason", "invalid_timing"),
])
def test_each_delivery_field_is_immutable_before_and_after_attempt_linking(connection, linked, field, replacement):
    identifier = add_session(connection)
    values = ({"delivery_measurement_version": "pause-metrics-v1", "pause_count": None,
               "total_pause_duration_seconds": None, "longest_pause_seconds": None,
               "pause_unavailable_reason": "missing_timings"} if field == "pause_unavailable_reason"
              else {**AVAILABLE_DELIVERY, "longest_pause_seconds": 0.75})
    measurement = add_measurement(connection, identifier, recognized_word_count=3, **values)
    attempt = add_attempt(connection, identifier, measurement_id=measurement) if linked else None
    before = dict(connection.execute(select(TranscriptionMeasurement.__table__).where(
        TranscriptionMeasurement.id == measurement)).mappings().one())
    # Every replacement would satisfy the state constraints if updates were
    # permitted, proving rejection comes from whole-row immutability.
    rejected(connection, lambda: connection.execute(update(TranscriptionMeasurement).where(
        TranscriptionMeasurement.id == measurement).values(**{field: replacement})), "23514")
    after = dict(connection.execute(select(TranscriptionMeasurement.__table__).where(
        TranscriptionMeasurement.id == measurement)).mappings().one())
    assert after == before
    if attempt is not None:
        assert connection.scalar(select(QuestionAttempt.measurement_id).where(QuestionAttempt.id == attempt)) == measurement


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
