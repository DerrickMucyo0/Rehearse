"""Transactional retry/Continue behavior against isolated real PostgreSQL."""
from datetime import datetime
from threading import Event, Thread, current_thread
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app import database as database_configuration
from app.database import (
    DatabaseConfigurationError, create_database_engine, create_session_factory,
    get_database_session_factory,
)
from app.database_models import QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement
from app.main import app
from app.session_routes import get_session_service
from app.sessions import (
    QUESTIONS, AttemptRequest, ContinueRequest, InterviewSessionService, SessionConflict, SessionNotFound,
)


@pytest.fixture
def sessions(postgres_session_factory):
    return InterviewSessionService(postgres_session_factory)


@pytest.fixture
def default_dependency_engines(monkeypatch):
    get_session_service.cache_clear()
    get_database_session_factory.cache_clear()
    engines = []
    actual_create_engine = database_configuration.create_database_engine

    def captured_engine(*args, **kwargs):
        engine = actual_create_engine(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(database_configuration, "create_database_engine", captured_engine)
    try:
        yield engines
    finally:
        get_session_service.cache_clear()
        get_database_session_factory.cache_clear()
        for engine in engines:
            engine.dispose()


def submit(sessions, identifier, question=0, revision=0, answer="Answer"):
    return sessions.submit_attempt(identifier, question, AttemptRequest(
        expected_last_attempt_number=revision, answer=answer,
    ))


def advance(sessions, identifier, question=0, revision=1):
    return sessions.continue_question(identifier, question, ContinueRequest(
        expected_last_attempt_number=revision,
    ))


def reach_question(sessions, identifier, question):
    for index in range(question):
        submit(sessions, identifier, index, answer=f"Answer {index}")
        advance(sessions, identifier, index)


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
                (attempt.id, attempt.question_index, attempt.attempt_number, attempt.answer_text,
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
    assert created.current_question_latest_attempt_number == 0
    assert created.answers == []
    assert created.status == "active"
    assert sessions.get_attempts(created.id, 0) == []
    stored = snapshot(postgres_session_factory, created.id)
    assert isinstance(stored["created_at"], datetime)
    assert stored["created_at"].tzinfo is not None
    assert stored["completed_at"] is None
    postgres_engine.dispose()
    rebuilt_engine, rebuilt = reconstructed_service(postgres_engine)
    try:
        assert rebuilt.get(created.id) == created
        assert rebuilt.get_attempts(created.id, 0) == []
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
    finally:
        rebuilt_engine.dispose()


def test_retries_append_without_advancing_and_survive_reconstruction(
        sessions, postgres_engine, postgres_session_factory):
    created = sessions.start()
    first = submit(sessions, created.id, answer="  First answer. \n")
    first_stored = snapshot(postgres_session_factory, created.id)["attempts"][0]
    second = submit(sessions, created.id, revision=1, answer="Second answer.")
    third = submit(sessions, created.id, revision=2, answer="Third answer.")
    for number, result in enumerate((first, second, third), start=1):
        assert result.session.current_question_index == 0
        assert result.session.current_question == QUESTIONS[0]
        assert result.session.current_question_latest_attempt_number == number
        assert result.session.status == "active"
        assert result.session.answers == []
        assert result.attempt.attempt_number == number
        assert result.attempt.question_index == 0
        assert result.attempt.measurement_id is None
        assert result.attempt.submitted_at.tzinfo is not None
    assert first.attempt.answer == "First answer."
    attempts = sessions.get_attempts(created.id, 0)
    assert attempts == [first.attempt, second.attempt, third.attempt]
    assert len({attempt.id for attempt in attempts}) == 3
    stored = snapshot(postgres_session_factory, created.id)
    assert stored["index"] == 0
    assert stored["completed_at"] is None
    assert stored["attempts"][0] == first_stored
    assert stored["attempts"][0][4] >= stored["created_at"]
    postgres_engine.dispose()
    rebuilt_engine, rebuilt = reconstructed_service(postgres_engine)
    try:
        assert rebuilt.get(created.id) == third.session
        assert rebuilt.get_attempts(created.id, 0) == attempts
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
    finally:
        rebuilt_engine.dispose()


def test_continue_finalizes_latest_answer_and_reconstructs_all_attempts(
        sessions, postgres_engine, postgres_session_factory):
    created = sessions.start()
    submit(sessions, created.id, answer="First answer")
    submit(sessions, created.id, revision=1, answer="Latest answer")
    attempts = sessions.get_attempts(created.id, 0)
    before = snapshot(postgres_session_factory, created.id)
    updated = advance(sessions, created.id, revision=2)
    assert updated.current_question_index == 1
    assert updated.current_question == QUESTIONS[1]
    assert updated.current_question_latest_attempt_number == 0
    assert updated.answers == ["Latest answer"]
    assert updated.status == "active"
    stored = snapshot(postgres_session_factory, created.id)
    assert stored["attempts"] == before["attempts"]
    assert stored["completed_at"] is None
    postgres_engine.dispose()
    rebuilt_engine, rebuilt = reconstructed_service(postgres_engine)
    try:
        assert rebuilt.get(created.id) == updated
        assert rebuilt.get_attempts(created.id, 0) == attempts
        assert rebuilt.get_attempts(created.id, 1) == []
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
    finally:
        rebuilt_engine.dispose()


def test_fifth_submission_remains_active_until_continue_and_completion_survives_reconstruction(
        sessions, postgres_engine, postgres_session_factory):
    created = sessions.start()
    reach_question(sessions, created.id, 4)
    submitted = submit(sessions, created.id, 4, answer="Answer 4")
    assert submitted.session.status == "active"
    assert submitted.session.current_question_index == 4
    assert submitted.session.current_question == QUESTIONS[4]
    assert submitted.session.answers == [f"Answer {index}" for index in range(4)]
    assert snapshot(postgres_session_factory, created.id)["completed_at"] is None
    updated = advance(sessions, created.id, 4)
    assert updated.status == "completed"
    assert updated.current_question_index == 5
    assert updated.current_question is None
    assert updated.current_question_latest_attempt_number == 0
    assert updated.answers == [f"Answer {index}" for index in range(5)]
    stored = snapshot(postgres_session_factory, created.id)
    assert stored["completed_at"].tzinfo is not None
    assert stored["completed_at"] >= stored["created_at"]
    assert [attempt[1] for attempt in stored["attempts"]] == list(range(5))
    assert all(attempt[2] == 1 and attempt[5] is None for attempt in stored["attempts"])
    postgres_engine.dispose()
    rebuilt_engine, rebuilt = reconstructed_service(postgres_engine)
    try:
        assert rebuilt.get(created.id) == updated
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
        with pytest.raises(SessionConflict, match="Session is already completed"):
            submit(rebuilt, created.id, 5, answer="Extra")
        with pytest.raises(SessionConflict, match="Session is already completed"):
            advance(rebuilt, created.id, 5)
        assert snapshot(create_session_factory(rebuilt_engine), created.id) == stored
    finally:
        rebuilt_engine.dispose()


def test_returned_question_snapshot_cannot_mutate_persisted_snapshot(sessions, monkeypatch):
    import app.sessions as session_module

    created = sessions.start()
    created.questions[0] = "Changed returned copy"
    monkeypatch.setattr(session_module, "QUESTIONS", tuple(f"New question {index}" for index in range(5)))
    assert sessions.get(created.id).questions == list(QUESTIONS)
    submitted = submit(sessions, created.id)
    assert submitted.session.questions == list(QUESTIONS)
    assert submitted.session.current_question == QUESTIONS[0]
    updated = advance(sessions, created.id)
    assert updated.questions == list(QUESTIONS)
    assert updated.current_question == QUESTIONS[1]


@pytest.mark.parametrize("index", [0, 2])
@pytest.mark.parametrize("operation", ["submit", "continue"])
def test_stale_or_future_question_leaves_all_stored_state_unchanged(
        sessions, postgres_session_factory, index, operation):
    created = sessions.start()
    submit(sessions, created.id, answer="Accepted")
    advance(sessions, created.id)
    before = snapshot(postgres_session_factory, created.id)
    with pytest.raises(SessionConflict, match="Answer does not match the current question"):
        if operation == "submit":
            submit(sessions, created.id, index, answer="Rejected")
        else:
            advance(sessions, created.id, index)
    assert snapshot(postgres_session_factory, created.id) == before


@pytest.mark.parametrize("revision", [0, 2])
@pytest.mark.parametrize("operation", ["submit", "continue"])
def test_stale_or_future_attempt_revision_leaves_all_state_unchanged(
        sessions, postgres_session_factory, revision, operation):
    created = sessions.start()
    submit(sessions, created.id, answer="Accepted")
    before = snapshot(postgres_session_factory, created.id)
    with pytest.raises(SessionConflict, match="Attempt revision does not match the current question"):
        if operation == "submit":
            submit(sessions, created.id, revision=revision, answer="Rejected")
        else:
            advance(sessions, created.id, revision=revision)
    assert snapshot(postgres_session_factory, created.id) == before
    assert sessions.get(created.id).current_question_latest_attempt_number == 1


def test_continue_without_attempt_rejects_without_mutation(sessions, postgres_session_factory):
    created = sessions.start()
    before = snapshot(postgres_session_factory, created.id)
    with pytest.raises(SessionConflict):
        advance(sessions, created.id, revision=0)
    assert snapshot(postgres_session_factory, created.id) == before


def test_unknown_session_cannot_insert_an_attempt_or_continue(sessions, postgres_session_factory):
    identifier = uuid4()
    operations = (
        lambda: sessions.get(identifier),
        lambda: sessions.get_attempts(identifier, 0),
        lambda: submit(sessions, identifier),
        lambda: advance(sessions, identifier),
    )
    for operation in operations:
        with pytest.raises(SessionNotFound, match="Session not found"):
            operation()
    with postgres_session_factory() as database:
        assert database.scalar(select(func.count()).select_from(QuestionAttempt).where(
            QuestionAttempt.session_id == identifier,
        )) == 0


@pytest.mark.parametrize("failure_stage", ["after_flush_postexec", "before_commit"])
@pytest.mark.parametrize("question", [0, 4])
@pytest.mark.parametrize("initial_attempts", [0, 2])
def test_flush_or_commit_failure_rolls_back_append_and_preserves_progress(
        sessions, postgres_engine, postgres_session_factory, failure_stage, question, initial_attempts):
    created = sessions.start()
    reach_question(sessions, created.id, question)
    for revision in range(initial_attempts):
        submit(sessions, created.id, question, revision, f"Accepted {revision + 1}")
    before = snapshot(postgres_session_factory, created.id)

    class FailingSession(Session):
        pass

    def fail_operation(*args):
        raise RuntimeError("Injected transactional failure")

    event.listen(FailingSession, failure_stage, fail_operation)
    failing = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=FailingSession))
    try:
        with pytest.raises(RuntimeError, match="Injected transactional failure"):
            submit(failing, created.id, question, initial_attempts, "Rejected")
    finally:
        event.remove(FailingSession, failure_stage, fail_operation)
    assert snapshot(postgres_session_factory, created.id) == before
    restored = sessions.get(created.id)
    assert restored.current_question_index == question
    assert restored.current_question_latest_attempt_number == initial_attempts
    assert restored.status == "active"


