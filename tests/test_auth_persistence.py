"""Authentication schema and ownership with explicitly inserted legacy rows.

All PostgreSQL writes and migration DDL roll back in the existing isolated test
database. Synthetic metrics exercise the real session service without providers.
TODO(auth): Provider and history routes remain transitional until later slices.
Nullable schema ownership preserves historical rows, never anonymous creation.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import DateTime, LargeBinary, MetaData, Table, Text, delete, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthenticatedPrincipal
from app.database_models import (
    AuthSession, Base, MEASUREMENT_VERSION, QuestionAttempt, StoredInterviewSession,
    TranscriptionMeasurement, User,
)
from app.sessions import AttemptRequest, ContinueRequest, InterviewSessionService, QUESTIONS, SessionNotFound
from app.transcription import TranscriptionResult
from test_measurement_persistence import delivery_for, metrics_for, original_result

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "0002_pause_delivery_metrics"
AUTH_REVISION = "0003_auth_user_ownership"
LEGACY_TABLES = ("interview_sessions", "question_attempts", "transcription_measurements")
OWNER_INDEX = "ix_interview_sessions_user_created_at"
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
ISSUER = "https://issuer.example/realm"


@pytest.fixture
def connection(postgres_engine):
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def migration_config(connection):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes.update(connection=connection, skip_logging=True)
    return config


def rejected(connection, operation, sqlstate):
    with pytest.raises(IntegrityError) as caught:
        with connection.begin_nested():
            operation()
    # Inspect only SQLSTATE, never exception messages, credentials or bound values.
    assert caught.value.orig.sqlstate == sqlstate


def add_user(connection, **changes):
    values = {"id": uuid4(), "auth_provider": ISSUER, "provider_subject": uuid4().hex, **changes}
    connection.execute(insert(User).values(**values))
    return values["id"]


def add_auth_session(connection, owner_id, **changes):
    values = {
        "id": uuid4(), "user_id": owner_id, "token_hash": sha256(uuid4().bytes).digest(),
        "created_at": NOW, "expires_at": NOW + timedelta(hours=1),
        "request_context": uuid4().hex, **changes,
    }
    connection.execute(insert(AuthSession).values(**values))
    return values["id"]


def add_session(connection, **changes):
    values = {"id": uuid4(), "questions": list(QUESTIONS), **changes}
    connection.execute(insert(StoredInterviewSession).values(**values))
    return values["id"]


def add_measurement(connection, session_id, **changes):
    span = 1.234567890123
    values = {
        "id": uuid4(), "session_id": session_id, "question_index": 0,
        "measurement_version": MEASUREMENT_VERSION, "measurement_source": "original_transcription",
        "recognized_word_count": 3, "um_count": 1, "uh_count": 1,
        "filler_unavailable_reason": None, "timed_utterance_span_seconds": span,
        "estimated_words_per_minute": 180 / span, "timing_unavailable_reason": None, **changes,
    }
    connection.execute(insert(TranscriptionMeasurement).values(**values))
    return values["id"]


def add_attempt(connection, session_id, **changes):
    values = {
        "id": uuid4(), "session_id": session_id, "question_index": 0,
        "attempt_number": 1, "answer_text": "Synthetic submitted answer.", **changes,
    }
    connection.execute(insert(QuestionAttempt).values(**values))
    return values["id"]


def test_auth_models_contain_only_approved_identity_and_digest_fields():
    assert set(User.__table__.columns.keys()) == {
        "id", "auth_provider", "provider_subject", "created_at",
    }
    assert set(AuthSession.__table__.columns.keys()) == {
        "id", "token_hash", "user_id", "created_at", "expires_at", "request_context",
    }
    for field in ("auth_provider", "provider_subject"):
        column = User.__table__.c[field]
        assert isinstance(column.type, Text)
        assert column.type.length is None
        assert column.type.collation == "C"
    assert isinstance(AuthSession.__table__.c.token_hash.type, LargeBinary)
    assert isinstance(AuthSession.__table__.c.request_context.type, Text)
    for model, fields in ((User, ("created_at",)), (AuthSession, ("created_at", "expires_at"))):
        assert all(not column.nullable for column in model.__table__.columns)
        for field in fields:
            assert isinstance(model.__table__.c[field].type, DateTime)
            assert model.__table__.c[field].type.timezone is True
    assert "user_id" not in QuestionAttempt.__table__.columns
    assert "user_id" not in TranscriptionMeasurement.__table__.columns
    owner = StoredInterviewSession.__table__.c.user_id
    assert owner.nullable is True
    assert owner.default is None and owner.server_default is None
    for model in (User, AuthSession, StoredInterviewSession):
        for relationship in inspect(model).relationships:
            assert not relationship.cascade.delete
            assert not relationship.cascade.delete_orphan


class HostileText(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("Subclass methods must not be called")

    def __str__(self):
        raise AssertionError("Subclass values must not be formatted")


class HostileBytes(bytes):
    def __len__(self):
        raise AssertionError("Subclass methods must not be called")


@pytest.mark.parametrize("model,field", [
    (User, "auth_provider"), (User, "provider_subject"), (AuthSession, "request_context"),
])
@pytest.mark.parametrize("value", [
    pytest.param(None, id="null"), pytest.param(1, id="integer"),
    pytest.param(True, id="boolean"), pytest.param(b"private-text", id="bytes"),
    pytest.param(["private-text"], id="list"), pytest.param({"private-text": "value"}, id="mapping"),
    pytest.param(HostileText("private-text"), id="subclass"),
    pytest.param("", id="empty"), pytest.param(" \t\r\n ", id="whitespace"),
    pytest.param("private\x00text", id="nul"),
])
def test_auth_orm_text_rejects_invalid_values_without_coercion_or_disclosure(model, field, value):
    with pytest.raises(ValueError) as caught:
        model(**{field: value})
    assert str(caught.value) == "Authentication text must be nonblank text without U+0000."


@pytest.mark.parametrize("value", [
    pytest.param(None, id="null"), pytest.param("x" * 32, id="text"),
    pytest.param(bytearray(b"x" * 32), id="bytearray"),
    pytest.param(memoryview(b"x" * 32), id="memoryview"),
    pytest.param(HostileBytes(b"x" * 32), id="subclass"),
    pytest.param(b"", id="empty"), pytest.param(b"x" * 31, id="short"),
    pytest.param(b"x" * 33, id="long"),
])
def test_auth_orm_accepts_only_an_exact_32_byte_digest_without_disclosure(value):
    with pytest.raises(ValueError) as caught:
        AuthSession(token_hash=value)
    assert str(caught.value) == "Authentication token hash must be exactly 32 bytes."


def test_auth_orm_preserves_exact_identity_and_keeps_sensitive_values_out_of_repr():
    provider, subject = " https://Issuer.example/re\u0301alm/ ", " Subject-\u00e9 "
    user = User(auth_provider=provider, provider_subject=subject)
    digest = b"private-digest-marker".ljust(32, b"x")
    context = "private-request-context-marker"
    record = AuthSession(token_hash=digest, request_context=context)
    assert (user.auth_provider, user.provider_subject) == (provider, subject)
    assert record.token_hash == digest
    assert record.request_context == context
    for rendered in (repr(record), str(record)):
        assert context not in rendered
        assert digest.decode() not in rendered
        assert digest.hex() not in rendered
        assert repr(digest) not in rendered


def test_user_and_auth_session_uuid_timestamp_defaults_and_digest_round_trip(connection):
    digest = sha256(b"synthetic-fixture-input").digest()
    with Session(bind=connection, join_transaction_mode="create_savepoint") as database:
        user = User(auth_provider=ISSUER, provider_subject="opaque-subject")
        database.add(user)
        database.flush()
        record = AuthSession(
            user_id=user.id, token_hash=digest, request_context="synthetic-login-context",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        database.add(record)
        database.flush()
        user_id, record_id = user.id, record.id
        database.expunge_all()
        loaded_user, loaded_auth = database.get(User, user_id), database.get(AuthSession, record_id)
        assert isinstance(loaded_user.id, UUID) and loaded_user.id.version == 4
        assert isinstance(loaded_auth.id, UUID) and loaded_auth.id.version == 4
        assert loaded_user.created_at.tzinfo is not None
        assert loaded_auth.created_at.tzinfo is not None
        assert loaded_auth.expires_at.tzinfo is not None
        assert loaded_auth.expires_at > loaded_auth.created_at
        assert loaded_auth.user_id == loaded_user.id
        assert loaded_auth.token_hash == digest
        assert loaded_auth.request_context == "synthetic-login-context"


def test_exact_provider_identity_is_unique_but_issuer_and_subject_separation_are_allowed(connection):
    first = add_user(connection, provider_subject="shared-subject")
    rejected(connection, lambda: add_user(connection, provider_subject="shared-subject"), "23505")
    other_issuer = add_user(connection, auth_provider="https://other.example/realm", provider_subject="shared-subject")
    other_subject = add_user(connection, provider_subject="different-subject")
    assert len({first, other_issuer, other_subject}) == 3
    assert connection.scalar(select(User.provider_subject).where(User.id == first)) == "shared-subject"


@pytest.mark.parametrize("field,first,second", [
    ("auth_provider", ISSUER, "https://Issuer.example/realm"),
    ("auth_provider", ISSUER, ISSUER + "/"),
    ("auth_provider", ISSUER, " " + ISSUER + " "),
    ("auth_provider", "https://issuer.example/r\u00e9alm", "https://issuer.example/re\u0301alm"),
    ("provider_subject", "Subject-1", "subject-1"),
    ("provider_subject", "subject-1", " subject-1 "),
    ("provider_subject", "subject-\u00e9", "subject-e\u0301"),
])
def test_identity_case_whitespace_and_unicode_normalization_remain_exact(connection, field, first, second):
    identifiers = [add_user(connection, **{"provider_subject": "shared", field: value}) for value in (first, second)]
    actual = [connection.scalar(select(User.__table__.c[field]).where(User.id == identifier))
              for identifier in identifiers]
    assert actual == [first, second]
    collations = dict(connection.execute(text(
        "SELECT attribute.attname, identity_collation.collname FROM pg_attribute AS attribute "
        "JOIN pg_class AS relation ON relation.oid = attribute.attrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
        "JOIN pg_collation AS identity_collation ON identity_collation.oid = attribute.attcollation "
        "WHERE namespace.nspname = current_schema() AND relation.relname = 'users' "
        "AND attribute.attname IN ('auth_provider', 'provider_subject')"
    )).all())
    assert collations == {"auth_provider": "C", "provider_subject": "C"}


@pytest.mark.parametrize("field", ["auth_provider", "provider_subject", "created_at"])
def test_user_required_fields_reject_explicit_nulls(connection, field):
    rejected(connection, lambda: add_user(connection, **{field: None}), "23502")


@pytest.mark.parametrize("field", ["auth_provider", "provider_subject"])
@pytest.mark.parametrize("value", ["", " ", "\t", "\n", "\r\n", " \t\r\n "])
def test_database_rejects_blank_identity_text(connection, field, value):
    rejected(connection, lambda: add_user(connection, **{field: value}), "23514")


def test_auth_session_requires_existing_user_and_unique_digest(connection):
    first_user, second_user = add_user(connection), add_user(connection)
    digest = sha256(b"synthetic-unique-digest").digest()
    first = add_auth_session(connection, first_user, token_hash=digest)
    rejected(connection, lambda: add_auth_session(connection, second_user, token_hash=digest), "23505")
    rejected(connection, lambda: add_auth_session(connection, uuid4()), "23503")
    assert connection.scalar(select(AuthSession.user_id).where(AuthSession.id == first)) == first_user


@pytest.mark.parametrize("field", ["token_hash", "user_id", "created_at", "expires_at", "request_context"])
def test_auth_session_required_fields_reject_explicit_nulls(connection, field):
    user_id = add_user(connection)
    rejected(connection, lambda: add_auth_session(connection, user_id, **{field: None}), "23502")


@pytest.mark.parametrize("size", [0, 1, 31, 33, 64])
def test_database_requires_a_32_byte_digest(connection, size):
    user_id = add_user(connection)
    rejected(connection, lambda: add_auth_session(connection, user_id, token_hash=b"x" * size), "23514")


@pytest.mark.parametrize("value", ["", " ", "\t", "\n", "\r\n", " \t\r\n "])
def test_database_rejects_blank_auth_request_context(connection, value):
    user_id = add_user(connection)
    rejected(connection, lambda: add_auth_session(connection, user_id, request_context=value), "23514")


@pytest.mark.parametrize("expires_at", [NOW, NOW - timedelta(microseconds=1)])
def test_auth_session_expiry_must_follow_creation(connection, expires_at):
    user_id = add_user(connection)
    rejected(connection, lambda: add_auth_session(connection, user_id, expires_at=expires_at), "23514")


@pytest.mark.parametrize("reference", ["auth_session", "interview_session"])
def test_user_deletion_is_restricted_without_cascading_referenced_records(connection, reference):
    user_id = add_user(connection)
    model = AuthSession if reference == "auth_session" else StoredInterviewSession
    identifier = (add_auth_session(connection, user_id) if reference == "auth_session"
                  else add_session(connection, user_id=user_id))
    foreign_key = next(key for key in inspect(connection).get_foreign_keys(model.__tablename__)
                       if key["constrained_columns"] == ["user_id"])
    assert foreign_key["referred_table"] == "users"
    assert foreign_key["referred_columns"] == ["id"]
    assert foreign_key["options"]["ondelete"] == "RESTRICT"
    rejected(connection, lambda: connection.execute(delete(User).where(User.id == user_id)), "23001")
    assert connection.scalar(select(model.user_id).where(model.id == identifier)) == user_id
    assert connection.scalar(select(User.id).where(User.id == user_id)) == user_id
    connection.execute(delete(model).where(model.id == identifier))
    connection.execute(delete(User).where(User.id == user_id))
    assert connection.scalar(select(User.id).where(User.id == user_id)) is None


def test_owned_session_fk_and_legacy_null_rows_remain_supported_by_schema(connection):
    # Explicit legacy insertion proves schema compatibility. The owned service's
    # normal start path must always persist its authenticated principal's user ID.
    user_id = add_user(connection)
    owned = add_session(connection, user_id=user_id)
    anonymous = add_session(connection, user_id=None)
    assert connection.scalar(select(StoredInterviewSession.user_id).where(StoredInterviewSession.id == owned)) == user_id
    assert connection.scalar(select(StoredInterviewSession.user_id).where(StoredInterviewSession.id == anonymous)) is None
    rejected(connection, lambda: add_session(connection, user_id=uuid4()), "23503")
    rejected(connection, lambda: connection.execute(update(StoredInterviewSession).where(
        StoredInterviewSession.id == owned).values(user_id=uuid4())), "23503")
    columns = {column["name"]: column for column in inspect(connection).get_columns("interview_sessions")}
    assert columns["user_id"]["nullable"] is True
    assert columns["user_id"]["default"] is None
    index = next(index for index in inspect(connection).get_indexes("interview_sessions")
                 if index["name"] == OWNER_INDEX)
    assert index["column_names"] == ["user_id", "created_at", "id"]
    assert index["unique"] is False


def test_owned_children_keep_exact_session_question_links_and_existing_deletion_policy(connection):
    user_id = add_user(connection)
    first, second = add_session(connection, user_id=user_id), add_session(connection, user_id=user_id)
    measurement = add_measurement(connection, first)
    for model in (QuestionAttempt, TranscriptionMeasurement):
        assert "user_id" not in {column["name"] for column in inspect(connection).get_columns(model.__tablename__)}
    rejected(connection, lambda: add_attempt(connection, second, measurement_id=measurement), "23503")
    rejected(connection, lambda: add_attempt(connection, first, question_index=1, measurement_id=measurement), "23503")
    rejected(connection, lambda: add_attempt(connection, first, measurement_id=uuid4()), "23503")
    rejected(connection, lambda: add_attempt(connection, uuid4()), "23503")
    rejected(connection, lambda: add_measurement(connection, uuid4()), "23503")
    attempt = add_attempt(connection, first, measurement_id=measurement)
    rejected(connection, lambda: add_attempt(connection, first, attempt_number=2, measurement_id=measurement), "23505")
    rejected(connection, lambda: connection.execute(delete(TranscriptionMeasurement).where(
        TranscriptionMeasurement.id == measurement)), "23503")
    connection.execute(delete(StoredInterviewSession).where(StoredInterviewSession.id == first))
    assert connection.scalar(select(QuestionAttempt.id).where(QuestionAttempt.id == attempt)) is None
    assert connection.scalar(select(TranscriptionMeasurement.id).where(TranscriptionMeasurement.id == measurement)) is None
    assert connection.scalar(select(User.id).where(User.id == user_id)) == user_id


@pytest.mark.parametrize("changes", [
    {"status": "completed", "current_question_index": 5},
    {"status": "completed", "current_question_index": 4, "completed_at": NOW},
    {"status": "active", "current_question_index": 5},
    {"completed_at": NOW},
    {"status": "completed", "current_question_index": 5,
     "created_at": NOW, "completed_at": NOW - timedelta(microseconds=1)},
])
def test_owned_sessions_preserve_completion_constraints(connection, changes):
    user_id = add_user(connection)
    rejected(connection, lambda: add_session(connection, user_id=user_id, **changes), "23514")


def rows(connection, table):
    return {row["id"]: dict(row) for row in connection.execute(select(table)).mappings()}


def prior_schema_facts(connection):
    """Snapshot every pre-auth constraint, index, trigger and trigger function."""
    return {
        "constraints": dict(connection.execute(text(
            "SELECT relation.relname || '.' || constraint_row.conname, "
            "pg_get_constraintdef(constraint_row.oid) FROM pg_constraint AS constraint_row "
            "JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = current_schema() "
            "AND relation.relname IN ('interview_sessions', 'question_attempts', 'transcription_measurements')"
        )).all()),
        "indexes": dict(connection.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename IN ('interview_sessions', 'question_attempts', 'transcription_measurements')"
        )).all()),
        "triggers": dict(connection.execute(text(
            "SELECT relation.relname || '.' || trigger_row.tgname, "
            "pg_get_triggerdef(trigger_row.oid) || ':' || CAST(trigger_row.tgenabled AS text) "
            "FROM pg_trigger AS trigger_row JOIN pg_class AS relation ON relation.oid = trigger_row.tgrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE NOT trigger_row.tgisinternal AND namespace.nspname = current_schema() "
            "AND relation.relname IN ('interview_sessions', 'question_attempts', 'transcription_measurements')"
        )).all()),
        "functions": dict(connection.execute(text(
            "SELECT function_row.proname, pg_get_functiondef(function_row.oid) FROM pg_proc AS function_row "
            "JOIN pg_namespace AS namespace ON namespace.oid = function_row.pronamespace "
            "WHERE namespace.nspname = current_schema() "
            "AND function_row.proname IN ('rehearse_preserve_measurement', 'rehearse_preserve_questions')"
        )).all()),
    }


def test_populated_0002_upgrade_downgrade_and_reupgrade_preserve_all_legacy_facts(connection):
    config = migration_config(connection)
    command.downgrade(config, PREVIOUS_REVISION)
    baseline = MetaData()
    tables = {name: Table(name, baseline, autoload_with=connection) for name in LEGACY_TABLES}
    completed = add_session(connection, status="completed", current_question_index=5,
                            created_at=NOW, completed_at=NOW + timedelta(minutes=3))
    active = add_session(connection, created_at=NOW)
    available = add_measurement(connection, completed, delivery_measurement_version="pause-metrics-v1",
                                pause_count=1, total_pause_duration_seconds=0.987654321234,
                                longest_pause_seconds=0.987654321234, pause_unavailable_reason=None)
    unavailable = add_measurement(
        connection, completed, recognized_word_count=0, um_count=None, uh_count=None,
        filler_unavailable_reason="unsupported_language", timed_utterance_span_seconds=None,
        estimated_words_per_minute=None, timing_unavailable_reason="missing_timings",
        delivery_measurement_version="pause-metrics-v1", pause_count=None,
        total_pause_duration_seconds=None, longest_pause_seconds=None, pause_unavailable_reason="missing_timings",
    )
    legacy_delivery = add_measurement(connection, active)
    first_attempt = add_attempt(connection, completed, measurement_id=available)
    retry_attempt = add_attempt(connection, completed, attempt_number=2, measurement_id=unavailable)
    for question in range(1, len(QUESTIONS)):
        add_attempt(connection, completed, question_index=question)
    add_attempt(connection, active, measurement_id=legacy_delivery)
    prior_rows = {name: rows(connection, table) for name, table in tables.items()}
    prior_facts = prior_schema_facts(connection)
    assert set(prior_facts["functions"]) == {"rehearse_preserve_measurement", "rehearse_preserve_questions"}
    assert len(prior_facts["triggers"]) == 2

    command.upgrade(config, AUTH_REVISION)

    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == AUTH_REVISION
    assert set(inspect(connection).get_table_names()) == set(LEGACY_TABLES) | {"alembic_version", "users", "auth_sessions"}
    upgraded_sessions = Table("interview_sessions", MetaData(), autoload_with=connection)
    upgraded_rows = rows(connection, upgraded_sessions)
    for identifier, old in prior_rows["interview_sessions"].items():
        assert {field: upgraded_rows[identifier][field] for field in old} == old
        assert upgraded_rows[identifier]["user_id"] is None
    for name in LEGACY_TABLES[1:]:
        assert rows(connection, tables[name]) == prior_rows[name]
    assert connection.scalar(select(User.id).limit(1)) is None
    assert connection.scalar(select(AuthSession.id).limit(1)) is None
    after_facts = prior_schema_facts(connection)
    for kind, old in prior_facts.items():
        assert {name: after_facts[kind][name] for name in old} == old
    assert set(after_facts["constraints"]) - set(prior_facts["constraints"]) == {
        "interview_sessions.fk_interview_sessions_user",
    }
    assert set(after_facts["indexes"]) - set(prior_facts["indexes"]) == {OWNER_INDEX}
    assert after_facts["triggers"] == prior_facts["triggers"]
    assert after_facts["functions"] == prior_facts["functions"]
    assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    assert prior_rows["question_attempts"][first_attempt]["measurement_id"] == available
    assert prior_rows["question_attempts"][retry_attempt]["measurement_id"] == unavailable
    assert prior_rows["transcription_measurements"][available]["timed_utterance_span_seconds"] == 1.234567890123
    assert prior_rows["transcription_measurements"][available]["total_pause_duration_seconds"] == 0.987654321234
    assert prior_rows["transcription_measurements"][unavailable]["pause_unavailable_reason"] == "missing_timings"
    assert prior_rows["transcription_measurements"][legacy_delivery]["delivery_measurement_version"] is None
    rejected(connection, lambda: connection.execute(update(TranscriptionMeasurement).where(
        TranscriptionMeasurement.id == available).values(recognized_word_count=4)), "23514")
    rejected(connection, lambda: connection.execute(update(StoredInterviewSession).where(
        StoredInterviewSession.id == active).values(questions=["Changed", *QUESTIONS[1:]])), "23514")

    owner = add_user(connection)
    add_auth_session(connection, owner)
    owned = add_session(connection, user_id=owner)
    owned_measurement = add_measurement(connection, owned)
    add_attempt(connection, owned, measurement_id=owned_measurement)
    expected_legacy_rows = {name: rows(connection, table) for name, table in tables.items()}

    command.downgrade(config, PREVIOUS_REVISION)

    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS_REVISION
    assert set(inspect(connection).get_table_names()) == set(LEGACY_TABLES) | {"alembic_version"}
    assert {column["name"] for column in inspect(connection).get_columns("interview_sessions")} == set(tables["interview_sessions"].columns.keys())
    assert prior_schema_facts(connection) == prior_facts
    for name, table in tables.items():
        assert rows(connection, table) == expected_legacy_rows[name]
    rejected(connection, lambda: connection.execute(update(TranscriptionMeasurement).where(
        TranscriptionMeasurement.id == owned_measurement).values(recognized_word_count=4)), "23514")

    command.upgrade(config, AUTH_REVISION)

    # Downgrade intentionally removes auth records and owners, preserving history.
    # Re-upgrade cannot reconstruct an identity or claim the retained session UUID.
    assert connection.scalar(select(User.id).limit(1)) is None
    assert connection.scalar(select(AuthSession.id).limit(1)) is None
    assert rows(connection, upgraded_sessions) == {
        identifier: {**old, "user_id": None}
        for identifier, old in expected_legacy_rows["interview_sessions"].items()
    }
    for name in LEGACY_TABLES[1:]:
        assert rows(connection, tables[name]) == expected_legacy_rows[name]
    assert prior_schema_facts(connection) == after_facts
    assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_owned_session_real_service_preserves_retry_completion_and_exact_measurement_links(connection):
    factory = sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    owner = add_user(connection)
    principal = AuthenticatedPrincipal(user_id=owner, auth_session_id=uuid4(), request_context="synthetic-owned-context")
    service = InterviewSessionService(factory, principal)
    legacy = add_session(connection, user_id=None)
    created = service.start()
    assert connection.scalar(select(StoredInterviewSession.user_id).where(StoredInterviewSession.id == created.id)) == owner
    assert connection.scalar(select(StoredInterviewSession.user_id).where(StoredInterviewSession.id == legacy)) is None
    with pytest.raises(SessionNotFound, match="^Session not found\\.$"):
        service.get(legacy)
    first_result = original_result()
    retry_result = TranscriptionResult(text="Um, uh hello", language="eng", words=[
        {"text": "Um,", "start": 2.0, "end": 2.25},
        {"text": "uh", "start": 2.876543210987, "end": 3.0},
        {"text": "hello", "start": 3.0, "end": 3.876543210987},
    ])
    measurements = [service.create_measurement(
        created.id, 0, metrics_for(result), expected_last_attempt_number=0,
        delivery_metrics=delivery_for(result),
    ) for result in (first_result, retry_result)]
    before_measurements = rows(connection, TranscriptionMeasurement.__table__)
    attempts = []
    for revision, measurement in enumerate(measurements):
        submitted = service.submit_attempt(created.id, 0, AttemptRequest(
            expected_last_attempt_number=revision, answer=f"Answer revision {revision + 1}",
            measurement_id=measurement,
        ))
        assert submitted.session.current_question_index == 0
        assert submitted.session.answers == []
        assert submitted.attempt.attempt_number == revision + 1
        assert submitted.attempt.measurement_id == measurement
        attempts.append(submitted.attempt)
    before_attempts = rows(connection, QuestionAttempt.__table__)
    comparison = service.get_comparison(created.id, 0)
    assert comparison.before_attempt.attempt_number == 1
    assert comparison.after_attempt.attempt_number == 2
    assert comparison.before_attempt.measurement_id == measurements[0]
    assert comparison.after_attempt.measurement_id == measurements[1]
    for field in ("timed_utterance_span_seconds", "estimated_words_per_minute"):
        change = getattr(comparison.comparison, field)
        assert change.delta == before_measurements[measurements[1]][field] - before_measurements[measurements[0]][field]
        assert change.delta != round(change.delta, 2)
    assert comparison.delivery_comparison.pause_count.before == 0
    assert comparison.delivery_comparison.pause_count.after == 1
    updated = service.continue_question(created.id, 0, ContinueRequest(expected_last_attempt_number=2))
    assert updated.current_question_index == 1
    assert updated.answers == ["Answer revision 2"]
    assert rows(connection, QuestionAttempt.__table__) == before_attempts
    for question in range(1, len(QUESTIONS)):
        service.submit_attempt(created.id, question, AttemptRequest(
            expected_last_attempt_number=0, answer=f"Final answer {question}",
        ))
        updated = service.continue_question(created.id, question, ContinueRequest(expected_last_attempt_number=1))
    assert updated.status == "completed"
    assert updated.current_question_index == len(QUESTIONS)
    assert updated.current_question is None
    assert updated.answers == ["Answer revision 2", *(f"Final answer {question}" for question in range(1, len(QUESTIONS)))]
    assert service.get_attempts(created.id, 0) == attempts
    assert service.get_comparison(created.id, 0) == comparison
    assert rows(connection, TranscriptionMeasurement.__table__) == before_measurements
    for identifier, old in before_attempts.items():
        assert rows(connection, QuestionAttempt.__table__)[identifier] == old
    stored = connection.execute(select(StoredInterviewSession.__table__).where(
        StoredInterviewSession.id == created.id)).mappings().one()
    assert stored["user_id"] == owner
    assert stored["completed_at"].tzinfo is not None
    assert stored["completed_at"] >= stored["created_at"]
