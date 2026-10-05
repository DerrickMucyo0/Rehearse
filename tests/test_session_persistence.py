"""Transactional session behavior against the isolated real PostgreSQL database."""
from datetime import datetime
from threading import Event, Thread, current_thread
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.database import DatabaseConfigurationError, create_database_engine, create_session_factory
from app.database_models import QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement
from app.main import app
from app.session_routes import get_session_service
from app.sessions import (
    QUESTIONS, AnswerRequest, InterviewSessionService, SessionConflict, SessionNotFound,
)


@pytest.fixture
def sessions(postgres_session_factory):
    return InterviewSessionService(postgres_session_factory)


def snapshot(factory, identifier):
    """Copy stored state while its operation-scoped session remains open."""
    with factory() as database:
        record = database.get(StoredInterviewSession, identifier)
        attempts = database.execute(select(QuestionAttempt).where(
            QuestionAttempt.session_id == identifier,
        ).order_by(QuestionAttempt.question_index, QuestionAttempt.attempt_number)).scalars().all()
        return {
            "questions": record.questions,
            "index": record.current_question_index,
            "status": record.status,
            "created_at": record.created_at,
            "completed_at": record.completed_at,
            "attempts": [
                (attempt.question_index, attempt.attempt_number, attempt.answer_text,
                 attempt.submitted_at, attempt.measurement_id)
                for attempt in attempts
            ],
        }


def reconstructed_service(engine):
    rebuilt_engine = create_database_engine(engine.url)
    return rebuilt_engine, InterviewSessionService(create_session_factory(rebuilt_engine))


def test_created_session_survives_service_and_engine_reconstruction(sessions, postgres_engine,
                                                                   postgres_session_factory):
    created = sessions.start()
    assert created.questions == list(QUESTIONS)
    assert created.current_question_index == 0
    assert created.answers == []
    assert created.status == "active"
    stored = snapshot(postgres_session_factory, created.id)
    assert isinstance(stored["created_at"], datetime)
    assert stored["created_at"].tzinfo is not None
    assert stored["completed_at"] is None
    postgres_engine.dispose()
    rebuilt_engine, rebuilt = reconstructed_service(postgres_engine)
    try:
        assert rebuilt.get(created.id) == created
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
    finally:
        rebuilt_engine.dispose()


def test_submitted_trimmed_answer_and_index_survive_reconstruction(sessions, postgres_engine,
                                                                 postgres_session_factory):
    created = sessions.start()
    updated = sessions.submit_answer(created.id, AnswerRequest(question_index=0, answer="  Final answer. \n"))
    assert updated.current_question_index == 1
    assert updated.answers == ["Final answer."]
    stored = snapshot(postgres_session_factory, created.id)
    question, number, answer, submitted_at, measurement_id = stored["attempts"][0]
    assert (question, number, answer, measurement_id) == (0, 1, "Final answer.", None)
    assert submitted_at.tzinfo is not None
    assert submitted_at >= stored["created_at"]
    postgres_engine.dispose()
    rebuilt_engine, rebuilt = reconstructed_service(postgres_engine)
    try:
        assert rebuilt.get(created.id) == updated
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
    finally:
        rebuilt_engine.dispose()


def test_fifth_answer_and_completion_timestamps_survive_reconstruction(sessions, postgres_engine,
                                                                    postgres_session_factory):
    created = sessions.start()
    for index in range(5):
        updated = sessions.submit_answer(created.id, AnswerRequest(question_index=index, answer=f"Answer {index}"))
    assert updated.status == "completed"
    assert updated.current_question_index == 5
    assert updated.current_question is None
    assert updated.answers == [f"Answer {index}" for index in range(5)]
    stored = snapshot(postgres_session_factory, created.id)
    assert stored["completed_at"].tzinfo is not None
    assert stored["completed_at"] >= stored["created_at"]
    assert [attempt[0] for attempt in stored["attempts"]] == list(range(5))
    assert all(attempt[1] == 1 and attempt[4] is None for attempt in stored["attempts"])
    postgres_engine.dispose()
    rebuilt_engine, rebuilt = reconstructed_service(postgres_engine)
    try:
        assert rebuilt.get(created.id) == updated
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
        with pytest.raises(SessionConflict, match="Session is already completed"):
            rebuilt.submit_answer(created.id, AnswerRequest(question_index=5, answer="Extra"))
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
    finally:
        rebuilt_engine.dispose()


