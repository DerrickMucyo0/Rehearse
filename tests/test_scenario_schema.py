"""Scenario migration tests use only the isolated PostgreSQL test schema."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from app.sessions import QUESTIONS

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("job_interview", "public_speaking", "thesis_defense", "salary_negotiation")


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


def rows(connection, table):
    return {row["id"]: dict(row) for row in connection.execute(select(table)).mappings()}


def test_populated_0004_upgrade_defaults_legacy_scenarios_and_preserves_all_prior_facts(connection):
    config = migration_config(connection)
    command.downgrade(config, "0004_oidc_login_transactions")
    metadata = MetaData()
    metadata.reflect(bind=connection)
    tables = {name: table for name, table in metadata.tables.items() if name != "alembic_version"}
    sessions = tables["interview_sessions"]
    assert "scenario_type" not in sessions.c
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    owner, owned, anonymous, measurement = (uuid4() for _ in range(4))
    connection.execute(insert(tables["users"]).values(
        id=owner, auth_provider="https://scenario.example.test", provider_subject="scenario-owner",
        created_at=now,
    ))
    connection.execute(insert(tables["auth_sessions"]).values(
        id=uuid4(), user_id=owner, token_hash=sha256(b"synthetic-scenario-auth").digest(),
        request_context="synthetic-scenario-context", created_at=now,
        expires_at=now + timedelta(hours=1),
    ))
    connection.execute(insert(sessions), [
        dict(id=owned, user_id=owner, questions=list(QUESTIONS), status="completed",
             current_question_index=5, created_at=now, completed_at=now + timedelta(minutes=5)),
        dict(id=anonymous, user_id=None, questions=list(QUESTIONS), status="active",
             current_question_index=0, created_at=now, completed_at=None),
    ])
    connection.execute(insert(tables["transcription_measurements"]).values(
        id=measurement, session_id=owned, question_index=0, created_at=now,
        measurement_version="speaking-metrics-v1", measurement_source="original_transcription",
        recognized_word_count=2, um_count=0, uh_count=0, filler_unavailable_reason=None,
        timed_utterance_span_seconds=1.234567890123,
        estimated_words_per_minute=120 / 1.234567890123, timing_unavailable_reason=None,
    ))
    connection.execute(insert(tables["question_attempts"]), [
        dict(id=uuid4(), session_id=owned, question_index=index, attempt_number=1,
             answer_text=f"Preserved answer {index}.", submitted_at=now,
             measurement_id=measurement if index == 0 else None)
        for index in range(5)
    ])
    prior = {name: rows(connection, table) for name, table in tables.items()}

    command.upgrade(config, "head")
    current = Table("interview_sessions", MetaData(), autoload_with=connection)
    migrated = rows(connection, current)
    assert set(migrated) == {owned, anonymous}
    assert all(row["scenario_type"] == "job_interview" for row in migrated.values())
    assert migrated[owned]["user_id"] == owner
    assert migrated[anonymous]["user_id"] is None
    assert {name: rows(connection, table) for name, table in tables.items()} == prior

    command.downgrade(config, "0004_oidc_login_transactions")
    assert "scenario_type" not in {column["name"] for column in inspect(connection).get_columns("interview_sessions")}
    assert {name: rows(connection, table) for name, table in tables.items()} == prior
    command.upgrade(config, "head")
    assert all(row["scenario_type"] == "job_interview" for row in rows(connection, current).values())


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario_database_constraint_accepts_each_canonical_value(connection, scenario):
    table = Table("interview_sessions", MetaData(), autoload_with=connection)
    identifier = uuid4()
    connection.execute(insert(table).values(id=identifier, questions=list(QUESTIONS), scenario_type=scenario))
    assert connection.scalar(select(table.c.scenario_type).where(table.c.id == identifier)) == scenario


@pytest.mark.parametrize("scenario", ["", "other", "Job Interview", "JOB_INTERVIEW", None])
def test_scenario_database_constraint_rejects_unknown_or_null_values(connection, scenario):
    table = Table("interview_sessions", MetaData(), autoload_with=connection)
    with pytest.raises(IntegrityError) as caught:
        with connection.begin_nested():
            connection.execute(insert(table).values(id=uuid4(), questions=list(QUESTIONS), scenario_type=scenario))
    assert caught.value.orig.sqlstate == ("23502" if scenario is None else "23514")


def test_legacy_database_inserts_keep_explicit_job_interview_server_default(connection):
    table = Table("interview_sessions", MetaData(), autoload_with=connection)
    identifier = uuid4()
    connection.execute(insert(table).values(id=identifier, questions=list(QUESTIONS)))
    assert connection.scalar(select(table.c.scenario_type).where(table.c.id == identifier)) == "job_interview"
