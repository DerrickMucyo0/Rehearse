"""Adaptive question shape and transitions in the isolated PostgreSQL schema."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.database_models import StoredInterviewSession
from app.scenarios import questions_for_scenario

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = questions_for_scenario("job_interview")


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


def rejected(connection, operation):
    with pytest.raises(IntegrityError) as caught:
        with connection.begin_nested():
            operation()
    assert caught.value.orig.sqlstate == "23514"


def add_adaptive(connection, **changes):
    identifier = uuid4()
    connection.execute(insert(StoredInterviewSession).values(
        id=identifier, question_engine="live-ai-roleplay-v1", questions=list(QUESTIONS[:1]), **changes,
    ))
    return identifier


def read(connection, identifier):
    return dict(connection.execute(select(StoredInterviewSession.__table__).where(
        StoredInterviewSession.id == identifier,
    )).mappings().one())


def advance(connection, identifier, index):
    connection.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == identifier).values(
        questions=list(QUESTIONS[:index + 2]), current_question_index=index + 1,
    ))


def test_populated_0005_upgrade_preserves_every_existing_fact(connection):
    config = migration_config(connection)
    command.downgrade(config, "0005_session_scenarios")
    metadata = MetaData()
    metadata.reflect(bind=connection)
    tables = {name: table for name, table in metadata.tables.items() if name != "alembic_version"}
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    owner, auth_id, session_id, measurement_id = (uuid4() for _ in range(4))
    connection.execute(insert(tables["users"]).values(
        id=owner, auth_provider="https://roleplay.example.test", provider_subject="preserved-owner", created_at=now,
    ))
    connection.execute(insert(tables["auth_sessions"]).values(
        id=auth_id, user_id=owner, token_hash=sha256(b"synthetic-roleplay-auth").digest(),
        request_context="synthetic-roleplay-context", created_at=now, expires_at=now + timedelta(hours=1),
    ))
    connection.execute(insert(tables["oidc_login_transactions"]).values(
        id=uuid4(), state_hash=sha256(b"synthetic-roleplay-state").digest(), nonce="preserved-nonce",
        code_verifier="v" * 43, created_at=now, expires_at=now + timedelta(minutes=5),
    ))
    connection.execute(insert(tables["interview_sessions"]), [
        dict(id=session_id, user_id=owner, scenario_type="salary_negotiation", questions=list(QUESTIONS),
             current_question_index=0, status="active", created_at=now, completed_at=None),
        dict(id=uuid4(), user_id=None, scenario_type="thesis_defense", questions=list(QUESTIONS),
             current_question_index=5, status="completed", created_at=now,
             completed_at=now + timedelta(minutes=5)),
    ])
    connection.execute(insert(tables["transcription_measurements"]).values(
        id=measurement_id, session_id=session_id, question_index=0, created_at=now,
        measurement_version="speaking-metrics-v1", measurement_source="original_transcription",
        recognized_word_count=2, um_count=0, uh_count=0, filler_unavailable_reason=None,
        timed_utterance_span_seconds=1.234567890123, estimated_words_per_minute=120 / 1.234567890123,
        timing_unavailable_reason=None,
    ))
    connection.execute(insert(tables["question_attempts"]).values(
        id=uuid4(), session_id=session_id, question_index=0, attempt_number=1,
        answer_text="Preserved submitted answer.", submitted_at=now, measurement_id=measurement_id,
    ))
    before = {name: [dict(row) for row in connection.execute(select(table).order_by(table.c.id)).mappings()]
              for name, table in tables.items()}
    command.upgrade(config, "head")
    assert {name: [dict(row) for row in connection.execute(select(table).order_by(table.c.id)).mappings()]
            for name, table in tables.items()} == before
    upgraded = Table("interview_sessions", MetaData(), autoload_with=connection)
    assert set(connection.scalars(select(upgraded.c.question_engine))) == {"deterministic-v1"}
    command.downgrade(config, "0005_session_scenarios")
    assert {name: [dict(row) for row in connection.execute(select(table).order_by(table.c.id)).mappings()]
            for name, table in tables.items()} == before
    command.upgrade(config, "head")


@pytest.mark.parametrize("changes", [
    {"question_engine": "unknown", "questions": list(QUESTIONS)},
    {"question_engine": "deterministic-v1", "questions": list(QUESTIONS[:1])},
    {"question_engine": "live-ai-roleplay-v1", "questions": []},
    {"question_engine": "live-ai-roleplay-v1", "questions": list(QUESTIONS) + ["Sixth?"]},
    {"question_engine": "live-ai-roleplay-v1", "questions": list(QUESTIONS[:2])},
    {"question_engine": "live-ai-roleplay-v1", "questions": list(QUESTIONS[:1]), "current_question_index": 1},
    {"question_engine": "live-ai-roleplay-v1", "questions": list(QUESTIONS[:1]), "status": "completed",
     "current_question_index": 1, "completed_at": datetime(2030, 1, 1, tzinfo=timezone.utc)},
])
def test_adaptive_and_legacy_shapes_are_database_enforced(connection, changes):
    rejected(connection, lambda: connection.execute(insert(StoredInterviewSession).values(id=uuid4(), **changes)))


def test_engine_and_deterministic_snapshots_remain_immutable(connection):
    identifier = uuid4()
    connection.execute(insert(StoredInterviewSession).values(id=identifier, questions=list(QUESTIONS)))
    before = read(connection, identifier)
    assert before["question_engine"] == "deterministic-v1"
    for changes in ({"question_engine": "live-ai-roleplay-v1"}, {"questions": ["Replacement?"] * 5}):
        rejected(connection, lambda: connection.execute(update(StoredInterviewSession).where(
            StoredInterviewSession.id == identifier,
        ).values(**changes)))
    assert read(connection, identifier) == before


@pytest.mark.parametrize("changes", [
    {"questions": list(QUESTIONS[:2])},
    {"current_question_index": 1},
    {"questions": list(QUESTIONS[:3]), "current_question_index": 2},
    {"questions": ["Replaced first?", QUESTIONS[1]], "current_question_index": 1},
    {"questions": ["Replaced first?"]},
    {"questions": []},
    {"question_engine": "deterministic-v1", "questions": list(QUESTIONS)},
    {"status": "completed", "current_question_index": 5, "questions": list(QUESTIONS),
     "completed_at": datetime(2030, 1, 1, tzinfo=timezone.utc)},
])
def test_partial_or_skipped_adaptive_transitions_are_rejected_without_change(connection, changes):
    identifier = add_adaptive(connection)
    before = read(connection, identifier)
    rejected(connection, lambda: connection.execute(update(StoredInterviewSession).where(
        StoredInterviewSession.id == identifier,
    ).values(**changes)))
    assert read(connection, identifier) == before


def test_append_prefix_progression_and_q5_completion_are_atomic_and_terminal(connection):
    identifier = add_adaptive(connection)
    for index in range(4):
        before = read(connection, identifier)
        advance(connection, identifier, index)
        after = read(connection, identifier)
        assert tuple(after["questions"][:-1]) == tuple(before["questions"])
        assert after["current_question_index"] == index + 1
        assert after["status"] == "active"
    completed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    connection.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == identifier).values(
        current_question_index=5, status="completed", completed_at=completed_at,
    ))
    completed = read(connection, identifier)
    assert tuple(completed["questions"]) == QUESTIONS
    for changes in (
        {"status": "active", "current_question_index": 4, "completed_at": None},
        {"questions": list(QUESTIONS) + ["Sixth?"], "current_question_index": 6},
        {"questions": list(QUESTIONS[:4]), "current_question_index": 3, "status": "active", "completed_at": None},
        {"completed_at": completed_at + timedelta(seconds=1)},
    ):
        rejected(connection, lambda: connection.execute(update(StoredInterviewSession).where(
            StoredInterviewSession.id == identifier,
        ).values(**changes)))
    assert read(connection, identifier) == completed


@pytest.mark.parametrize("completed", [False, True])
def test_downgrade_refuses_to_reclassify_any_adaptive_session(connection, completed):
    identifier = add_adaptive(connection)
    if completed:
        for index in range(4):
            advance(connection, identifier, index)
        connection.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == identifier).values(
            current_question_index=5, status="completed", completed_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        ))
    before = read(connection, identifier)
    rejected(connection, lambda: command.downgrade(migration_config(connection), "0005_session_scenarios"))
    assert read(connection, identifier) == before