def test_returned_question_snapshot_cannot_mutate_persisted_snapshot(sessions, monkeypatch):
    import app.sessions as session_module

    created = sessions.start()
    created.questions[0] = "Changed returned copy"
    monkeypatch.setattr(session_module, "QUESTIONS", tuple(f"New question {index}" for index in range(5)))
    assert sessions.get(created.id).questions == list(QUESTIONS)
    updated = sessions.submit_answer(created.id, AnswerRequest(question_index=0, answer="Answer"))
    assert updated.questions == list(QUESTIONS)
    assert updated.current_question == QUESTIONS[1]


@pytest.mark.parametrize("index", [0, 2])
def test_stale_or_future_submission_leaves_all_stored_state_unchanged(sessions, postgres_session_factory, index):
    created = sessions.start()
    sessions.submit_answer(created.id, AnswerRequest(question_index=0, answer="Accepted"))
    before = snapshot(postgres_session_factory, created.id)
    with pytest.raises(SessionConflict, match="Answer does not match the current question"):
        sessions.submit_answer(created.id, AnswerRequest(question_index=index, answer="Rejected"))
    assert snapshot(postgres_session_factory, created.id) == before


def test_unknown_session_cannot_insert_an_attempt(sessions, postgres_session_factory):
    identifier = uuid4()
    with pytest.raises(SessionNotFound, match="Session not found"):
        sessions.get(identifier)
    with pytest.raises(SessionNotFound, match="Session not found"):
        sessions.submit_answer(identifier, AnswerRequest(question_index=0, answer="Answer"))
    with postgres_session_factory() as database:
        assert database.scalar(select(func.count()).select_from(QuestionAttempt).where(
            QuestionAttempt.session_id == identifier,
        )) == 0


@pytest.mark.parametrize("failure_stage", ["after_flush_postexec", "before_commit"])
@pytest.mark.parametrize("initial_answers", [0, 4])
def test_flush_or_commit_failure_rolls_back_attempt_progress_and_completion(
        sessions, postgres_engine, postgres_session_factory, failure_stage, initial_answers):
    created = sessions.start()
    for index in range(initial_answers):
        sessions.submit_answer(created.id, AnswerRequest(question_index=index, answer="Accepted"))
    before = snapshot(postgres_session_factory, created.id)

    class FailingSession(Session):
        pass

    def fail_operation(*args):
        raise RuntimeError("Injected transactional failure")

    event.listen(FailingSession, failure_stage, fail_operation)
    failing = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=FailingSession))
    try:
        with pytest.raises(RuntimeError, match="Injected transactional failure"):
            failing.submit_answer(created.id, AnswerRequest(question_index=initial_answers, answer="Rejected"))
    finally:
        event.remove(FailingSession, failure_stage, fail_operation)
    assert snapshot(postgres_session_factory, created.id) == before
    assert sessions.get(created.id).current_question_index == initial_answers


def test_unique_attempt_conflict_cannot_advance_or_replace_an_answer(sessions, postgres_session_factory):
    created = sessions.start()
    with postgres_session_factory.begin() as database:
        database.execute(insert(QuestionAttempt).values(
            session_id=created.id, question_index=0, attempt_number=1, answer_text="Already stored",
        ))
    before = snapshot(postgres_session_factory, created.id)
    with pytest.raises((IntegrityError, SessionConflict)) as caught:
        sessions.submit_answer(created.id, AnswerRequest(question_index=0, answer="Replacement"))
    if isinstance(caught.value, IntegrityError):
        assert caught.value.orig.sqlstate == "23505"
    assert snapshot(postgres_session_factory, created.id) == before


@pytest.mark.parametrize("answer", ["before\x00after", "\x00", "  answer\x00  "])
def test_embedded_nul_is_http_422_without_any_database_mutation(sessions, postgres_session_factory, answer):
    created = sessions.start()
    before = snapshot(postgres_session_factory, created.id)
    app.dependency_overrides[get_session_service] = lambda: sessions
    try:
        with TestClient(app) as client:
            response = client.post(f"/api/sessions/{created.id}/answers", json={
                "question_index": 0, "answer": answer,
            })
        assert response.status_code == 422
    finally:
        app.dependency_overrides.pop(get_session_service)
    assert snapshot(postgres_session_factory, created.id) == before


def test_service_rejects_nul_even_if_request_validation_was_bypassed(sessions, postgres_session_factory):
    created = sessions.start()
    before = snapshot(postgres_session_factory, created.id)
    request = AnswerRequest.model_construct(question_index=0, answer="before\x00after")
    with pytest.raises(ValueError, match="must not contain U\\+0000"):
        sessions.submit_answer(created.id, request)
    assert snapshot(postgres_session_factory, created.id) == before