@pytest.mark.parametrize("failure_stage", ["after_flush_postexec", "before_commit"])
@pytest.mark.parametrize("question", [0, 4])
def test_flush_or_commit_failure_rolls_back_continue_and_completion(
        sessions, postgres_engine, postgres_session_factory, failure_stage, question):
    created = sessions.start()
    reach_question(sessions, created.id, question)
    submit(sessions, created.id, question, answer="Persisted answer")
    before = snapshot(postgres_session_factory, created.id)

    class FailingSession(Session):
        pass

    def fail_operation(*args):
        raise RuntimeError("Injected transactional failure")

    event.listen(FailingSession, failure_stage, fail_operation)
    failing = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=FailingSession))
    try:
        with pytest.raises(RuntimeError, match="Injected transactional failure"):
            advance(failing, created.id, question)
    finally:
        event.remove(FailingSession, failure_stage, fail_operation)
    assert snapshot(postgres_session_factory, created.id) == before
    restored = sessions.get(created.id)
    assert restored.current_question_index == question
    assert restored.current_question_latest_attempt_number == 1
    assert restored.status == "active"


def test_number_allocation_uses_maximum_not_count_and_preserves_historical_attempts(
        sessions, postgres_session_factory):
    created = sessions.start()
    with postgres_session_factory.begin() as database:
        for number in (1, 3):
            database.execute(insert(QuestionAttempt).values(
                session_id=created.id, question_index=0, attempt_number=number,
                answer_text=f"Historical attempt {number}",
            ))
    before = snapshot(postgres_session_factory, created.id)
    assert sessions.get(created.id).current_question_latest_attempt_number == 3
    appended = submit(sessions, created.id, revision=3, answer="Fourth attempt")
    assert appended.attempt.attempt_number == 4
    assert appended.session.current_question_latest_attempt_number == 4
    assert appended.session.current_question_index == 0
    assert snapshot(postgres_session_factory, created.id)["attempts"][:2] == before["attempts"]
    assert [attempt.attempt_number for attempt in sessions.get_attempts(created.id, 0)] == [1, 3, 4]
    finalized = advance(sessions, created.id, revision=4)
    assert finalized.answers == ["Fourth attempt"]


