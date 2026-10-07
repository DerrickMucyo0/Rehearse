"""OIDC transaction mappings and reversible PostgreSQL-only schema extension."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import (
    CheckConstraint, DateTime, LargeBinary, MetaData, Text, UniqueConstraint,
    insert, inspect, select, text, update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database_models import (
    AuthSession, Base, MEASUREMENT_VERSION, OIDCLoginTransaction, QuestionAttempt,
    StoredInterviewSession, TranscriptionMeasurement, User,
)
from app.sessions import QUESTIONS

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "0003_auth_user_ownership"
LOGIN_REVISION = "0004_oidc_login_transactions"
TABLE_NAME = "oidc_login_transactions"
PRIOR_MODELS = (User, AuthSession, StoredInterviewSession, TranscriptionMeasurement, QuestionAttempt)
PRIOR_TABLES = {model.__tablename__ for model in PRIOR_MODELS}
PRIOR_TABLE_SQL = "('users', 'auth_sessions', 'interview_sessions', 'transcription_measurements', 'question_attempts')"
CHECK_NAMES = {
    "ck_oidc_login_transactions_state_hash_length",
    "ck_oidc_login_transactions_nonce_nonblank",
    "ck_oidc_login_transactions_code_verifier",
    "ck_oidc_login_transactions_expiration",
}
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
VERIFIER = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


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
    # Do not render PostgreSQL messages or bound authentication material.
    assert caught.value.orig.sqlstate == sqlstate


def add_transaction(connection, **changes):
    values = {
        "id": uuid4(), "state_hash": sha256(uuid4().bytes).digest(),
        "nonce": "synthetic-independent-nonce", "code_verifier": VERIFIER,
        "created_at": NOW, "expires_at": NOW + timedelta(minutes=10), **changes,
    }
    connection.execute(insert(OIDCLoginTransaction).values(**values))
    return values["id"]


def previous_metadata():
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name != TABLE_NAME:
            table.to_metadata(metadata)
    return metadata


def test_login_mapping_contains_only_the_approved_server_side_fields_and_defaults():
    table = OIDCLoginTransaction.__table__
    assert set(table.columns.keys()) == {
        "id", "state_hash", "nonce", "code_verifier", "created_at", "expires_at",
    }
    assert all(not column.nullable for column in table.columns)
    assert not table.foreign_keys
    assert not inspect(OIDCLoginTransaction).relationships
    assert table.c.id.primary_key
    assert table.c.id.type.as_uuid is True
    first, second = table.c.id.default.arg(None), table.c.id.default.arg(None)
    assert isinstance(first, UUID) and first.version == 4
    assert isinstance(second, UUID) and second.version == 4 and first != second
    assert table.c.id.server_default is None
    assert isinstance(table.c.state_hash.type, LargeBinary)
    assert table.c.state_hash.default is None and table.c.state_hash.server_default is None
    for field in ("nonce", "code_verifier"):
        column = table.c[field]
        assert isinstance(column.type, Text) and column.type.length is None
        assert column.type.collation == "C"
        assert column.default is None and column.server_default is None
    for field in ("created_at", "expires_at"):
        assert isinstance(table.c[field].type, DateTime)
        assert table.c[field].type.timezone is True
    assert str(table.c.created_at.server_default.arg) == "now()"
    assert table.c.expires_at.default is None and table.c.expires_at.server_default is None
    assert {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)} == CHECK_NAMES
    assert {(constraint.name, tuple(constraint.columns.keys())) for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)} == {
        ("uq_oidc_login_transactions_state_hash", ("state_hash",)),
    }
    assert {(index.name, tuple(index.columns.keys()), index.unique) for index in table.indexes} == {
        ("ix_oidc_login_transactions_expires_at", ("expires_at",), False),
    }


class HostileText(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("Subclass methods must not be called")

    def __str__(self):
        raise AssertionError("Subclass values must not be formatted")


class HostileBytes(bytes):
    def __len__(self):
        raise AssertionError("Subclass methods must not be called")


@pytest.mark.parametrize("value", [
    pytest.param(None, id="null"), pytest.param("sensitive-digest" * 2, id="text"),
    pytest.param(bytearray(b"x" * 32), id="bytearray"),
    pytest.param(memoryview(b"x" * 32), id="memoryview"),
    pytest.param(HostileBytes(b"x" * 32), id="subclass"),
    pytest.param(b"", id="empty"), pytest.param(b"x" * 31, id="short"),
    pytest.param(b"x" * 33, id="long"),
])
def test_login_digest_validator_requires_exact_bytes_without_disclosure(value):
    with pytest.raises(ValueError) as caught:
        OIDCLoginTransaction(state_hash=value)
    assert str(caught.value) == "OIDC login state hash must be exactly 32 bytes."


@pytest.mark.parametrize("value", [
    pytest.param(None, id="null"), pytest.param(1, id="integer"),
    pytest.param(b"sensitive-nonce", id="bytes"),
    pytest.param(HostileText("sensitive-nonce"), id="subclass"),
    pytest.param("", id="empty"), pytest.param(" \t\r\n ", id="whitespace"),
    pytest.param("sensitive\x00nonce", id="nul"),
])
def test_nonce_validator_is_strict_nonblank_and_sanitized(value):
    with pytest.raises(ValueError) as caught:
        OIDCLoginTransaction(nonce=value)
    assert str(caught.value) == "Authentication text must be nonblank text without U+0000."


@pytest.mark.parametrize("value", [
    pytest.param(None, id="null"), pytest.param(1, id="integer"),
    pytest.param(b"A" * 43, id="bytes"), pytest.param(HostileText("A" * 43), id="subclass"),
    pytest.param("A" * 42, id="short"), pytest.param("A" * 129, id="long"),
    pytest.param("A" * 42 + "é", id="non-ascii"), pytest.param("A" * 42 + "/", id="slash"),
    pytest.param("A" * 42 + "+", id="plus"), pytest.param("A" * 42 + "=", id="padding"),
    pytest.param("A" * 42 + " ", id="space"), pytest.param("A" * 43 + "\n", id="newline"),
    pytest.param("A" * 42 + "\x00", id="nul"),
])
def test_verifier_validator_requires_exact_ascii_unreserved_bounds_without_disclosure(value):
    with pytest.raises(ValueError) as caught:
        OIDCLoginTransaction(code_verifier=value)
    assert str(caught.value) == "OIDC PKCE verifier must contain 43–128 ASCII unreserved characters."


@pytest.mark.parametrize("verifier", ["A" * 43, "z" * 128, VERIFIER])
def test_valid_login_values_preserve_exact_nonce_verifier_and_digest(verifier):
    nonce = "  Nonce-é-e\u0301-Case-sensitive  "
    digest = b"sensitive-digest-marker".ljust(32, b"x")
    record = OIDCLoginTransaction(state_hash=digest, nonce=nonce, code_verifier=verifier)
    assert record.state_hash is digest
    assert record.nonce is nonce
    assert record.code_verifier is verifier
    for rendered in (repr(record), str(record)):
        assert nonce not in rendered and verifier not in rendered
        assert repr(digest) not in rendered and digest.hex() not in rendered


def test_login_uuid_timestamp_defaults_and_exact_server_side_roundtrip(connection):
    digest = sha256(b"synthetic-browser-facing-state-not-persisted").digest()
    nonce = "  Nonce-é-e\u0301-Case-sensitive  "
    with Session(bind=connection, join_transaction_mode="create_savepoint") as database:
        record = OIDCLoginTransaction(
            state_hash=digest, nonce=nonce, code_verifier=VERIFIER,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        database.add(record)
        database.flush()
        assert isinstance(record.id, UUID) and record.id.version == 4
        assert record.created_at.utcoffset() is not None
        assert record.expires_at.utcoffset() is not None
        assert record.created_at < record.expires_at
        identifier = record.id
        database.expire_all()
        restored = database.get(OIDCLoginTransaction, identifier)
        assert (restored.state_hash, restored.nonce, restored.code_verifier) == (digest, nonce, VERIFIER)


def test_migrated_login_schema_matches_metadata_and_exact_constraints(connection):
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == LOGIN_REVISION
    assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    inspector = inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns(TABLE_NAME)}
    assert set(columns) == set(OIDCLoginTransaction.__table__.columns.keys())
    assert all(not column["nullable"] for column in columns.values())
    assert columns["id"]["default"] is None
    assert str(columns["state_hash"]["type"]) == "BYTEA"
    assert all(columns[field]["type"].timezone for field in ("created_at", "expires_at"))
    assert inspector.get_foreign_keys(TABLE_NAME) == []
    assert {constraint["name"] for constraint in inspector.get_check_constraints(TABLE_NAME)} == CHECK_NAMES
    assert {(constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(TABLE_NAME)} == {
        ("uq_oidc_login_transactions_state_hash", ("state_hash",)),
    }
    expiry = next(index for index in inspector.get_indexes(TABLE_NAME)
                  if index["name"] == "ix_oidc_login_transactions_expires_at")
    assert expiry["column_names"] == ["expires_at"] and not expiry["unique"]
    collations = dict(connection.execute(text(
        "SELECT attribute.attname, collation_row.collname FROM pg_attribute AS attribute "
        "JOIN pg_class AS relation ON relation.oid = attribute.attrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
        "JOIN pg_collation AS collation_row ON collation_row.oid = attribute.attcollation "
        "WHERE namespace.nspname = current_schema() AND relation.relname = 'oidc_login_transactions' "
        "AND attribute.attname IN ('nonce', 'code_verifier')"
    )).all())
    assert collations == {"nonce": "C", "code_verifier": "C"}


@pytest.mark.parametrize("field", ["id", "state_hash", "nonce", "code_verifier", "created_at", "expires_at"])
def test_every_login_column_is_required_by_postgresql(connection, field):
    rejected(connection, lambda: add_transaction(connection, **{field: None}), "23502")


@pytest.mark.parametrize("digest", [b"", b"x" * 31, b"x" * 33], ids=["empty", "short", "long"])
def test_postgresql_rejects_wrong_digest_lengths(connection, digest):
    rejected(connection, lambda: add_transaction(connection, state_hash=digest), "23514")


@pytest.mark.parametrize("nonce", ["", " \t\r\n "], ids=["empty", "whitespace"])
def test_postgresql_rejects_blank_nonces(connection, nonce):
    rejected(connection, lambda: add_transaction(connection, nonce=nonce), "23514")


@pytest.mark.parametrize("verifier", [
    "A" * 42, "A" * 129, "A" * 42 + "é", "A" * 42 + "/", "A" * 42 + "+",
    "A" * 42 + "=", "A" * 42 + " ", "A" * 43 + "\n",
], ids=["short", "long", "non-ascii", "slash", "plus", "padding", "space", "trailing-newline"])
def test_postgresql_enforces_verifier_ascii_bounds_and_characters(connection, verifier):
    rejected(connection, lambda: add_transaction(connection, code_verifier=verifier), "23514")


@pytest.mark.parametrize("verifier", ["A" * 43, "z" * 128, VERIFIER], ids=["minimum", "maximum", "alphabet"])
def test_postgresql_accepts_every_unreserved_verifier_character_and_boundaries(connection, verifier):
    identifier = add_transaction(connection, nonce="  Exact-Nonce-é  ", code_verifier=verifier)
    assert tuple(connection.execute(select(
        OIDCLoginTransaction.nonce, OIDCLoginTransaction.code_verifier,
    ).where(OIDCLoginTransaction.id == identifier)).one()) == ("  Exact-Nonce-é  ", verifier)


@pytest.mark.parametrize("expires_at", [NOW, NOW - timedelta(microseconds=1)], ids=["equal", "earlier"])
def test_postgresql_requires_expiration_after_creation(connection, expires_at):
    rejected(connection, lambda: add_transaction(connection, expires_at=expires_at), "23514")


def test_postgresql_unique_digest_is_independent_of_nonce_and_verifier(connection):
    digest = sha256(b"synthetic-shared-state").digest()
    first = add_transaction(connection, state_hash=digest)
    rejected(connection, lambda: add_transaction(
        connection, state_hash=digest, nonce="different-nonce", code_verifier="z" * 128,
    ), "23505")
    assert connection.scalar(select(OIDCLoginTransaction.id).where(OIDCLoginTransaction.state_hash == digest)) == first


def rows(connection, table):
    return {row["id"]: dict(row) for row in connection.execute(select(table)).mappings()}


def prior_schema_facts(connection):
    """Compare all pre-login columns, constraints, indexes and immutability DDL."""
    return {
        "columns": dict(connection.execute(text(
            "SELECT relation.relname || '.' || attribute.attname, "
            "format_type(attribute.atttypid, attribute.atttypmod) || ':' || attribute.attnotnull::text || ':' || "
            "COALESCE(pg_get_expr(default_row.adbin, default_row.adrelid), '') || ':' || COALESCE(collation_row.collname, '') "
            "FROM pg_attribute AS attribute JOIN pg_class AS relation ON relation.oid = attribute.attrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "LEFT JOIN pg_attrdef AS default_row ON default_row.adrelid = relation.oid AND default_row.adnum = attribute.attnum "
            "LEFT JOIN pg_collation AS collation_row ON collation_row.oid = attribute.attcollation "
            "WHERE attribute.attnum > 0 AND NOT attribute.attisdropped AND namespace.nspname = current_schema() "
            f"AND relation.relname IN {PRIOR_TABLE_SQL}"
        )).all()),
        "constraints": dict(connection.execute(text(
            "SELECT relation.relname || '.' || constraint_row.conname, pg_get_constraintdef(constraint_row.oid) "
            "FROM pg_constraint AS constraint_row JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = current_schema() "
            f"AND relation.relname IN {PRIOR_TABLE_SQL}"
        )).all()),
        "indexes": dict(connection.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            f"AND tablename IN {PRIOR_TABLE_SQL}"
        )).all()),
        "triggers": dict(connection.execute(text(
            "SELECT relation.relname || '.' || trigger_row.tgname, "
            "pg_get_triggerdef(trigger_row.oid) || ':' || CAST(trigger_row.tgenabled AS text) "
            "FROM pg_trigger AS trigger_row JOIN pg_class AS relation ON relation.oid = trigger_row.tgrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE NOT trigger_row.tgisinternal AND namespace.nspname = current_schema() "
            f"AND relation.relname IN {PRIOR_TABLE_SQL}"
        )).all()),
        "functions": dict(connection.execute(text(
            "SELECT function_row.proname, pg_get_functiondef(function_row.oid) FROM pg_proc AS function_row "
            "JOIN pg_namespace AS namespace ON namespace.oid = function_row.pronamespace "
            "WHERE namespace.nspname = current_schema() "
            "AND function_row.proname IN ('rehearse_preserve_measurement', 'rehearse_preserve_questions')"
        )).all()),
    }


def seed_prior_history(connection):
    owner, local_auth, completed, legacy = uuid4(), uuid4(), uuid4(), uuid4()
    connection.execute(insert(User).values(
        id=owner, auth_provider="https://schema.example.test", provider_subject=" Exact-SUBJECT-é ", created_at=NOW,
    ))
    connection.execute(insert(AuthSession).values(
        id=local_auth, user_id=owner, token_hash=sha256(b"preserved-local-auth-credential").digest(),
        request_context="preserved-request-context", created_at=NOW, expires_at=NOW + timedelta(hours=8),
    ))
    connection.execute(insert(StoredInterviewSession), [
        {"id": completed, "user_id": owner, "questions": list(QUESTIONS), "current_question_index": 5,
         "status": "completed", "created_at": NOW, "completed_at": NOW + timedelta(minutes=5)},
        {"id": legacy, "user_id": None, "questions": list(QUESTIONS), "current_question_index": 0,
         "status": "active", "created_at": NOW, "completed_at": None},
    ])
    measurement_ids = [uuid4(), uuid4(), uuid4()]
    base = {
        "session_id": completed, "question_index": 0, "created_at": NOW,
        "measurement_version": MEASUREMENT_VERSION, "measurement_source": "original_transcription",
        "recognized_word_count": 3, "um_count": 1, "uh_count": 1, "filler_unavailable_reason": None,
        "timed_utterance_span_seconds": 1.234567890123, "estimated_words_per_minute": 145.800001312,
        "timing_unavailable_reason": None,
    }
    connection.execute(insert(TranscriptionMeasurement).values(
        **base, id=measurement_ids[0], delivery_measurement_version="pause-metrics-v1", pause_count=1,
        total_pause_duration_seconds=0.987654321234, longest_pause_seconds=0.987654321234,
        pause_unavailable_reason=None,
    ))
    connection.execute(insert(TranscriptionMeasurement).values(
        **{**base, "recognized_word_count": 0, "um_count": None, "uh_count": None,
           "filler_unavailable_reason": "unsupported_language", "timed_utterance_span_seconds": None,
           "estimated_words_per_minute": None, "timing_unavailable_reason": "missing_timings"},
        id=measurement_ids[1], delivery_measurement_version="pause-metrics-v1", pause_count=None,
        total_pause_duration_seconds=None, longest_pause_seconds=None, pause_unavailable_reason="missing_timings",
    ))
    connection.execute(insert(TranscriptionMeasurement).values(**base, id=measurement_ids[2]))
    for number, measurement_id in enumerate(measurement_ids, start=1):
        connection.execute(insert(QuestionAttempt).values(
            id=uuid4(), session_id=completed, question_index=0, attempt_number=number,
            answer_text=f"Preserved retry {number}.", submitted_at=NOW + timedelta(seconds=number),
            measurement_id=measurement_id,
        ))
    for question in range(1, len(QUESTIONS)):
        connection.execute(insert(QuestionAttempt).values(
            id=uuid4(), session_id=completed, question_index=question, attempt_number=1,
            answer_text=f"Preserved answer {question}.", submitted_at=NOW + timedelta(minutes=question),
        ))
    return owner, local_auth, completed, legacy, measurement_ids


def test_populated_0003_roundtrip_preserves_identity_auth_owners_history_and_all_prior_ddl(connection):
    config = migration_config(connection)
    command.downgrade(config, PREVIOUS_REVISION)
    assert set(inspect(connection).get_table_names()) == PRIOR_TABLES | {"alembic_version"}
    owner, local_auth, completed, legacy, measurements = seed_prior_history(connection)
    prior_rows = {model.__tablename__: rows(connection, model.__table__) for model in PRIOR_MODELS}
    prior_facts = prior_schema_facts(connection)
    assert set(prior_facts["functions"]) == {"rehearse_preserve_measurement", "rehearse_preserve_questions"}
    assert len(prior_facts["triggers"]) == 2
    assert compare_metadata(MigrationContext.configure(connection), previous_metadata()) == []

    for revision in (LOGIN_REVISION, PREVIOUS_REVISION, LOGIN_REVISION):
        if revision == PREVIOUS_REVISION:
            command.downgrade(config, revision)
            assert set(inspect(connection).get_table_names()) == PRIOR_TABLES | {"alembic_version"}
            assert connection.scalar(text(
                "SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() "
                "AND tablename = 'oidc_login_transactions'"
            )) == 0
            assert compare_metadata(MigrationContext.configure(connection), previous_metadata()) == []
        else:
            command.upgrade(config, revision)
            assert set(inspect(connection).get_table_names()) == PRIOR_TABLES | {TABLE_NAME, "alembic_version"}
            assert connection.scalar(select(OIDCLoginTransaction.id).limit(1)) is None
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
            add_transaction(connection)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == revision
        assert prior_schema_facts(connection) == prior_facts
        assert {model.__tablename__: rows(connection, model.__table__) for model in PRIOR_MODELS} == prior_rows
        assert prior_rows["interview_sessions"][completed]["user_id"] == owner
        assert prior_rows["interview_sessions"][legacy]["user_id"] is None
        assert prior_rows["auth_sessions"][local_auth]["user_id"] == owner
        assert prior_rows["transcription_measurements"][measurements[0]]["total_pause_duration_seconds"] == 0.987654321234
        assert prior_rows["transcription_measurements"][measurements[1]]["pause_unavailable_reason"] == "missing_timings"
        assert prior_rows["transcription_measurements"][measurements[2]]["delivery_measurement_version"] is None
        rejected(connection, lambda: connection.execute(update(TranscriptionMeasurement).where(
            TranscriptionMeasurement.id == measurements[0]).values(recognized_word_count=4)), "23514")
        rejected(connection, lambda: connection.execute(update(StoredInterviewSession).where(
            StoredInterviewSession.id == completed).values(questions=["Changed", *QUESTIONS[1:]])), "23514")