def test_session_submission_does_not_create_or_link_transcription_measurements(sessions, postgres_session_factory):
    created = sessions.start()
    sessions.submit_answer(created.id, AnswerRequest(question_index=0, answer="Typed answer"))
    with postgres_session_factory() as database:
        assert database.scalar(select(func.count()).select_from(TranscriptionMeasurement).where(
            TranscriptionMeasurement.session_id == created.id,
        )) == 0
        assert database.scalar(select(QuestionAttempt.measurement_id).where(
            QuestionAttempt.session_id == created.id,
        )) is None


def test_service_operations_return_connections_on_success_and_error(sessions, postgres_engine):
    checked_out = set()
    counts = {"checkout": 0, "checkin": 0}

    def checkout(connection, record, proxy):
        checked_out.add(id(connection))
        counts["checkout"] += 1

    def checkin(connection, record):
        checked_out.discard(id(connection))
        counts["checkin"] += 1

    event.listen(postgres_engine, "checkout", checkout)
    event.listen(postgres_engine, "checkin", checkin)
    try:
        created = sessions.start()
        assert checked_out == set()
        assert counts["checkout"] == counts["checkin"] == 1
        unknown = uuid4()
        operations = [
            (lambda: sessions.get(created.id), None),
            (lambda: sessions.submit_answer(created.id, AnswerRequest(question_index=0, answer="Accepted")), None),
            (lambda: sessions.validate_current_question(created.id, 1), None),
            (lambda: sessions.submit_answer(created.id, AnswerRequest(question_index=0, answer="Stale")), SessionConflict),
            (lambda: sessions.get(unknown), SessionNotFound),
            (lambda: sessions.submit_answer(unknown, AnswerRequest(question_index=0, answer="Missing")), SessionNotFound),
            (lambda: sessions.submit_answer(created.id, AnswerRequest.model_construct(
                question_index=1, answer="Invalid\x00answer",
            )), ValueError),
        ]
        for operation, expected_error in operations:
            if expected_error is None:
                operation()
            else:
                with pytest.raises(expected_error):
                    operation()
            assert checked_out == set()
            assert counts["checkout"] == counts["checkin"]
            assert postgres_engine.pool.checkedout() == 0
        assert counts["checkout"] == 8
    finally:
        event.remove(postgres_engine, "checkout", checkout)
        event.remove(postgres_engine, "checkin", checkin)