def test_existing_attempt_cannot_be_overwritten_by_initial_revision(sessions, postgres_session_factory):
    created = sessions.start()
    with postgres_session_factory.begin() as database:
        database.execute(insert(QuestionAttempt).values(
            session_id=created.id, question_index=0, attempt_number=1, answer_text="Already stored",
        ))
    before = snapshot(postgres_session_factory, created.id)
    with pytest.raises(SessionConflict, match="Attempt revision does not match the current question"):
        submit(sessions, created.id, answer="Replacement")
    assert snapshot(postgres_session_factory, created.id) == before


@pytest.mark.parametrize("answer", ["before\x00after", "\x00", "  answer\x00  "])
def test_embedded_nul_is_http_422_without_any_database_mutation(sessions, postgres_session_factory, answer):
    created = sessions.start()
    before = snapshot(postgres_session_factory, created.id)
    app.dependency_overrides[get_session_service] = lambda: sessions
    try:
        with TestClient(app) as client:
            response = client.post(f"/api/sessions/{created.id}/questions/0/attempts", json={
                "expected_last_attempt_number": 0, "answer": answer,
            })
        assert response.status_code == 422
    finally:
        app.dependency_overrides.pop(get_session_service)
    assert snapshot(postgres_session_factory, created.id) == before


def test_service_rejects_nul_even_if_request_validation_was_bypassed(sessions, postgres_session_factory):
    created = sessions.start()
    before = snapshot(postgres_session_factory, created.id)
    request = AttemptRequest.model_construct(expected_last_attempt_number=0, answer="before\x00after")
    with pytest.raises(ValueError, match="must not contain U\\+0000"):
        sessions.submit_attempt(created.id, 0, request)
    assert snapshot(postgres_session_factory, created.id) == before


