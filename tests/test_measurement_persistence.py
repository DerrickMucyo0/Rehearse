"""Real PostgreSQL measurement provenance and atomic answer-linking tests.

All transcription results are synthetic; the transport guard forbids provider
requests. Measurements refer to original transcription metrics, never edits.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Thread, current_thread
from time import monotonic, sleep
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import create_database_engine, create_session_factory
from app.database_models import (
    MEASUREMENT_VERSION, QuestionAttempt, StoredInterviewSession,
    TranscriptionMeasurement,
)
from app.delivery_metrics import measure_delivery
from app.main import app
from app.session_routes import get_session_service
from app.sessions import (
    AttemptRequest, ContinueRequest, InterviewSessionService, SessionConflict, SessionNotFound,
)
from app.speaking_metrics import measure_transcription
from app.transcription import (
    TranscriptionFailed, TranscriptionResult, TranscriptionTimeout,
    TranscriptionUnavailable, get_transcription_service,
)


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    async def blocked(*args, **kwargs):
        raise AssertionError("Real provider requests are forbidden in measurement tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)


def original_result():
    return TranscriptionResult(text="Um, uh hello", language="eng", words=[
        {"text": "Um,", "start": 2.0, "end": 2.25},
        {"text": "uh", "start": 2.25, "end": 2.75},
        {"text": "hello", "start": 2.75, "end": 3.234567890123},
    ])


class FakeTranscriber:
    def __init__(self):
        self.result = original_result()
        self.error = None
        self.during_provider = None
        self.calls = 0

    async def transcribe(self, audio, filename):
        self.calls += 1
        if self.during_provider:
            self.during_provider()
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def setup(postgres_session_factory, authenticated_principal, authenticated_session_override, authenticated_http_headers):
    service = InterviewSessionService(postgres_session_factory, authenticated_principal)
    fake = FakeTranscriber()
    app.dependency_overrides[get_session_service] = authenticated_session_override(service)
    app.dependency_overrides[get_transcription_service] = lambda: fake
    try:
        with TestClient(app, raise_server_exceptions=False, headers=authenticated_http_headers) as client:
            yield client, service, fake
    finally:
        app.dependency_overrides.pop(get_session_service, None)
        app.dependency_overrides.pop(get_transcription_service, None)


def transcribe(client, session_id, question_index=0, revision=0):
    return client.post(
        f"/api/sessions/{session_id}/transcriptions",
        data={"question_index": str(question_index), "expected_last_attempt_number": str(revision)},
        files={"audio": ("private-recording.webm", b"PRIVATE-SYNTHETIC-AUDIO", "audio/webm")},
    )


def metrics_for(result):
    return measure_transcription(result.text, result.language, result.words)


def delivery_for(result):
    return measure_delivery(result.text, result.words)


def delivery_projection(row):
    if row["delivery_measurement_version"] is None:
        return None
    return {
        "version": row["delivery_measurement_version"],
        "source": row["measurement_source"],
        "pause_count": row["pause_count"],
        "total_pause_duration_seconds": row["total_pause_duration_seconds"],
        "longest_pause_seconds": row["longest_pause_seconds"],
        "unavailable_reason": row["pause_unavailable_reason"],
    }


def metrics_projection(row):
    return {
        "source": row["measurement_source"],
        "recognized_word_count": row["recognized_word_count"],
        "um_count": row["um_count"],
        "uh_count": row["uh_count"],
        "filler_unavailable_reason": row["filler_unavailable_reason"],
        "timed_utterance_span_seconds": row["timed_utterance_span_seconds"],
        "estimated_words_per_minute": row["estimated_words_per_minute"],
        "timing_unavailable_reason": row["timing_unavailable_reason"],
    }


def measurement_rows(factory):
    with factory() as database:
        return [dict(row) for row in database.execute(
            select(TranscriptionMeasurement.__table__).order_by(TranscriptionMeasurement.created_at),
        ).mappings()]


def stored_state(factory, session_id):
    with factory() as database:
        session = dict(database.execute(select(StoredInterviewSession.__table__).where(
            StoredInterviewSession.id == session_id,
        )).mappings().one())
        attempts = [dict(row) for row in database.execute(select(QuestionAttempt.__table__).where(
            QuestionAttempt.session_id == session_id,
        ).order_by(QuestionAttempt.question_index, QuestionAttempt.attempt_number)).mappings()]
        return session, attempts


def advance(service, session_id, count):
    for index in range(count):
        service.submit_attempt(session_id, index, AttemptRequest(
            expected_last_attempt_number=0, answer=f"Answer {index}",
        ))
        service.continue_question(session_id, index, ContinueRequest(expected_last_attempt_number=1))


def test_success_persists_one_exact_original_measurement_with_opaque_uuid(setup, postgres_session_factory):
    client, service, fake = setup
    created = service.start()
    expected = metrics_for(fake.result).model_dump()
    assert expected["timed_utterance_span_seconds"] != round(expected["timed_utterance_span_seconds"], 2)
    before = datetime.now(timezone.utc)
    response = transcribe(client, created.id)
    after = datetime.now(timezone.utc)
    assert response.status_code == 200
    body = response.json()
    identifier = UUID(body["measurement_id"])
    assert identifier.version == 4
    assert set(body) == {"session_id", "question_index", "text", "language", "words", "metrics", "delivery_metrics", "measurement_id"}
    assert body["metrics"] == expected
    assert {key: body[key] for key in fake.result.model_dump()} == fake.result.model_dump()
    assert body["delivery_metrics"] == {**delivery_for(fake.result).model_dump(), "source": "original_transcription"}
    rows = measurement_rows(postgres_session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert (row["id"], row["session_id"], row["question_index"]) == (identifier, created.id, 0)
    assert row["measurement_version"] == MEASUREMENT_VERSION == "speaking-metrics-v1"
    assert metrics_projection(row) == expected
    assert delivery_projection(row) == body["delivery_metrics"]
    assert row["created_at"].tzinfo is not None
    assert before <= row["created_at"] <= after
    assert service.get(created.id) == created
    assert stored_state(postgres_session_factory, created.id)[1] == []
    assert fake.calls == 1


@pytest.mark.parametrize("language", [None, "spa"])
def test_unavailable_values_remain_null_in_persisted_measurement(setup, postgres_session_factory, language):
    client, service, fake = setup
    fake.result = TranscriptionResult(text="um uh", language=language)
    created = service.start()
    response = transcribe(client, created.id)
    assert response.status_code == 200
    expected = metrics_for(fake.result).model_dump()
    assert expected["um_count"] is expected["uh_count"] is None
    assert expected["timed_utterance_span_seconds"] is expected["estimated_words_per_minute"] is None
    assert expected["filler_unavailable_reason"] == "unsupported_language"
    assert expected["timing_unavailable_reason"] == "missing_timings"
    assert response.json()["metrics"] == expected
    row = measurement_rows(postgres_session_factory)[0]
    assert metrics_projection(row) == expected
    assert delivery_projection(row) == response.json()["delivery_metrics"] == {
        "version": "pause-metrics-v1", "source": "original_transcription", "pause_count": None,
        "total_pause_duration_seconds": None, "longest_pause_seconds": None, "unavailable_reason": "missing_timings",
    }


def test_measurement_storage_contains_no_audio_transcript_or_word_timing_columns(setup, postgres_session_factory):
    client, service, fake = setup
    created = service.start()
    assert transcribe(client, created.id).status_code == 200
    rows = measurement_rows(postgres_session_factory)
    assert len(rows) == 1
    assert set(rows[0]) == {
        "id", "session_id", "question_index", "created_at", "measurement_version",
        "measurement_source", "recognized_word_count", "um_count", "uh_count",
        "filler_unavailable_reason", "timed_utterance_span_seconds",
        "estimated_words_per_minute", "timing_unavailable_reason",
        "delivery_measurement_version", "pause_count", "total_pause_duration_seconds",
        "longest_pause_seconds", "pause_unavailable_reason",
    }
    assert b"PRIVATE-SYNTHETIC-AUDIO" not in [value for value in rows[0].values()]
    assert fake.result.text not in [value for value in rows[0].values()]
    assert stored_state(postgres_session_factory, created.id)[1] == []


def test_measurement_survives_engine_and_service_reconstruction(
        setup, postgres_engine, postgres_session_factory, authenticated_principal):
    client, service, _ = setup
    created = service.start()
    response = transcribe(client, created.id)
    assert response.status_code == 200
    identifier = UUID(response.json()["measurement_id"])
    expected_rows = measurement_rows(postgres_session_factory)
    postgres_engine.dispose()
    rebuilt_engine = create_database_engine(postgres_engine.url)
    rebuilt_factory = create_session_factory(rebuilt_engine)
    rebuilt_service = InterviewSessionService(rebuilt_factory, authenticated_principal)
    try:
        assert measurement_rows(rebuilt_factory) == expected_rows
        updated = rebuilt_service.submit_attempt(created.id, 0, AttemptRequest(
            expected_last_attempt_number=0, answer="Durable answer", measurement_id=identifier,
        ))
        assert updated.session.current_question_index == 0
        assert updated.attempt.attempt_number == 1
        assert stored_state(rebuilt_factory, created.id)[1][0]["measurement_id"] == identifier
        assert measurement_rows(rebuilt_factory) == expected_rows
    finally:
        rebuilt_engine.dispose()


def test_replacement_transcription_gets_new_id_and_retains_old_unlinked_measurement(
        setup, postgres_session_factory):
    client, service, fake = setup
    created = service.start()
    first = transcribe(client, created.id)
    assert first.status_code == 200
    first_id = UUID(first.json()["measurement_id"])
    first_row = measurement_rows(postgres_session_factory)[0]
    fake.result = TranscriptionResult(text="A replacement original transcript", language="eng")
    second = transcribe(client, created.id)
    assert second.status_code == 200
    second_id = UUID(second.json()["measurement_id"])
    assert second_id != first_id
    rows = measurement_rows(postgres_session_factory)
    assert len(rows) == 2
    assert next(row for row in rows if row["id"] == first_id) == first_row
    assert metrics_projection(next(row for row in rows if row["id"] == second_id)) == second.json()["metrics"]
    assert delivery_projection(next(row for row in rows if row["id"] == second_id)) == second.json()["delivery_metrics"]
    assert stored_state(postgres_session_factory, created.id)[1] == []
    now = datetime.now(timezone.utc)
    with postgres_session_factory() as database:
        for measurement in database.scalars(select(TranscriptionMeasurement)):
            assert not measurement.unlinked_deletion_eligible(linked=False, now=now)
    assert fake.calls == 2


def test_typed_answer_never_implicitly_attaches_an_existing_measurement(setup, postgres_session_factory):
    client, service, _ = setup
    created = service.start()
    assert transcribe(client, created.id).status_code == 200
    rows_before = measurement_rows(postgres_session_factory)
    response = client.post(f"/api/sessions/{created.id}/questions/0/attempts", json={
        "expected_last_attempt_number": 0, "answer": "  Typed answer  ",
    })
    assert response.status_code == 201
    assert response.json()["attempt"]["answer"] == "Typed answer"
    assert response.json()["session"]["answers"] == []
    assert stored_state(postgres_session_factory, created.id)[1][0]["measurement_id"] is None
    assert measurement_rows(postgres_session_factory) == rows_before


def test_edited_answer_links_exact_original_id_without_recalculating_metrics(setup, postgres_session_factory):
    client, service, fake = setup
    created = service.start()
    first = transcribe(client, created.id)
    assert first.status_code == 200
    first_id = UUID(first.json()["measurement_id"])
    fake.result = TranscriptionResult(text="Later original transcript", language="eng")
    second = transcribe(client, created.id)
    assert second.status_code == 200
    assert UUID(second.json()["measurement_id"]) != first_id
    before = measurement_rows(postgres_session_factory)
    edited = "A substantially edited answer without the original filler words"
    response = client.post(f"/api/sessions/{created.id}/questions/0/attempts", json={
        "expected_last_attempt_number": 0, "answer": f"  {edited}  ", "measurement_id": str(first_id),
    })
    assert response.status_code == 201
    assert response.json()["attempt"]["answer"] == edited
    assert response.json()["session"]["answers"] == []
    attempt = stored_state(postgres_session_factory, created.id)[1][0]
    assert (attempt["session_id"], attempt["question_index"], attempt["attempt_number"],
            attempt["answer_text"], attempt["measurement_id"]) == (created.id, 0, 1, edited, first_id)
    assert attempt["submitted_at"].tzinfo is not None
    assert measurement_rows(postgres_session_factory) == before
    linked_metrics = metrics_projection(next(row for row in before if row["id"] == first_id))
    assert linked_metrics["um_count"] == linked_metrics["uh_count"] == 1
    assert linked_metrics == first.json()["metrics"]
    assert fake.calls == 2


@pytest.mark.parametrize("initial_answers", [0, 4])
@pytest.mark.parametrize("invalid_reference", ["unknown", "different_session", "different_question", "already_linked"])
def test_invalid_measurement_reference_has_uniform_409_and_rolls_back_all_answer_state(
        setup, postgres_session_factory, initial_answers, invalid_reference):
    client, service, fake = setup
    created = service.start()
    advance(service, created.id, initial_answers)
    if invalid_reference == "unknown":
        identifier = uuid4()
    elif invalid_reference == "different_session":
        other = service.start()
        identifier = service.create_measurement(other.id, 0, metrics_for(fake.result), expected_last_attempt_number=0)
    elif invalid_reference == "different_question":
        # A controlled row provides the wrong context without changing the
        # immutable measurement after insertion or advancing this session.
        with postgres_session_factory.begin() as database:
            measurement = TranscriptionMeasurement(
                session_id=created.id, question_index=(initial_answers + 1) % 5,
                measurement_version=MEASUREMENT_VERSION,
                measurement_source="original_transcription",
                **{key: value for key, value in metrics_for(fake.result).model_dump().items() if key != "source"},
            )
            database.add(measurement)
            database.flush()
            identifier = measurement.id
    else:
        identifier = service.create_measurement(created.id, initial_answers, metrics_for(fake.result), expected_last_attempt_number=0)
        # Seed a linked row while the current index is unchanged to exercise
        # the explicit already-linked guard before any attempt uniqueness error.
        with postgres_session_factory.begin() as database:
            database.add(QuestionAttempt(
                session_id=created.id, question_index=initial_answers,
                attempt_number=1, answer_text="Already linked", measurement_id=identifier,
            ))
    before = stored_state(postgres_session_factory, created.id)
    measurements_before = measurement_rows(postgres_session_factory)
    response = client.post(f"/api/sessions/{created.id}/questions/{initial_answers}/attempts", json={
        "expected_last_attempt_number": (1 if invalid_reference == "already_linked" else 0), "answer": "Rejected", "measurement_id": str(identifier),
    })
    assert response.status_code == 409
    assert response.json() == {"detail": "Measurement cannot be attached to this answer."}
    assert stored_state(postgres_session_factory, created.id) == before
    assert measurement_rows(postgres_session_factory) == measurements_before
    assert before[0]["completed_at"] is None
    assert fake.calls == 0


@pytest.mark.parametrize("identifier", ["not-a-uuid", "", 23, {}, []])
def test_malformed_measurement_uuid_is_422_with_no_database_mutation(setup, postgres_session_factory, identifier):
    client, service, _ = setup
    created = service.start()
    before = stored_state(postgres_session_factory, created.id)
    response = client.post(f"/api/sessions/{created.id}/questions/0/attempts", json={
        "expected_last_attempt_number": 0, "answer": "Rejected", "measurement_id": identifier,
    })
    assert response.status_code == 422
    assert stored_state(postgres_session_factory, created.id) == before
    assert measurement_rows(postgres_session_factory) == []


def test_real_link_cannot_be_reused_on_later_question(setup, postgres_session_factory):
    client, service, _ = setup
    created = service.start()
    transcription = transcribe(client, created.id)
    assert transcription.status_code == 200
    identifier = transcription.json()["measurement_id"]
    first = client.post(f"/api/sessions/{created.id}/questions/0/attempts", json={
        "expected_last_attempt_number": 0, "answer": "First", "measurement_id": identifier,
    })
    assert first.status_code == 201
    assert service.continue_question(created.id, 0, ContinueRequest(expected_last_attempt_number=1)).current_question_index == 1
    before = stored_state(postgres_session_factory, created.id)
    second = client.post(f"/api/sessions/{created.id}/questions/1/attempts", json={
        "expected_last_attempt_number": 0, "answer": "Reuse", "measurement_id": identifier,
    })
    assert second.status_code == 409
    assert second.json() == {"detail": "Measurement cannot be attached to this answer."}
    assert stored_state(postgres_session_factory, created.id) == before
    assert len(before[1]) == 1
    assert before[1][0]["measurement_id"] == UUID(identifier)


def test_fifth_attempt_links_then_explicit_continue_completes(setup, postgres_session_factory):
    client, service, _ = setup
    created = service.start()
    advance(service, created.id, 4)
    transcription = transcribe(client, created.id, 4)
    assert transcription.status_code == 200
    identifier = UUID(transcription.json()["measurement_id"])
    measurements_before = measurement_rows(postgres_session_factory)
    response = client.post(f"/api/sessions/{created.id}/questions/4/attempts", json={
        "expected_last_attempt_number": 0, "answer": "Final", "measurement_id": str(identifier),
    })
    assert response.status_code == 201
    assert response.json()["session"]["status"] == "active"
    assert response.json()["session"]["current_question_index"] == 4
    assert stored_state(postgres_session_factory, created.id)[0]["completed_at"] is None
    completed = service.continue_question(created.id, 4, ContinueRequest(expected_last_attempt_number=1))
    assert completed.status == "completed"
    assert completed.current_question_index == 5
    assert completed.current_question is None
    session, attempts = stored_state(postgres_session_factory, created.id)
    assert (session["status"], session["current_question_index"]) == ("completed", 5)
    assert session["completed_at"].tzinfo is not None
    assert len(attempts) == 5
    assert attempts[-1]["measurement_id"] == identifier
    assert all(attempt["measurement_id"] is None for attempt in attempts[:-1])
    assert measurement_rows(postgres_session_factory) == measurements_before


@pytest.mark.parametrize("failure_stage", ["after_flush_postexec", "before_commit"])
@pytest.mark.parametrize("initial_answers", [0, 4])
def test_link_flush_or_commit_failure_rolls_back_attempt_progress_and_completion(
        setup, postgres_engine, postgres_session_factory, failure_stage, initial_answers, authenticated_principal):
    _, service, fake = setup
    created = service.start()
    advance(service, created.id, initial_answers)
    identifier = service.create_measurement(created.id, initial_answers, metrics_for(fake.result), expected_last_attempt_number=0)
    before = stored_state(postgres_session_factory, created.id)
    measurements_before = measurement_rows(postgres_session_factory)

    class FailingSession(Session):
        pass

    def fail(*args):
        raise RuntimeError("Injected transactional failure")

    event.listen(FailingSession, failure_stage, fail)
    failing = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=FailingSession), authenticated_principal)
    try:
        with pytest.raises(RuntimeError, match="Injected transactional failure"):
            failing.submit_attempt(created.id, initial_answers, AttemptRequest(
                expected_last_attempt_number=0, answer="Rejected", measurement_id=identifier,
            ))
    finally:
        event.remove(FailingSession, failure_stage, fail)
    assert stored_state(postgres_session_factory, created.id) == before
    assert measurement_rows(postgres_session_factory) == measurements_before


@pytest.mark.parametrize(("failure", "status"), [
    (TranscriptionFailed(), 502), (TranscriptionTimeout(), 504), (TranscriptionUnavailable(), 503),
])
def test_provider_failure_creates_no_measurement_or_attempt(setup, postgres_session_factory, failure, status):
    client, service, fake = setup
    created = service.start()
    before = stored_state(postgres_session_factory, created.id)
    fake.error = failure
    response = transcribe(client, created.id)
    assert response.status_code == status
    assert measurement_rows(postgres_session_factory) == []
    assert stored_state(postgres_session_factory, created.id) == before
    assert fake.calls == 1


@pytest.mark.parametrize("engine", ["measure_transcription", "measure_delivery"])
def test_metric_calculation_failure_creates_no_measurement(setup, postgres_session_factory, monkeypatch, engine):
    import app.session_routes as routes

    client, service, fake = setup
    created = service.start()
    before = stored_state(postgres_session_factory, created.id)

    def fail(*args):
        raise RuntimeError("Injected metric calculation failure")

    monkeypatch.setattr(routes, engine, fail)
    response = transcribe(client, created.id)
    assert response.status_code == 500
    assert measurement_rows(postgres_session_factory) == []
    assert stored_state(postgres_session_factory, created.id) == before
    assert fake.calls == 1


@pytest.mark.parametrize("failure_stage", ["after_flush_postexec", "before_commit"])
def test_measurement_flush_or_commit_failure_does_not_leave_a_partial_row(
        setup, postgres_engine, postgres_session_factory, failure_stage, authenticated_principal):
    _, service, fake = setup
    created = service.start()
    before = stored_state(postgres_session_factory, created.id)

    class FailingSession(Session):
        pass

    def fail(*args):
        raise RuntimeError("Injected measurement transaction failure")

    event.listen(FailingSession, failure_stage, fail)
    failing = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=FailingSession), authenticated_principal)
    try:
        with pytest.raises(RuntimeError, match="Injected measurement transaction failure"):
            failing.create_measurement(created.id, 0, metrics_for(fake.result),
                                       delivery_metrics=delivery_for(fake.result), expected_last_attempt_number=0)
    finally:
        event.remove(FailingSession, failure_stage, fail)
    assert measurement_rows(postgres_session_factory) == []
    assert stored_state(postgres_session_factory, created.id) == before


@pytest.mark.parametrize("context", ["unknown", "stale", "future", "completed"])
def test_create_measurement_rejects_invalid_authoritative_session_context(setup, postgres_session_factory, context):
    _, service, fake = setup
    created = service.start()
    if context == "unknown":
        identifier, index, error = uuid4(), 0, SessionNotFound
    elif context == "completed":
        advance(service, created.id, 5)
        identifier, index, error = created.id, 5, SessionConflict
    elif context == "stale":
        advance(service, created.id, 1)
        identifier, index, error = created.id, 0, SessionConflict
    else:
        identifier, index, error = created.id, 1, SessionConflict
    before = stored_state(postgres_session_factory, created.id)
    with pytest.raises(error):
        service.create_measurement(identifier, index, metrics_for(fake.result), expected_last_attempt_number=0)
    assert measurement_rows(postgres_session_factory) == []
    assert stored_state(postgres_session_factory, created.id) == before
    assert fake.calls == 0


@pytest.mark.parametrize("initial_answers", [0, 4])
def test_no_database_checkout_or_lock_is_held_during_provider_and_same_question_revision_is_rejected(
        setup, postgres_engine, postgres_session_factory, initial_answers):
    client, service, fake = setup
    created = service.start()
    advance(service, created.id, initial_answers)
    checked_out = set()

    def checkout(connection, record, proxy):
        checked_out.add(id(connection))

    def checkin(connection, record):
        checked_out.discard(id(connection))

    def during_provider():
        assert checked_out == set()
        assert postgres_engine.pool.checkedout() == 0
        # A separate connection can acquire this session's row lock while the
        # synthetic provider is executing, then append without advancing.
        with ThreadPoolExecutor(max_workers=1) as executor:
            updated = executor.submit(service.submit_attempt, created.id, initial_answers, AttemptRequest(
                expected_last_attempt_number=0, answer="Other tab accepted",
            )).result(timeout=3)
        assert updated.session.current_question_index == initial_answers
        assert updated.session.current_question_latest_attempt_number == 1
        assert checked_out == set()

    fake.during_provider = during_provider
    event.listen(postgres_engine, "checkout", checkout)
    event.listen(postgres_engine, "checkin", checkin)
    try:
        response = transcribe(client, created.id, initial_answers)
        assert response.status_code == 409
        assert checked_out == set()
        assert postgres_engine.pool.checkedout() == 0
    finally:
        event.remove(postgres_engine, "checkout", checkout)
        event.remove(postgres_engine, "checkin", checkin)
    assert measurement_rows(postgres_session_factory) == []
    stored, attempts = stored_state(postgres_session_factory, created.id)
    assert stored["current_question_index"] == initial_answers
    assert stored["status"] == "active"
    assert attempts[-1]["answer_text"] == "Other tab accepted"
    assert fake.calls == 1


@pytest.mark.parametrize("engine", ["measure_transcription", "measure_delivery"])
def test_attempt_appended_after_metric_calculation_is_revalidated_before_measurement_insert(
        setup, postgres_session_factory, monkeypatch, engine):
    import app.session_routes as routes

    client, service, fake = setup
    created = service.start()

    def calculate_then_append(*args):
        metrics = (measure_transcription if engine == "measure_transcription" else measure_delivery)(*args)
        service.submit_attempt(created.id, 0, AttemptRequest(expected_last_attempt_number=0, answer="Won the race"))
        return metrics

    monkeypatch.setattr(routes, engine, calculate_then_append)
    response = transcribe(client, created.id)
    assert response.status_code == 409
    assert measurement_rows(postgres_session_factory) == []
    assert service.get(created.id).answers == []
    assert service.get(created.id).current_question_latest_attempt_number == 1
    assert service.get_attempts(created.id, 0)[0].answer == "Won the race"
    assert fake.calls == 1


def test_measurement_creation_waits_for_session_row_lock_then_rechecks_context(
        setup, postgres_engine, postgres_session_factory):
    """Observe a real blocker before committing a same-question revision change."""
    _, service, fake = setup
    created = service.start()
    started = Event()
    pids = {}
    results = {}
    errors = {}

    def capture_waiter(connection, cursor, statement, parameters, context, executemany):
        if (current_thread().name == "measurement-context-racer"
                and "FOR UPDATE" in statement.upper() and "interview_sessions" in statement):
            pids["waiter"] = connection.connection.dbapi_connection.info.backend_pid
            started.set()

    def create_measurement():
        try:
            results["measurement_id"] = service.create_measurement(created.id, 0, metrics_for(fake.result), expected_last_attempt_number=0)
        except Exception as error:
            errors["measurement"] = error

    worker = Thread(name="measurement-context-racer", target=create_measurement, daemon=True)
    event.listen(postgres_engine, "before_cursor_execute", capture_waiter)
    launched = False
    try:
        with postgres_session_factory.begin() as holding:
            stored = holding.scalar(select(StoredInterviewSession).where(
                StoredInterviewSession.id == created.id,
            ).with_for_update())
            holder_pid = holding.connection().connection.dbapi_connection.info.backend_pid
            worker.start()
            launched = True
            assert started.wait(5), "Measurement creation did not attempt the session row lock"
            blocked = False
            deadline = monotonic() + 5
            with postgres_engine.connect() as observer:
                while monotonic() < deadline:
                    blockers = observer.scalar(select(func.pg_blocking_pids(pids["waiter"])))
                    if holder_pid in blockers:
                        blocked = True
                        break
                    sleep(0.01)
            assert blocked, "Measurement creation did not wait for the locked session row"
            holding.add(QuestionAttempt(
                session_id=created.id, question_index=0, attempt_number=1, answer_text="Accepted while locked",
            ))
            holding.flush()
        worker.join(10)
        assert not worker.is_alive()
    finally:
        if launched:
            worker.join(10)
        event.remove(postgres_engine, "before_cursor_execute", capture_waiter)
    assert isinstance(errors.get("measurement"), SessionConflict)
    assert results == {}
    assert measurement_rows(postgres_session_factory) == []
    restored = service.get(created.id)
    assert restored.current_question_index == 0
    assert restored.current_question_latest_attempt_number == 1
    assert restored.answers == []
    assert service.get_attempts(created.id, 0)[0].answer == "Accepted while locked"


def test_concurrent_submissions_attach_once_and_preserve_session_locking(setup, postgres_session_factory):
    _, service, fake = setup
    created = service.start()
    identifier = service.create_measurement(created.id, 0, metrics_for(fake.result), expected_last_attempt_number=0)
    before = measurement_rows(postgres_session_factory)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(service.submit_attempt, created.id, 0, AttemptRequest(
            expected_last_attempt_number=0, answer=f"Concurrent answer {index}", measurement_id=identifier,
        )) for index in range(2)]
        successes, failures = [], []
        for future in futures:
            try:
                successes.append(future.result(timeout=5))
            except SessionConflict as error:
                failures.append(error)
    assert len(successes) == len(failures) == 1
    session, attempts = stored_state(postgres_session_factory, created.id)
    assert session["current_question_index"] == 0
    assert session["completed_at"] is None
    assert len(attempts) == 1
    assert attempts[0]["measurement_id"] == identifier
    assert attempts[0]["answer_text"] == successes[0].attempt.answer
    assert measurement_rows(postgres_session_factory) == before


def test_retry_attempts_keep_separate_explicit_measurements_and_typed_retry_is_unmeasured(
        setup, postgres_session_factory):
    client, service, fake = setup
    created = service.start()
    first = transcribe(client, created.id)
    assert first.status_code == 200
    first_id = UUID(first.json()["measurement_id"])
    first_metrics = first.json()["metrics"]
    first_attempt = service.submit_attempt(created.id, 0, AttemptRequest(
        expected_last_attempt_number=0, answer="Edited first answer", measurement_id=first_id,
    ))
    initial_attempt = stored_state(postgres_session_factory, created.id)[1][0]
    initial_measurement = measurement_rows(postgres_session_factory)[0]
    fake.result = TranscriptionResult(text="A new retry transcript", language="eng")
    second = transcribe(client, created.id, revision=1)
    assert second.status_code == 200
    second_id = UUID(second.json()["measurement_id"])
    assert second_id != first_id
    second_attempt = service.submit_attempt(created.id, 0, AttemptRequest(
        expected_last_attempt_number=1, answer="Edited retry answer", measurement_id=second_id,
    ))
    typed_attempt = service.submit_attempt(created.id, 0, AttemptRequest(
        expected_last_attempt_number=2, answer="Typed third answer",
    ))
    state, attempts = stored_state(postgres_session_factory, created.id)
    assert [item["attempt_number"] for item in attempts] == [1, 2, 3]
    assert [item["measurement_id"] for item in attempts] == [first_id, second_id, None]
    assert attempts[0] == initial_attempt
    assert [item.id for item in service.get_attempts(created.id, 0)] == [
        first_attempt.attempt.id, second_attempt.attempt.id, typed_attempt.attempt.id,
    ]
    rows = measurement_rows(postgres_session_factory)
    assert len(rows) == 2
    assert next(item for item in rows if item["id"] == first_id) == initial_measurement
    assert metrics_projection(initial_measurement) == first_metrics
    assert delivery_projection(initial_measurement) == first.json()["delivery_metrics"]
    assert metrics_projection(next(item for item in rows if item["id"] == second_id)) == second.json()["metrics"]
    assert delivery_projection(next(item for item in rows if item["id"] == second_id)) == second.json()["delivery_metrics"]
    assert (state["current_question_index"], state["status"], state["completed_at"]) == (0, "active", None)
    assert service.get(created.id).answers == []
    assert service.get(created.id).current_question_latest_attempt_number == 3
    assert fake.calls == 2


def test_linked_measurement_cannot_be_reused_for_retry_on_same_question(setup, postgres_session_factory):
    client, service, _ = setup
    created = service.start()
    response = transcribe(client, created.id)
    assert response.status_code == 200
    measurement_id = response.json()["measurement_id"]
    accepted = client.post(f"/api/sessions/{created.id}/questions/0/attempts", json={
        "expected_last_attempt_number": 0, "answer": "First", "measurement_id": measurement_id,
    })
    assert accepted.status_code == 201
    before = stored_state(postgres_session_factory, created.id)
    measurements_before = measurement_rows(postgres_session_factory)
    rejected = client.post(f"/api/sessions/{created.id}/questions/0/attempts", json={
        "expected_last_attempt_number": 1, "answer": "Retry", "measurement_id": measurement_id,
    })
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "Measurement cannot be attached to this answer."}
    assert stored_state(postgres_session_factory, created.id) == before
    assert measurement_rows(postgres_session_factory) == measurements_before


def test_stale_transcription_revision_rejected_before_provider(setup, postgres_session_factory):
    client, service, fake = setup
    created = service.start()
    service.submit_attempt(created.id, 0, AttemptRequest(
        expected_last_attempt_number=0, answer="Accepted first attempt",
    ))
    before = stored_state(postgres_session_factory, created.id)
    response = transcribe(client, created.id, revision=0)
    assert response.status_code == 409
    assert fake.calls == 0
    assert measurement_rows(postgres_session_factory) == []
    assert stored_state(postgres_session_factory, created.id) == before


@pytest.mark.parametrize("failure_stage", ["after_flush_postexec", "before_commit"])
def test_failed_measured_retry_keeps_prior_attempt_and_measurement_unchanged(
        setup, postgres_engine, postgres_session_factory, failure_stage, authenticated_principal):
    _, service, fake = setup
    created = service.start()
    first_id = service.create_measurement(
        created.id, 0, metrics_for(fake.result), expected_last_attempt_number=0,
    )
    service.submit_attempt(created.id, 0, AttemptRequest(
        expected_last_attempt_number=0, answer="Saved first attempt", measurement_id=first_id,
    ))
    retry_id = service.create_measurement(
        created.id, 0, metrics_for(fake.result), expected_last_attempt_number=1,
    )
    before = stored_state(postgres_session_factory, created.id)
    measurements_before = measurement_rows(postgres_session_factory)

    class FailingSession(Session):
        pass

    def fail(*args):
        raise RuntimeError("Injected retry failure")

    event.listen(FailingSession, failure_stage, fail)
    failing = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=FailingSession), authenticated_principal)
    try:
        with pytest.raises(RuntimeError, match="Injected retry failure"):
            failing.submit_attempt(created.id, 0, AttemptRequest(
                expected_last_attempt_number=1, answer="Rejected retry", measurement_id=retry_id,
            ))
    finally:
        event.remove(FailingSession, failure_stage, fail)
    assert stored_state(postgres_session_factory, created.id) == before
    assert measurement_rows(postgres_session_factory) == measurements_before
    accepted = service.submit_attempt(created.id, 0, AttemptRequest(
        expected_last_attempt_number=1, answer="Successful retry", measurement_id=retry_id,
    ))
    assert accepted.attempt.attempt_number == 2
    assert accepted.attempt.measurement_id == retry_id


@pytest.mark.parametrize(("result", "expected"), [
    (TranscriptionResult(text="one two", language="eng", words=[
        {"text": "one", "start": 0.0, "end": 0.2}, {"text": "two", "start": 0.7, "end": 1.0},
    ]), (1, 0.5, 0.5, None)),
    (TranscriptionResult(text="one two", language="eng", words=[
        {"text": "one", "start": 0.0, "end": 0.2}, {"text": "two", "start": 0.69, "end": 1.0},
    ]), (0, 0.0, 0.0, None)),
    (TranscriptionResult(text="one", language="eng", words=[
        {"text": "one", "start": 2.0, "end": 2.25},
    ]), (0, 0.0, 0.0, None)),
    (TranscriptionResult(text="one two three", language="eng", words=[
        {"text": "one", "start": 0.0, "end": 1.2345678901234567},
        {"text": "two", "start": 2.234567890123458, "end": 2.5},
        {"text": "three", "start": 3.1, "end": 3.3},
    ]), (2, 1.6000000000000013, 1.0000000000000013, None)),
    (TranscriptionResult(text="one two", language="eng"), (None, None, None, "missing_timings")),
    (TranscriptionResult(text="one two", language="eng", words=[
        {"text": "one", "start": 0.0, "end": 0.2},
    ]), (None, None, None, "timing_coverage_mismatch")),
    (TranscriptionResult(text="one two", language="eng", words=[
        {"text": "one", "start": 0.0, "end": 1.0}, {"text": "two", "start": 0.5, "end": 2.0},
    ]), (None, None, None, "invalid_timing_order")),
    (TranscriptionResult(text="...", language="eng", words=[
        {"text": "...", "start": 0.0, "end": 1.0},
    ]), (None, None, None, "missing_timings")),
    (TranscriptionResult(text="one", language="eng", words=[
        {"text": "one", "start": 0.0, "end": 0.0},
    ]), (None, None, None, "unusable_span")),
])
def test_transcription_round_trips_both_families_in_one_complete_snapshot(
        setup, postgres_session_factory, result, expected):
    client, service, fake = setup
    fake.result = result
    created = service.start()
    before = result.model_dump()
    response = transcribe(client, created.id)
    assert response.status_code == 200
    body = response.json()
    rows = measurement_rows(postgres_session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert str(row["id"]) == body["measurement_id"]
    assert row["measurement_version"] == "speaking-metrics-v1"
    assert row["delivery_measurement_version"] == "pause-metrics-v1"
    assert row["measurement_source"] == body["metrics"]["source"] == body["delivery_metrics"]["source"] == "original_transcription"
    assert metrics_projection(row) == metrics_for(result).model_dump() == body["metrics"]
    assert delivery_projection(row) == body["delivery_metrics"]
    assert (row["pause_count"], row["total_pause_duration_seconds"], row["longest_pause_seconds"], row["pause_unavailable_reason"]) == expected
    assert body["delivery_metrics"] == {**delivery_for(result).model_dump(), "source": row["measurement_source"]}
    assert set(body["delivery_metrics"]) == {
        "version", "source", "pause_count", "total_pause_duration_seconds", "longest_pause_seconds", "unavailable_reason",
    }
    assert body["words"] == result.model_dump()["words"]
    assert not ({"words", "timings", "pause_events", "pause_gaps", "provider_payload"} & body["delivery_metrics"].keys())
    assert all(not isinstance(value, (list, dict)) for value in body["delivery_metrics"].values())
    assert result.model_dump() == before
    assert fake.calls == 1
    assert service.get(created.id) == created
    assert stored_state(postgres_session_factory, created.id)[1] == []


def test_legacy_direct_measurement_remains_readable_without_fabricated_delivery_version(setup, postgres_session_factory):
    _, service, fake = setup
    created = service.start()
    expected = metrics_for(fake.result)
    identifier = service.create_measurement(created.id, 0, expected, expected_last_attempt_number=0)
    rows = measurement_rows(postgres_session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == identifier
    assert metrics_projection(row) == expected.model_dump()
    assert delivery_projection(row) is None
    for field in ("delivery_measurement_version", "pause_count", "total_pause_duration_seconds",
                  "longest_pause_seconds", "pause_unavailable_reason"):
        assert row[field] is None
    with postgres_session_factory() as database:
        stored = database.get(TranscriptionMeasurement, identifier)
        assert stored.delivery_measurement_version is None
        assert stored.pause_count is None
    linked = service.submit_attempt(created.id, 0, AttemptRequest(
        expected_last_attempt_number=0, answer="A historical measurement remains linkable", measurement_id=identifier,
    ))
    assert linked.attempt.measurement_id == identifier
    assert measurement_rows(postgres_session_factory) == rows


def test_delivery_exception_happens_before_any_measurement_insert(setup, postgres_session_factory, monkeypatch):
    import app.session_routes as routes

    client, service, fake = setup
    created = service.start()
    writes = []
    original = service.create_measurement

    def capture_insert(*args, **kwargs):
        writes.append(True)
        return original(*args, **kwargs)

    def fail_delivery(*args):
        raise RuntimeError("Injected delivery engine failure")

    monkeypatch.setattr(service, "create_measurement", capture_insert)
    monkeypatch.setattr(routes, "measure_delivery", fail_delivery)
    response = transcribe(client, created.id)
    assert response.status_code == 500
    assert writes == []
    assert measurement_rows(postgres_session_factory) == []
    assert service.get(created.id) == created
    assert fake.calls == 1


def test_delivery_snapshot_adds_no_content_or_timing_logging(setup, postgres_session_factory, caplog, capsys):
    client, service, fake = setup
    fake.result = TranscriptionResult(text="PRIVATE-TRANSCRIPT token", language="eng", words=[
        {"text": "PRIVATE-TRANSCRIPT", "start": 0.0, "end": 0.2},
        {"text": "token", "start": 0.7, "end": 1.0},
    ])
    created = service.start()
    response = transcribe(client, created.id)
    assert response.status_code == 200
    output = capsys.readouterr()
    assert "PRIVATE-TRANSCRIPT" not in caplog.text + output.out + output.err
    assert "PRIVATE-SYNTHETIC-AUDIO" not in caplog.text + output.out + output.err
    body = response.json()
    assert "PRIVATE-TRANSCRIPT" not in str(body["delivery_metrics"])
    assert not any(isinstance(value, (list, dict, bytes)) for value in measurement_rows(postgres_session_factory)[0].values())
    assert body["words"] == fake.result.model_dump()["words"]
    assert not any(isinstance(value, (list, dict)) for value in body["delivery_metrics"].values())
    assert fake.calls == 1