def test_default_dependency_requires_application_url_and_never_falls_back_to_test_url(
        postgres_engine, monkeypatch):
    get_session_service.cache_clear()
    configured_engine = None
    monkeypatch.setenv("TEST_DATABASE_URL", postgres_engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL must be explicitly configured"):
            get_session_service()
        assert get_session_service.cache_info().currsize == 0
        monkeypatch.setenv("DATABASE_URL", postgres_engine.url.render_as_string(hide_password=False))
        configured = get_session_service()
        configured_engine = configured._session_factory.kw["bind"]
        assert isinstance(configured, InterviewSessionService)
        assert configured_engine.url == postgres_engine.url
        assert configured_engine.echo is False
        assert configured_engine.hide_parameters is True
        assert configured_engine.pool.checkedout() == 0
        assert get_session_service() is configured
    finally:
        get_session_service.cache_clear()
        if configured_engine is not None:
            configured_engine.dispose()


def test_default_http_dependency_persists_without_service_override(
        postgres_session_factory, postgres_engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_engine.url.render_as_string(hide_password=False))
    assert get_session_service not in app.dependency_overrides
    get_session_service.cache_clear()
    engines = []
    try:
        with TestClient(app) as client:
            created = client.post("/api/sessions")
            assert created.status_code == 201
            location = created.headers["Location"]
            engines.append(get_session_service()._session_factory.kw["bind"])
            submitted = client.post(f"{location}/answers", json={
                "question_index": 0, "answer": "  Durable answer.  ",
            })
            assert submitted.status_code == 200
            assert submitted.json()["answers"] == ["Durable answer."]
            # Rebuild the actual route dependency, rather than injecting a fake.
            get_session_service.cache_clear()
            engines[0].dispose()
            reloaded = client.get(location)
            assert reloaded.status_code == 200
            assert reloaded.json() == submitted.json()
            engines.append(get_session_service()._session_factory.kw["bind"])
    finally:
        get_session_service.cache_clear()
        for engine in engines:
            engine.dispose()


def test_read_snapshot_remains_consistent_when_submission_commits_during_read(sessions, postgres_engine):
    created = sessions.start()
    reader_executed = Event()
    release_reader = Event()
    writer_finished = Event()
    results = {}
    errors = {}

    def pause_reader(connection, cursor, statement, parameters, context, executemany):
        if (current_thread().name == "persistence-snapshot-reader"
                and statement.lstrip().upper().startswith("SELECT")
                and "interview_sessions" in statement):
            reader_executed.set()
            if not release_reader.wait(10):
                raise AssertionError("Test did not release the completed session read")

    def read():
        try:
            results["read"] = sessions.get(created.id)
        except Exception as failure:
            errors["read"] = failure

    def write():
        try:
            results["write"] = sessions.submit_answer(created.id, AnswerRequest(question_index=0, answer="Committed"))
        except Exception as failure:
            errors["write"] = failure
        finally:
            writer_finished.set()

    reader = Thread(name="persistence-snapshot-reader", target=read, daemon=True)
    writer = Thread(name="persistence-snapshot-writer", target=write, daemon=True)
    started = []
    event.listen(postgres_engine, "after_cursor_execute", pause_reader)
    try:
        reader.start()
        started.append(reader)
        assert reader_executed.wait(5), "Session read did not execute its statement"
        writer.start()
        started.append(writer)
        assert writer_finished.wait(3), "Read unnecessarily blocked the answer transaction"
        assert "write" in results
        assert results["write"].answers == ["Committed"]
        assert results["write"].current_question_index == 1
    finally:
        release_reader.set()
        for worker in started:
            worker.join(10)
        event.remove(postgres_engine, "after_cursor_execute", pause_reader)
    assert all(not worker.is_alive() for worker in started)
    assert "read" in results
    assert results["read"] == created
    later = sessions.get(created.id)
    assert later.current_question_index == 1
    assert later.answers == ["Committed"]


def test_same_session_waits_for_row_lock_while_other_session_can_advance(
        sessions, postgres_engine, postgres_session_factory):
    """Observe the PostgreSQL blocker, rather than relying on thread start timing."""
    same_session = sessions.start()
    other_session = sessions.start()
    first_locked = Event()
    second_started = Event()
    release_first = Event()
    independent_done = Event()
    pids = {}
    results = {}
    errors = {}

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" not in statement.upper():
            return
        role = current_thread().name
        if role in {"persistence-lock-first", "persistence-lock-second"}:
            pids[role] = connection.connection.dbapi_connection.info.backend_pid
            if role == "persistence-lock-second":
                second_started.set()

    def after_execute(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name == "persistence-lock-first" and "FOR UPDATE" in statement.upper():
            first_locked.set()
            if not release_first.wait(15):
                raise AssertionError("Test did not release the held PostgreSQL row lock")

    def submit(role, identifier, text):
        try:
            results[role] = sessions.submit_answer(identifier, AnswerRequest(question_index=0, answer=text))
        except Exception as failure:
            errors[role] = failure
        finally:
            if role == "independent":
                independent_done.set()

    first = Thread(name="persistence-lock-first", target=submit,
                   args=("first", same_session.id, "Winning answer"), daemon=True)
    second = Thread(name="persistence-lock-second", target=submit,
                    args=("second", same_session.id, "Duplicate answer"), daemon=True)
    independent = Thread(name="persistence-lock-independent", target=submit,
                         args=("independent", other_session.id, "Independent answer"), daemon=True)
    started = []
    event.listen(postgres_engine, "before_cursor_execute", before_execute)
    event.listen(postgres_engine, "after_cursor_execute", after_execute)
    try:
        first.start()
        started.append(first)
        assert first_locked.wait(5), "First submission did not acquire its PostgreSQL row lock"
        second.start()
        started.append(second)
        assert second_started.wait(5), "Second submission did not attempt its PostgreSQL row lock"
        blocked = False
        deadline = monotonic() + 5
        with postgres_engine.connect() as observer:
            while monotonic() < deadline:
                blockers = observer.scalar(select(func.pg_blocking_pids(pids["persistence-lock-second"])))
                if pids["persistence-lock-first"] in blockers:
                    blocked = True
                    break
                sleep(0.01)
        assert blocked, "PostgreSQL did not report the first submission blocking the second"
        independent.start()
        started.append(independent)
        assert independent_done.wait(3), "A different session was unnecessarily blocked by the held row lock"
        assert "independent" in results
        assert results["independent"].current_question_index == 1
        assert not release_first.is_set()
    finally:
        release_first.set()
        for worker in started:
            worker.join(10)
        event.remove(postgres_engine, "before_cursor_execute", before_execute)
        event.remove(postgres_engine, "after_cursor_execute", after_execute)
    assert all(not worker.is_alive() for worker in started)
    assert "first" in results
    assert isinstance(errors.get("second"), SessionConflict)
    assert results["first"].current_question_index == 1
    assert sessions.get(same_session.id).answers == ["Winning answer"]
    assert snapshot(postgres_session_factory, same_session.id)["index"] == 1
    assert len(snapshot(postgres_session_factory, same_session.id)["attempts"]) == 1
    assert sessions.get(other_session.id).answers == ["Independent answer"]