def test_typed_retries_do_not_create_or_link_transcription_measurements(sessions, postgres_session_factory):
    created = sessions.start()
    submit(sessions, created.id, answer="Typed answer")
    submit(sessions, created.id, revision=1, answer="Typed retry")
    with postgres_session_factory() as database:
        assert database.scalar(select(func.count()).select_from(TranscriptionMeasurement).where(
            TranscriptionMeasurement.session_id == created.id,
        )) == 0
        assert list(database.scalars(select(QuestionAttempt.measurement_id).where(
            QuestionAttempt.session_id == created.id,
        ))) == [None, None]


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
            (lambda: sessions.get_attempts(created.id, 0), None),
            (lambda: submit(sessions, created.id), None),
            (lambda: submit(sessions, created.id), SessionConflict),
            (lambda: advance(sessions, created.id), None),
            (lambda: advance(sessions, created.id), SessionConflict),
            (lambda: sessions.validate_current_question(created.id, 1, 0), None),
            (lambda: sessions.validate_current_question(created.id, 1, 1), SessionConflict),
            (lambda: sessions.get(unknown), SessionNotFound),
            (lambda: submit(sessions, unknown), SessionNotFound),
            (lambda: advance(sessions, unknown), SessionNotFound),
            (lambda: sessions.submit_attempt(created.id, 1, AttemptRequest.model_construct(
                expected_last_attempt_number=0, answer="Invalid\x00answer",
            )), ValueError),
        ]
        for operation, expected_error in operations:
            before_count = counts["checkout"]
            if expected_error is None:
                operation()
            else:
                with pytest.raises(expected_error):
                    operation()
            assert checked_out == set()
            assert counts["checkout"] == counts["checkin"] == before_count + 1
            assert postgres_engine.pool.checkedout() == 0
    finally:
        event.remove(postgres_engine, "checkout", checkout)
        event.remove(postgres_engine, "checkin", checkin)


def test_default_dependency_requires_application_url_and_never_falls_back_to_test_url(
        postgres_engine, monkeypatch, default_dependency_engines):
    monkeypatch.setenv("TEST_DATABASE_URL", postgres_engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL must be explicitly configured"):
        get_session_service()
    assert get_session_service.cache_info().currsize == get_database_session_factory.cache_info().currsize == 0
    assert default_dependency_engines == []
    monkeypatch.setenv("DATABASE_URL", postgres_engine.url.render_as_string(hide_password=False))
    configured = get_session_service()
    assert isinstance(configured, InterviewSessionService)
    assert len(default_dependency_engines) == 1
    configured_engine = default_dependency_engines[0]
    assert isinstance(get_database_session_factory(), sessionmaker)
    assert configured_engine.url == postgres_engine.url
    assert configured_engine.echo is False
    assert configured_engine.hide_parameters is True
    assert configured_engine.pool.checkedout() == 0
    assert get_session_service() is configured
    assert len(default_dependency_engines) == 1


def test_default_http_dependency_persists_without_service_override(
        postgres_session_factory, postgres_engine, monkeypatch, default_dependency_engines):
    monkeypatch.setenv("DATABASE_URL", postgres_engine.url.render_as_string(hide_password=False))
    assert get_session_service not in app.dependency_overrides
    with TestClient(app) as client:
        created = client.post("/api/sessions")
        assert created.status_code == 201
        location = created.headers["Location"]
        assert len(default_dependency_engines) == 1
        first_engine = default_dependency_engines[0]
        submitted = client.post(f"{location}/questions/0/attempts", json={
            "expected_last_attempt_number": 0, "answer": "  Durable answer.  ",
        })
        assert submitted.status_code == 201
        assert submitted.json()["attempt"]["answer"] == "Durable answer."
        assert submitted.json()["session"]["answers"] == []
        assert submitted.json()["session"]["current_question_latest_attempt_number"] == 1
        assert len(default_dependency_engines) == 1
        # Rebuild both production dependencies to exercise persistence across engines.
        get_session_service.cache_clear()
        get_database_session_factory.cache_clear()
        first_engine.dispose()
        reloaded = client.get(location)
        assert reloaded.status_code == 200
        assert reloaded.json() == submitted.json()["session"]
        assert len(default_dependency_engines) == 2
        assert default_dependency_engines[1] is not first_engine
        attempts = client.get(f"{location}/questions/0/attempts")
        assert attempts.status_code == 200
        assert attempts.json() == [submitted.json()["attempt"]]
        continued = client.post(f"{location}/questions/0/continue", json={
            "expected_last_attempt_number": 1,
        })
        assert continued.status_code == 200
        assert continued.json()["answers"] == ["Durable answer."]
        assert client.get(location).json() == continued.json()
        assert len(default_dependency_engines) == 2


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
            results["write"] = submit(sessions, created.id, answer="Committed")
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
        assert writer_finished.wait(3), "Read unnecessarily blocked the attempt transaction"
        assert "write" in results
        assert results["write"].session.answers == []
        assert results["write"].session.current_question_index == 0
        assert results["write"].session.current_question_latest_attempt_number == 1
    finally:
        release_reader.set()
        for worker in started:
            worker.join(10)
        event.remove(postgres_engine, "after_cursor_execute", pause_reader)
    assert all(not worker.is_alive() for worker in started)
    assert errors == {}
    assert results["read"] == created
    later = sessions.get(created.id)
    assert later.current_question_index == 0
    assert later.current_question_latest_attempt_number == 1
    assert later.answers == []
    assert [attempt.answer for attempt in sessions.get_attempts(created.id, 0)] == ["Committed"]


def run_locked_race(postgres_engine, first_operation, second_operation, independent_operation=None):
    """Observe the PostgreSQL blocker before releasing the first transaction."""
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

    def run(role, operation):
        try:
            results[role] = operation()
        except Exception as failure:
            errors[role] = failure
        finally:
            if role == "independent":
                independent_done.set()

    first = Thread(name="persistence-lock-first", target=run,
                   args=("first", first_operation), daemon=True)
    second = Thread(name="persistence-lock-second", target=run,
                    args=("second", second_operation), daemon=True)
    independent = Thread(name="persistence-lock-independent", target=run,
                         args=("independent", independent_operation), daemon=True)
    started = []
    event.listen(postgres_engine, "before_cursor_execute", before_execute)
    event.listen(postgres_engine, "after_cursor_execute", after_execute)
    try:
        first.start()
        started.append(first)
        assert first_locked.wait(5), "First operation did not acquire its PostgreSQL row lock"
        second.start()
        started.append(second)
        assert second_started.wait(5), "Second operation did not attempt its PostgreSQL row lock"
        blocked = False
        deadline = monotonic() + 5
        with postgres_engine.connect() as observer:
            while monotonic() < deadline:
                blockers = observer.scalar(select(func.pg_blocking_pids(pids["persistence-lock-second"])))
                if pids["persistence-lock-first"] in blockers:
                    blocked = True
                    break
                sleep(0.01)
        assert blocked, "PostgreSQL did not report the first transaction blocking the second"
        if independent_operation is not None:
            independent.start()
            started.append(independent)
            assert independent_done.wait(3), "A different session was blocked by the held session row lock"
            assert "independent" in results
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
    assert set(errors) == {"second"}
    return results, errors


def test_same_session_same_revision_accepts_one_attempt_while_different_session_progresses(
        sessions, postgres_engine, postgres_session_factory):
    same_session = sessions.start()
    other_session = sessions.start()
    results, errors = run_locked_race(
        postgres_engine,
        lambda: submit(sessions, same_session.id, answer="Winning answer"),
        lambda: submit(sessions, same_session.id, answer="Duplicate answer"),
        lambda: submit(sessions, other_session.id, answer="Independent answer"),
    )
    assert str(errors["second"]) == "Attempt revision does not match the current question."
    assert results["first"].attempt.attempt_number == 1
    assert results["independent"].attempt.attempt_number == 1
    assert results["independent"].session.current_question_index == 0
    stored = snapshot(postgres_session_factory, same_session.id)
    assert stored["index"] == 0
    assert stored["status"] == "active"
    assert len(stored["attempts"]) == 1
    assert sessions.get(same_session.id).current_question_latest_attempt_number == 1
    assert [attempt.answer for attempt in sessions.get_attempts(same_session.id, 0)] == ["Winning answer"]
    assert [attempt.answer for attempt in sessions.get_attempts(other_session.id, 0)] == ["Independent answer"]


def test_concurrent_continue_advances_exactly_once(sessions, postgres_engine, postgres_session_factory):
    created = sessions.start()
    submit(sessions, created.id, answer="Persisted answer")
    attempts = snapshot(postgres_session_factory, created.id)["attempts"]
    results, errors = run_locked_race(
        postgres_engine,
        lambda: advance(sessions, created.id),
        lambda: advance(sessions, created.id),
    )
    assert str(errors["second"]) == "Answer does not match the current question."
    assert results["first"].current_question_index == 1
    assert sessions.get(created.id).answers == ["Persisted answer"]
    stored = snapshot(postgres_session_factory, created.id)
    assert stored["index"] == 1
    assert stored["attempts"] == attempts


@pytest.mark.parametrize("first_operation", ["retry", "continue"])
def test_retry_and_continue_race_serializes_without_silent_advancement_or_append(
        sessions, postgres_engine, postgres_session_factory, first_operation):
    created = sessions.start()
    submit(sessions, created.id, answer="Initial answer")
    first_stored = snapshot(postgres_session_factory, created.id)["attempts"][0]
    retry = lambda: submit(sessions, created.id, revision=1, answer="Retry answer")
    continuation = lambda: advance(sessions, created.id)
    first, second = (retry, continuation) if first_operation == "retry" else (continuation, retry)
    results, errors = run_locked_race(postgres_engine, first, second)
    stored = snapshot(postgres_session_factory, created.id)
    assert stored["attempts"][0] == first_stored
    assert stored["status"] == "active"
    assert stored["completed_at"] is None
    if first_operation == "retry":
        assert str(errors["second"]) == "Attempt revision does not match the current question."
        assert stored["index"] == 0
        assert len(stored["attempts"]) == 2
        assert results["first"].session.current_question_latest_attempt_number == 2
        assert sessions.get(created.id).answers == []
    else:
        assert str(errors["second"]) == "Answer does not match the current question."
        assert stored["index"] == 1
        assert len(stored["attempts"]) == 1
        assert sessions.get(created.id).answers == ["Initial answer"]
