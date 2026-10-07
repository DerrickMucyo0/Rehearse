"""Owner-scoped history reads against isolated, real PostgreSQL.

All measurements are synthetic and enter through the normal session service.
The transport guard prohibits transcription providers and other HTTP requests.
"""

from datetime import datetime, timezone
from threading import Event, Thread, current_thread
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.database import create_database_engine, create_session_factory
from app.database_models import (
    MEASUREMENT_VERSION, QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement,
)
from app.delivery_metrics import DeliveryMetrics
from app.history import HistoryReadService
from app.history_routes import get_history_service
from app.main import app
from app.session_routes import get_session_service
from app.sessions import AttemptRequest, ContinueRequest, InterviewSessionService
from app.speaking_metrics import SpeakingMetrics


SUMMARY_FIELDS = {
    "session_id", "scenario_type", "status", "created_at", "completed_at", "current_question_number",
    "total_questions", "finalized_question_count", "questions_practiced_count",
    "total_attempt_count", "total_retry_count", "measured_final_answer_count",
    "last_submitted_at", "last_saved_activity_at", "finalized_points",
}
MEASUREMENT_FIELDS = {
    "measurement_version", "measurement_source", "recognized_word_count", "um_count",
    "uh_count", "filler_unavailable_reason", "timed_utterance_span_seconds",
    "estimated_words_per_minute", "timing_unavailable_reason",
    "delivery_metrics",
}
POINT_FIELDS = {"question_index", "attempt_id", "attempt_number", "submitted_at", "measurement"}
QUESTION_FIELDS = {
    "question_index", "question_text", "finalized", "attempt_count", "latest_attempt_id",
    "latest_attempt_number", "final_attempt_id", "final_attempt_number",
}
ATTEMPT_FIELDS = {
    "attempt_id", "attempt_number", "answer_text", "submitted_at", "is_final", "measurement",
}


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    def blocked(*args, **kwargs):
        raise AssertionError("Provider requests are forbidden in history tests")

    async def async_blocked(*args, **kwargs):
        raise AssertionError("Provider requests are forbidden in history tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)


@pytest.fixture
def sessions(postgres_session_factory, authenticated_principal):
    return InterviewSessionService(postgres_session_factory, authenticated_principal)


@pytest.fixture
def history(postgres_session_factory, authenticated_principal):
    return HistoryReadService(postgres_session_factory, authenticated_principal)


@pytest.fixture
def client(sessions, history, authenticated_session_override, authenticated_http_headers):
    app.dependency_overrides[get_session_service] = authenticated_session_override(sessions)
    app.dependency_overrides[get_history_service] = authenticated_session_override(history)
    try:
        with TestClient(app, raise_server_exceptions=False, headers=authenticated_http_headers) as result:
            yield result
    finally:
        app.dependency_overrides.pop(get_session_service, None)
        app.dependency_overrides.pop(get_history_service, None)


def metrics(**changes):
    values = {
        "recognized_word_count": 4, "um_count": 0, "uh_count": 0,
        "filler_unavailable_reason": None,
        "timed_utterance_span_seconds": 1.234567890123,
        "estimated_words_per_minute": 194.40000174967392,
        "timing_unavailable_reason": None,
    }
    values.update(changes)
    return SpeakingMetrics(**values)


def measurement_payload(values, version=MEASUREMENT_VERSION, delivery=None):
    return {
        "measurement_version": version, "measurement_source": values.source,
        **values.model_dump(exclude={"source"}),
        "delivery_metrics": {**delivery.model_dump(), "source": values.source} if delivery is not None else None,
    }


def submit(sessions, identifier, *, question=0, revision=0, measurement=None, answer="Answer"):
    return sessions.submit_attempt(identifier, question, AttemptRequest(
        expected_last_attempt_number=revision, answer=answer, measurement_id=measurement,
    )).attempt


def measured_attempt(sessions, identifier, values=None, *, question=0, revision=0, answer="Answer", delivery=None):
    measurement = sessions.create_measurement(
        identifier, question, values or metrics(), expected_last_attempt_number=revision, delivery_metrics=delivery,
    )
    return submit(sessions, identifier, question=question, revision=revision,
                  measurement=measurement, answer=answer)


def advance(sessions, identifier, question=0, revision=1):
    return sessions.continue_question(identifier, question, ContinueRequest(
        expected_last_attempt_number=revision,
    ))


def batch(client, identifiers):
    return client.post("/api/history/summaries", json={"session_ids": [str(item) for item in identifiers]})


def detail_url(identifier):
    return f"/api/sessions/{identifier}/history-detail"


def summary_payload(history, identifier):
    return history.get_summaries([identifier]).model_dump(mode="json")["summaries"][0]


def rows(factory):
    with factory() as database:
        return tuple(
            database.execute(select(model.__table__).order_by(model.id)).all()
            for model in (StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
        )


def data_statement(statement):
    return statement.lstrip().upper().startswith(("SELECT", "WITH"))


def test_batch_reads_only_requested_owned_sessions_and_missing_ids_keep_first_order(client, sessions):
    first, second, hidden = (sessions.start() for _ in range(3))
    submit(sessions, hidden.id, answer="PRIVATE-UNREQUESTED-ANSWER")
    unknown, other_unknown = uuid4(), uuid4()
    response = client.post("/api/history/summaries", json={"session_ids": [
        str(second.id).upper(), str(other_unknown).upper(), str(first.id), str(second.id),
        str(unknown), str(other_unknown),
    ]})
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"summaries", "missing_session_ids"}
    assert [item["session_id"] for item in payload["summaries"]] == [str(second.id), str(first.id)]
    assert payload["missing_session_ids"] == [str(other_unknown), str(unknown)]
    assert response.headers["Cache-Control"] == "no-store"
    assert str(hidden.id) not in response.text
    assert "PRIVATE-UNREQUESTED-ANSWER" not in response.text
    discovery = client.get("/api/history/summaries")
    assert discovery.status_code == 200
    assert {item["session_id"] for item in discovery.json()["items"]} == {
        str(first.id), str(second.id), str(hidden.id),
    }
    assert discovery.json()["next_cursor"] is None
    assert "PRIVATE-UNREQUESTED-ANSWER" not in discovery.text
    assert client.get("/api/sessions").status_code in {404, 405}


def test_all_missing_batch_returns_only_canonical_requested_ids(client, sessions):
    unrelated = sessions.start()
    attempt = measured_attempt(sessions, unrelated.id, metrics(recognized_word_count=777),
                               answer="PRIVATE-UNRELATED-ALL-MISSING-ANSWER")
    advance(sessions, unrelated.id)
    first, second = uuid4(), uuid4()
    response = client.post("/api/history/summaries", json={"session_ids": [
        str(second).upper(), str(first), str(second), str(first).upper(),
    ]})
    assert response.status_code == 200
    assert response.json() == {
        "summaries": [], "missing_session_ids": [str(second), str(first)],
    }
    assert response.headers["Cache-Control"] == "no-store"
    for identifier in (unrelated.id, attempt.id, attempt.measurement_id):
        assert str(identifier) not in response.text
    assert "PRIVATE-UNRELATED-ALL-MISSING-ANSWER" not in response.text
    assert "recognized_word_count" not in response.text
    assert "measurement" not in response.text


def test_summaries_sort_saved_activity_descending_with_uuid_ties(
        client, sessions, postgres_session_factory):
    older, first_tie, second_tie = (sessions.start() for _ in range(3))
    with postgres_session_factory.begin() as database:
        for identifier, day in ((older.id, 1), (first_tie.id, 2), (second_tie.id, 2)):
            database.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == identifier)
                             .values(created_at=datetime(2026, 1, day, tzinfo=timezone.utc)))
    response = batch(client, [older.id, second_tie.id, first_tie.id])
    assert response.status_code == 200
    expected = sorted((first_tie.id, second_tie.id), key=lambda item: item.int) + [older.id]
    assert [item["session_id"] for item in response.json()["summaries"]] == [str(item) for item in expected]


@pytest.mark.parametrize("payload", [
    {"session_ids": []}, {"session_ids": [str(uuid4())] * 51},
    {"session_ids": [None]}, {"session_ids": [True]}, {"session_ids": [123]},
    {"session_ids": [{}]}, {"session_ids": ["not-a-uuid"]},
    {"session_ids": str(uuid4())}, {"session_ids": [str(uuid4())], "all_sessions": True},
    {}, [], None,
])
def test_batch_rejects_invalid_raw_requests_without_reading_database(client, postgres_engine, payload):
    statements = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", observe)
    try:
        response = client.post("/api/history/summaries", json=payload)
        assert response.status_code == 422
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        event.remove(postgres_engine, "before_cursor_execute", observe)
    assert statements == []


def test_empty_summary_and_detail_have_exact_bounded_shapes(client, sessions):
    created = sessions.start()
    summary = batch(client, [created.id]).json()["summaries"][0]
    assert set(summary) == SUMMARY_FIELDS
    assert summary["scenario_type"] == "job_interview"
    assert summary["status"] == "active"
    assert summary["current_question_number"] == 1
    assert summary["total_questions"] == 5
    for field in ("finalized_question_count", "questions_practiced_count", "total_attempt_count",
                  "total_retry_count", "measured_final_answer_count"):
        assert summary[field] == 0
    assert datetime.fromisoformat(summary["created_at"]).tzinfo is not None
    assert summary["completed_at"] is summary["last_submitted_at"] is None
    assert summary["last_saved_activity_at"] == summary["created_at"]
    assert summary["finalized_points"] == []
    response = client.get(detail_url(created.id))
    assert response.status_code == 200
    detail = response.json()
    assert set(detail) == {"summary", "questions", "selected_question"}
    assert detail["summary"] == summary
    assert detail["selected_question"] is None
    assert response.headers["Cache-Control"] == "no-store"
    assert [item["question_text"] for item in detail["questions"]] == created.questions
    for index, question in enumerate(detail["questions"]):
        assert set(question) == QUESTION_FIELDS
        assert question == {
            "question_index": index, "question_text": created.questions[index], "finalized": False,
            "attempt_count": 0, "latest_attempt_id": None, "latest_attempt_number": None,
            "final_attempt_id": None, "final_attempt_number": None,
        }
    selected = client.get(detail_url(created.id), params={"question_index": 0}).json()["selected_question"]
    assert selected == {"question_index": 0, "attempts": [], "has_more": False,
                        "next_after_attempt_number": None}


def test_summary_counts_distinguish_practiced_current_question_from_finalized_question(client, sessions):
    created = sessions.start()
    first = submit(sessions, created.id, answer="PRIVATE-FIRST-ANSWER")
    second = submit(sessions, created.id, revision=1, answer="PRIVATE-FINAL-ANSWER")
    advance(sessions, created.id, revision=2)
    current = submit(sessions, created.id, question=1, answer="PRIVATE-CURRENT-ANSWER")
    response = batch(client, [created.id])
    summary = response.json()["summaries"][0]
    assert summary["current_question_number"] == 2
    assert summary["finalized_question_count"] == 1
    assert summary["questions_practiced_count"] == 2
    assert summary["total_attempt_count"] == 3
    assert summary["total_retry_count"] == 1
    assert summary["measured_final_answer_count"] == 0
    assert datetime.fromisoformat(summary["last_submitted_at"]) == current.submitted_at
    assert summary["last_saved_activity_at"] == summary["last_submitted_at"]
    assert "PRIVATE-" not in response.text
    assert len(summary["finalized_points"]) == 1
    point = summary["finalized_points"][0]
    assert point == {
        "question_index": 0, "attempt_id": str(second.id), "attempt_number": 2,
        "submitted_at": point["submitted_at"], "measurement": None,
    }
    assert datetime.fromisoformat(point["submitted_at"]) == second.submitted_at
    detail = client.get(detail_url(created.id), params={"question_index": 0}).json()
    assert detail["questions"][0]["latest_attempt_id"] == str(second.id)
    assert detail["questions"][0]["final_attempt_id"] == str(second.id)
    assert detail["questions"][1]["latest_attempt_id"] == str(current.id)
    assert detail["questions"][1]["final_attempt_id"] is None
    assert [item["attempt_id"] for item in detail["selected_question"]["attempts"]] == [str(first.id), str(second.id)]
    assert [item["is_final"] for item in detail["selected_question"]["attempts"]] == [False, True]
    assert "PRIVATE-CURRENT-ANSWER" not in str(detail)


def test_number_gaps_use_actual_counts_and_greatest_number_not_timestamp(
        client, sessions, postgres_session_factory):
    created = sessions.start()
    earlier = datetime(2026, 1, 2, tzinfo=timezone.utc)
    later = datetime(2026, 1, 3, tzinfo=timezone.utc)
    identifiers = {number: uuid4() for number in (1, 3)}
    with postgres_session_factory.begin() as database:
        database.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == created.id)
                         .values(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        for number, submitted_at in ((1, later), (3, earlier)):
            values = metrics()
            pauses = delivery() if number == 1 else delivery(
                pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0,
            )
            measurement = TranscriptionMeasurement(
                session_id=created.id, question_index=0, measurement_version=MEASUREMENT_VERSION,
                measurement_source=values.source, **values.model_dump(exclude={"source"}),
                delivery_measurement_version=pauses.version, pause_count=pauses.pause_count,
                total_pause_duration_seconds=pauses.total_pause_duration_seconds,
                longest_pause_seconds=pauses.longest_pause_seconds, pause_unavailable_reason=None,
            )
            database.add(measurement)
            database.flush()
            database.execute(insert(QuestionAttempt).values(
                id=identifiers[number], session_id=created.id, question_index=0,
                attempt_number=number, answer_text=f"Historical {number}", submitted_at=submitted_at,
                measurement_id=measurement.id,
            ))
    advance(sessions, created.id, revision=3)
    summary = batch(client, [created.id]).json()["summaries"][0]
    assert (summary["total_attempt_count"], summary["total_retry_count"]) == (2, 1)
    assert datetime.fromisoformat(summary["last_submitted_at"]) == later
    assert summary["finalized_points"][0]["attempt_id"] == str(identifiers[3])
    assert summary["finalized_points"][0]["attempt_number"] == 3
    assert summary["finalized_points"][0]["measurement"]["delivery_metrics"]["pause_count"] == 0
    first_page = client.get(detail_url(created.id), params={"question_index": 0, "limit": 1}).json()["selected_question"]
    assert [item["attempt_number"] for item in first_page["attempts"]] == [1]
    assert first_page["has_more"] is True
    assert first_page["next_after_attempt_number"] == 1
    assert first_page["attempts"][0]["measurement"]["delivery_metrics"]["pause_count"] == 2
    last_page = client.get(detail_url(created.id), params={
        "question_index": 0, "limit": 1, "after_attempt_number": 1,
    }).json()["selected_question"]
    assert [item["attempt_number"] for item in last_page["attempts"]] == [3]
    assert last_page["attempts"][0]["measurement"]["delivery_metrics"]["pause_count"] == 0
    assert last_page["attempts"][0]["is_final"] is True
    assert last_page["has_more"] is False
    assert last_page["next_after_attempt_number"] is None


def test_fifth_answer_is_provisional_until_continue_and_completion_dates_are_stored(client, sessions):
    created = sessions.start()
    for question in range(4):
        submit(sessions, created.id, question=question)
        advance(sessions, created.id, question)
    fifth = measured_attempt(sessions, created.id, question=4)
    pending = batch(client, [created.id]).json()["summaries"][0]
    assert pending["status"] == "active"
    assert pending["current_question_number"] == 5
    assert pending["completed_at"] is None
    assert (pending["finalized_question_count"], pending["questions_practiced_count"]) == (4, 5)
    assert len(pending["finalized_points"]) == 4
    assert pending["measured_final_answer_count"] == 0
    assert client.get(detail_url(created.id), params={"question_index": 4}).json()["selected_question"]["attempts"][0]["is_final"] is False
    advance(sessions, created.id, 4)
    complete = batch(client, [created.id]).json()["summaries"][0]
    assert complete["status"] == "completed"
    assert complete["current_question_number"] is None
    assert complete["finalized_question_count"] == 5
    assert complete["measured_final_answer_count"] == 1
    assert [item["question_index"] for item in complete["finalized_points"]] == list(range(5))
    assert complete["finalized_points"][-1]["attempt_id"] == str(fifth.id)
    assert datetime.fromisoformat(complete["completed_at"]).tzinfo is not None
    assert datetime.fromisoformat(complete["completed_at"]) >= datetime.fromisoformat(complete["created_at"])
    assert complete["last_saved_activity_at"] == complete["completed_at"]


def test_final_metrics_keep_exact_provenance_zero_null_and_unrounded_values(client, sessions):
    created = sessions.start()
    measured_attempt(sessions, created.id, metrics(recognized_word_count=99))
    values = metrics()
    final = measured_attempt(sessions, created.id, values, revision=1,
                             answer="An edited answer with a different number of words")
    unlinked = sessions.create_measurement(created.id, 0, metrics(recognized_word_count=999),
                                          expected_last_attempt_number=2)
    advance(sessions, created.id, revision=2)
    older = measured_attempt(sessions, created.id, question=1)
    typed = submit(sessions, created.id, question=1, revision=1, answer="Typed final")
    advance(sessions, created.id, 1, 2)
    unavailable = metrics(
        recognized_word_count=0, um_count=None, uh_count=None,
        filler_unavailable_reason="unsupported_language", timed_utterance_span_seconds=None,
        estimated_words_per_minute=None, timing_unavailable_reason="missing_timings",
    )
    measured_attempt(sessions, created.id, unavailable, question=2)
    advance(sessions, created.id, 2)
    response = batch(client, [created.id])
    summary = response.json()["summaries"][0]
    assert summary["measured_final_answer_count"] == 2
    points = summary["finalized_points"]
    assert all(set(item) == POINT_FIELDS for item in points)
    assert points[0]["attempt_id"] == str(final.id)
    assert points[0]["measurement"] == measurement_payload(values)
    assert set(points[0]["measurement"]) == MEASUREMENT_FIELDS
    assert points[0]["measurement"]["um_count"] == points[0]["measurement"]["uh_count"] == 0
    assert points[0]["measurement"]["timed_utterance_span_seconds"] != round(values.timed_utterance_span_seconds, 2)
    assert points[1]["attempt_id"] == str(typed.id)
    assert points[1]["measurement"] is None
    assert points[2]["measurement"] == measurement_payload(unavailable)
    for measurement_id in (final.measurement_id, older.measurement_id, unlinked):
        assert str(measurement_id) not in response.text
    detail_response = client.get(detail_url(created.id), params={"question_index": 1})
    attempts = detail_response.json()["selected_question"]["attempts"]
    assert all(set(item) == ATTEMPT_FIELDS for item in attempts)
    assert attempts[0]["measurement"] == measurement_payload(metrics())
    assert attempts[1]["measurement"] is None
    assert attempts[1]["is_final"] is True
    assert str(older.measurement_id) not in detail_response.text
    assert "measurement_id" not in detail_response.text


def test_historical_measurement_definition_is_preserved_in_reads(client, sessions, postgres_session_factory):
    created = sessions.start()
    values = metrics()
    with postgres_session_factory.begin() as database:
        measurement = TranscriptionMeasurement(
            session_id=created.id, question_index=0, measurement_version="speaking-metrics-historical",
            measurement_source=values.source, **values.model_dump(exclude={"source"}),
        )
        database.add(measurement)
        database.flush()
        identifier = measurement.id
    submit(sessions, created.id, measurement=identifier)
    advance(sessions, created.id)
    expected = measurement_payload(values, "speaking-metrics-historical")
    assert batch(client, [created.id]).json()["summaries"][0]["finalized_points"][0]["measurement"] == expected
    assert client.get(detail_url(created.id), params={"question_index": 0}).json()["selected_question"]["attempts"][0]["measurement"] == expected


def test_owner_final_metrics_cannot_come_from_open_question_or_unrelated_session(client, sessions):
    owner, unrelated = sessions.start(), sessions.start()
    final = measured_attempt(sessions, owner.id, metrics(recognized_word_count=4))
    advance(sessions, owner.id)
    open_attempt = measured_attempt(sessions, owner.id, metrics(recognized_word_count=888), question=1)
    other_attempt = measured_attempt(sessions, unrelated.id, metrics(recognized_word_count=777))
    advance(sessions, unrelated.id)
    summary_response = batch(client, [owner.id])
    detail_response = client.get(detail_url(owner.id), params={"question_index": 0})
    assert summary_response.status_code == detail_response.status_code == 200
    summary = summary_response.json()["summaries"][0]
    assert summary["total_attempt_count"] == 2
    assert summary["measured_final_answer_count"] == 1
    assert [point["attempt_id"] for point in summary["finalized_points"]] == [str(final.id)]
    assert summary["finalized_points"][0]["measurement"]["recognized_word_count"] == 4
    assert str(open_attempt.id) not in summary_response.text
    detail = detail_response.json()
    assert [item["attempt_id"] for item in detail["selected_question"]["attempts"]] == [str(final.id)]
    assert detail["selected_question"]["attempts"][0]["measurement"]["recognized_word_count"] == 4
    assert detail["summary"]["finalized_points"] == summary["finalized_points"]
    for response in (summary_response, detail_response):
        for identifier in (unrelated.id, other_attempt.id, other_attempt.measurement_id,
                           final.measurement_id, open_attempt.measurement_id):
            assert str(identifier) not in response.text


@pytest.mark.parametrize("params", [
    {"question_index": -1}, {"question_index": "bad"},
    {"after_attempt_number": 1}, {"question_index": 0, "after_attempt_number": 0},
    {"question_index": 0, "after_attempt_number": -1},
    {"question_index": 0, "after_attempt_number": "bad"},
    {"question_index": 0, "limit": 0}, {"question_index": 0, "limit": 21},
    {"question_index": 0, "limit": "bad"},
    {"question_index": 0, "all_sessions": "true"},
])
def test_detail_rejects_invalid_page_parameters(client, sessions, params):
    created = sessions.start()
    assert client.get(detail_url(created.id), params=params).status_code == 422


def test_detail_limits_default_page_and_keeps_empty_past_cursor_valid(client, sessions):
    created = sessions.start()
    for revision in range(23):
        submit(sessions, created.id, revision=revision, answer=f"{revision + 1:02d}" + "x" * 9998)
    first = client.get(detail_url(created.id), params={"question_index": 0}).json()
    assert first["questions"][0]["attempt_count"] == 23
    assert [item["attempt_number"] for item in first["selected_question"]["attempts"]] == list(range(1, 11))
    assert all(len(item["answer_text"]) == 10000 for item in first["selected_question"]["attempts"])
    assert first["selected_question"]["attempts"][0]["answer_text"] == "01" + "x" * 9998
    assert first["selected_question"]["has_more"] is True
    assert first["selected_question"]["next_after_attempt_number"] == 10
    page = client.get(detail_url(created.id), params={
        "question_index": 0, "after_attempt_number": 10, "limit": 20,
    }).json()["selected_question"]
    assert [item["attempt_number"] for item in page["attempts"]] == list(range(11, 24))
    assert page["has_more"] is False
    assert page["next_after_attempt_number"] is None
    assert all(item["is_final"] is False for item in page["attempts"])
    assert client.get(detail_url(created.id), params={
        "question_index": 0, "after_attempt_number": 100,
    }).json()["selected_question"] == {
        "question_index": 0, "attempts": [], "has_more": False, "next_after_attempt_number": None,
    }


def test_detail_unknown_session_and_outside_snapshot_question_are_404(client, sessions):
    created = sessions.start()
    missing = client.get(detail_url(uuid4()))
    assert missing.status_code == 404
    assert missing.headers["Cache-Control"] == "no-store"
    assert client.get(detail_url(created.id), params={"question_index": 5}).status_code == 404
    assert client.get(detail_url(created.id), params={"question_index": 100}).status_code == 404
    assert client.get(detail_url("not-a-uuid")).status_code == 422


def test_history_is_read_only_and_does_not_allocate_attempts_or_measurements(
        client, sessions, postgres_session_factory, postgres_engine):
    created = sessions.start()
    measured_attempt(sessions, created.id)
    advance(sessions, created.id)
    before = rows(postgres_session_factory)
    statements = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", observe)
    try:
        for _ in range(2):
            assert batch(client, [created.id, uuid4()]).status_code == 200
            assert client.get(detail_url(created.id), params={"question_index": 0}).status_code == 200
    finally:
        event.remove(postgres_engine, "before_cursor_execute", observe)
    assert rows(postgres_session_factory) == before
    assert statements
    assert all(statement.lstrip().upper().startswith(("SELECT", "WITH", "SET", "SHOW"))
               for statement in statements)
    assert not any("FOR UPDATE" in statement.upper() for statement in statements)


@pytest.mark.parametrize("operation", ["summaries", "detail"])
def test_history_uses_read_only_repeatable_read_transactions(history, sessions, postgres_engine, operation):
    created = sessions.start()
    flags = []
    probing = False

    def observe(connection, cursor, statement, parameters, context, executemany):
        nonlocal probing
        if probing or flags or not data_statement(statement) or "interview_sessions" not in statement:
            return
        probing = True
        try:
            flags.append(tuple(connection.exec_driver_sql(
                "SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')"
            ).one()))
        finally:
            probing = False

    event.listen(postgres_engine, "before_cursor_execute", observe)
    try:
        if operation == "summaries":
            history.get_summaries([created.id])
        else:
            history.get_detail(created.id, question_index=0)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", observe)
    assert flags == [("on", "repeatable read")]


def test_query_count_is_constant_at_batch_and_page_limits_and_summaries_never_select_answers(
        history, sessions, postgres_engine):
    identifiers = [sessions.start().id for _ in range(50)]
    for identifier in identifiers:
        measured_attempt(sessions, identifier, delivery=delivery())
        if identifier != identifiers[0]:
            advance(sessions, identifier)
    for revision in range(1, 22):
        submit(sessions, identifiers[0], revision=revision, answer="PRIVATE-QUERY-COVERAGE")
    captures = []

    def capture(operation):
        statements = []

        def observe(connection, cursor, statement, parameters, context, executemany):
            if data_statement(statement):
                statements.append(statement)

        event.listen(postgres_engine, "before_cursor_execute", observe)
        try:
            operation()
        finally:
            event.remove(postgres_engine, "before_cursor_execute", observe)
        captures.append(statements)
        return statements

    small = capture(lambda: history.get_summaries(identifiers[:1]))
    full = capture(lambda: history.get_summaries(identifiers))
    assert len(small) == len(full) == 2
    assert all("answer_text" not in statement.lower() for statement in small + full)
    for statements in (small, full):
        assert "interview_sessions.user_id" in statements[0].lower()
        assert "question_attempts" not in statements[0].lower()
        assert "transcription_measurements" not in statements[0].lower()
        assert "delivery_measurement_version" in statements[1].lower()
    one = capture(lambda: history.get_detail(identifiers[0], question_index=0, limit=1))
    twenty = capture(lambda: history.get_detail(identifiers[0], question_index=0, limit=20))
    assert len(one) == len(twenty) == 3
    for statements in (one, twenty):
        assert "interview_sessions.user_id" in statements[0].lower()
        assert "question_attempts" not in statements[0].lower()
        assert "transcription_measurements" not in statements[0].lower()
        assert "answer_text" not in statements[1].lower()
        assert "answer_text" in statements[2].lower()
    assert not any("FOR UPDATE" in statement.upper() for statements in captures for statement in statements)


@pytest.mark.parametrize("operation", ["summaries", "detail"])
def test_database_read_failures_have_sanitized_http_errors(client, sessions, postgres_engine, operation):
    created = sessions.start()
    secret = "SECRET-DATABASE-URL-AND-PRIVATE-ANSWER"

    def fail(connection, cursor, statement, parameters, context, executemany):
        if data_statement(statement):
            raise SQLAlchemyError(secret)

    event.listen(postgres_engine, "before_cursor_execute", fail)
    try:
        response = batch(client, [created.id]) if operation == "summaries" else client.get(detail_url(created.id))
    finally:
        event.remove(postgres_engine, "before_cursor_execute", fail)
    assert response.status_code == 503
    assert set(response.json()) == {"detail"}
    assert isinstance(response.json()["detail"], str)
    assert secret not in response.text
    assert "SQLAlchemy" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("operation", ["summaries", "detail"])
def test_missing_final_attempt_in_inconsistent_stored_state_fails_safely(
        client, sessions, postgres_session_factory, operation):
    created = sessions.start()
    with postgres_session_factory.begin() as database:
        database.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == created.id)
                         .values(current_question_index=1))
    response = batch(client, [created.id]) if operation == "summaries" else client.get(detail_url(created.id))
    assert response.status_code == 500
    assert set(response.json()) == {"detail"}
    assert str(created.id) not in response.text
    assert "Traceback" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("configuration", ["measured", "typed", "unavailable", "historical"])
def test_history_reads_leave_existing_comparison_endpoint_unchanged(
        client, sessions, postgres_session_factory, configuration):
    created = sessions.start()
    first = measured_attempt(sessions, created.id)
    measured_attempt(sessions, created.id, metrics(recognized_word_count=7), revision=1)
    if configuration == "typed":
        latest = submit(sessions, created.id, revision=2)
    elif configuration == "unavailable":
        latest = measured_attempt(sessions, created.id, metrics(
            recognized_word_count=0, um_count=None, uh_count=None,
            filler_unavailable_reason="unsupported_language", timed_utterance_span_seconds=None,
            estimated_words_per_minute=None, timing_unavailable_reason="missing_timings",
        ), revision=2)
    elif configuration == "historical":
        values = metrics(recognized_word_count=11)
        with postgres_session_factory.begin() as database:
            measurement = TranscriptionMeasurement(
                session_id=created.id, question_index=0, measurement_version="historical-definition",
                measurement_source=values.source, **values.model_dump(exclude={"source"}),
            )
            database.add(measurement)
            database.flush()
            identifier = measurement.id
        latest = submit(sessions, created.id, revision=2, measurement=identifier)
    else:
        latest = measured_attempt(sessions, created.id, metrics(recognized_word_count=11), revision=2)
    url = f"/api/sessions/{created.id}/questions/0/comparison"
    before = client.get(url)
    assert before.status_code == 200
    assert before.json()["before_attempt"]["measurement_id"] == str(first.measurement_id)
    assert before.json()["after_attempt"]["attempt_number"] == 3
    assert before.json()["after_attempt"]["measurement_id"] == (
        str(latest.measurement_id) if latest.measurement_id is not None else None
    )
    fillers = before.json()["comparison"]["um_count"]
    assert fillers["before"] == 0
    if configuration == "measured":
        assert fillers["after"] == fillers["delta"] == 0
        assert before.json()["comparison"]["recognized_word_count"]["delta"] == 7
    elif configuration == "historical":
        assert fillers["after"] == 0
        assert fillers["delta"] is None
        assert fillers["comparison_unavailable_reason"] == "measurement_version_mismatch"
    else:
        assert fillers["after"] is fillers["delta"] is None
        assert fillers["after_unavailable_reason"] == (
            "no_measurement" if configuration == "typed" else "unsupported_language"
        )
    assert batch(client, [created.id]).status_code == 200
    assert client.get(detail_url(created.id), params={"question_index": 0}).status_code == 200
    assert client.get(url).json() == before.json()


def test_history_survives_service_and_engine_reconstruction(
        history, sessions, postgres_engine, authenticated_principal):
    created = sessions.start()
    measured_attempt(sessions, created.id, delivery=delivery())
    advance(sessions, created.id)
    expected_summary = history.get_summaries([created.id])
    expected_detail = history.get_detail(created.id, question_index=0)
    postgres_engine.dispose()
    rebuilt_engine = create_database_engine(postgres_engine.url)
    try:
        rebuilt = HistoryReadService(create_session_factory(rebuilt_engine), authenticated_principal)
        assert rebuilt.get_summaries([created.id]) == expected_summary
        assert rebuilt.get_detail(created.id, question_index=0) == expected_detail
    finally:
        rebuilt_engine.dispose()


@pytest.mark.parametrize("operation", ["summaries", "detail"])
def test_history_snapshot_stays_coherent_without_blocking_retry_and_continue(
        history, sessions, postgres_engine, operation):
    created = sessions.start()
    measured_attempt(sessions, created.id, delivery=delivery())
    advance(sessions, created.id)
    original = measured_attempt(sessions, created.id, question=1, delivery=delivery(
        pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0,
    ))
    reader_executed, release_reader, writer_finished = Event(), Event(), Event()
    results, errors = {}, {}
    reader_statements = []

    def pause_reader(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name != "history-snapshot-reader" or not data_statement(statement):
            return
        reader_statements.append(statement)
        assert "FOR UPDATE" not in statement.upper()
        if reader_executed.is_set():
            return
        # Establish the repeatable-read snapshot at the owner-filtered root
        # lookup, before the concurrent writer changes attempts and finalization.
        assert "interview_sessions.user_id" in statement.lower()
        assert "question_attempts" not in statement.lower()
        assert "transcription_measurements" not in statement.lower()
        reader_executed.set()
        if not release_reader.wait(10):
            raise AssertionError("History snapshot reader was not released")

    def read():
        try:
            result = (history.get_summaries([created.id]) if operation == "summaries"
                      else history.get_detail(created.id, question_index=1))
            results["read"] = result.model_dump(mode="json")
        except Exception as failure:
            errors["read"] = failure

    def write():
        try:
            results["retry"] = measured_attempt(sessions, created.id, metrics(recognized_word_count=77),
                                                question=1, revision=1, delivery=delivery())
            advance(sessions, created.id, 1, 2)
        except Exception as failure:
            errors["write"] = failure
        finally:
            writer_finished.set()

    reader = Thread(name="history-snapshot-reader", target=read, daemon=True)
    writer = Thread(name="history-snapshot-writer", target=write, daemon=True)
    started = []
    event.listen(postgres_engine, "after_cursor_execute", pause_reader)
    try:
        reader.start()
        started.append(reader)
        assert reader_executed.wait(5), "History reader did not execute its data read"
        writer.start()
        started.append(writer)
        assert writer_finished.wait(3), "History read blocked the same-session writer"
        assert "retry" in results
    finally:
        release_reader.set()
        for worker in started:
            worker.join(10)
        event.remove(postgres_engine, "after_cursor_execute", pause_reader)
    assert all(not worker.is_alive() for worker in started)
    assert errors == {}
    assert len(reader_statements) == (2 if operation == "summaries" else 3)
    summary = results["read"]["summaries"][0] if operation == "summaries" else results["read"]["summary"]
    assert summary["total_attempt_count"] == 2
    assert summary["finalized_question_count"] == 1
    assert summary["measured_final_answer_count"] == 1
    assert [point["question_index"] for point in summary["finalized_points"]] == [0]
    if operation == "detail":
        question = results["read"]["questions"][1]
        assert question["attempt_count"] == 1
        assert question["latest_attempt_id"] == str(original.id)
        assert question["final_attempt_id"] is None
        attempts = results["read"]["selected_question"]["attempts"]
        assert [attempt["attempt_id"] for attempt in attempts] == [str(original.id)]
        assert attempts[0]["is_final"] is False
        assert attempts[0]["measurement"]["delivery_metrics"]["pause_count"] == 0
    later = summary_payload(history, created.id)
    assert later["total_attempt_count"] == 3
    assert later["finalized_question_count"] == 2
    assert later["finalized_points"][-1]["attempt_id"] == str(results["retry"].id)
    assert later["finalized_points"][-1]["measurement"]["recognized_word_count"] == 77
    assert later["finalized_points"][-1]["measurement"]["delivery_metrics"]["pause_count"] == 2


DELIVERY_FIELDS = {"version", "source", "pause_count", "total_pause_duration_seconds",
                   "longest_pause_seconds", "unavailable_reason"}


def delivery(**changes):
    values = {"pause_count": 2, "total_pause_duration_seconds": 1.234567890123,
              "longest_pause_seconds": 0.734567890123, "unavailable_reason": None}
    values.update(changes)
    return DeliveryMetrics(**values)


def test_delivery_history_preserves_final_linkage_all_states_and_scoped_privacy(client, sessions):
    owner, unrelated = sessions.start(), sessions.start()
    superseded = measured_attempt(sessions, owner.id, delivery=delivery(), answer="SUPERSEDED-ANSWER")
    exact = delivery(pause_count=3, total_pause_duration_seconds=1.234567890789,
                     longest_pause_seconds=0.734567890789)
    final = measured_attempt(sessions, owner.id, revision=1, delivery=exact, answer="FINAL-EDITED-ANSWER")
    unlinked = sessions.create_measurement(owner.id, 0, metrics(recognized_word_count=999),
        expected_last_attempt_number=2, delivery_metrics=delivery(pause_count=99,
        total_pause_duration_seconds=99.0, longest_pause_seconds=1.0))
    advance(sessions, owner.id, revision=2)
    zero = delivery(pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0)
    zero_attempt = measured_attempt(sessions, owner.id, question=1, delivery=zero)
    advance(sessions, owner.id, 1)
    unavailable = delivery(pause_count=None, total_pause_duration_seconds=None, longest_pause_seconds=None,
                           unavailable_reason="invalid_timing_order")
    unavailable_attempt = measured_attempt(sessions, owner.id, question=2, delivery=unavailable)
    advance(sessions, owner.id, 2)
    legacy_attempt = measured_attempt(sessions, owner.id, question=3)
    advance(sessions, owner.id, 3)
    open_voice = measured_attempt(sessions, owner.id, question=4, delivery=delivery())
    typed_final = submit(sessions, owner.id, question=4, revision=1, answer="TYPED-FINAL-ANSWER")
    other = measured_attempt(sessions, unrelated.id, metrics(recognized_word_count=777),
                             delivery=delivery(), answer="PRIVATE-OTHER-ANSWER")
    advance(sessions, unrelated.id)
    response = batch(client, [owner.id])
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    summary = response.json()["summaries"][0]
    assert summary["status"] == "active"
    assert summary["total_attempt_count"] == 7
    assert summary["total_retry_count"] == 2
    assert summary["measured_final_answer_count"] == 4
    points = summary["finalized_points"]
    assert [point["attempt_id"] for point in points] == [
        str(item.id) for item in (final, zero_attempt, unavailable_attempt, legacy_attempt)
    ]
    for point, expected in zip(points, (exact, zero, unavailable, None), strict=True):
        assert point["measurement"] == measurement_payload(metrics(), delivery=expected)
        nested = point["measurement"]["delivery_metrics"]
        if nested is not None:
            assert set(nested) == DELIVERY_FIELDS
    assert points[1]["measurement"]["delivery_metrics"]["pause_count"] == 0
    assert points[2]["measurement"]["delivery_metrics"]["unavailable_reason"] == "invalid_timing_order"
    assert points[3]["measurement"]["delivery_metrics"] is None
    assert str(superseded.id) not in response.text
    assert str(open_voice.id) not in response.text
    assert str(typed_final.id) not in response.text
    assert "answer_text" not in response.text
    details = client.get(detail_url(owner.id), params={"question_index": 0, "limit": 1})
    assert details.status_code == 200
    page = details.json()["selected_question"]
    assert page["has_more"] is True
    assert page["next_after_attempt_number"] == 1
    assert page["attempts"][0]["attempt_id"] == str(superseded.id)
    assert page["attempts"][0]["is_final"] is False
    assert page["attempts"][0]["measurement"] == measurement_payload(metrics(), delivery=delivery())
    next_page = client.get(detail_url(owner.id), params={
        "question_index": 0, "after_attempt_number": 1, "limit": 1,
    })
    assert next_page.status_code == 200
    final_page = next_page.json()["selected_question"]
    assert final_page["has_more"] is False
    assert final_page["next_after_attempt_number"] is None
    assert final_page["attempts"][0]["attempt_id"] == str(final.id)
    assert final_page["attempts"][0]["is_final"] is True
    assert final_page["attempts"][0]["measurement"] == points[0]["measurement"]
    for read in (response, details, next_page):
        for identifier in (unrelated.id, other.id, other.measurement_id, unlinked,
                           superseded.measurement_id, final.measurement_id, zero_attempt.measurement_id,
                           unavailable_attempt.measurement_id, legacy_attempt.measurement_id, open_voice.measurement_id):
            assert str(identifier) not in read.text
        for private_field in ("words", "pause_events", "provider_response", "audio"):
            assert f'"{private_field}"' not in read.text
        assert "PRIVATE-OTHER-ANSWER" not in read.text
    advance(sessions, owner.id, 4, 2)
    completed = batch(client, [owner.id]).json()["summaries"][0]
    assert completed["status"] == "completed"
    assert completed["measured_final_answer_count"] == 4
    assert completed["finalized_points"][:4] == points
    assert completed["finalized_points"][4]["attempt_id"] == str(typed_final.id)
    assert completed["finalized_points"][4]["measurement"] is None
    typed_detail = client.get(detail_url(owner.id), params={"question_index": 4}).json()
    assert typed_detail["selected_question"]["attempts"][0]["measurement"]["delivery_metrics"] is not None
    assert typed_detail["selected_question"]["attempts"][1]["is_final"] is True
    assert typed_detail["selected_question"]["attempts"][1]["measurement"] is None


def test_history_preserves_independent_historical_delivery_version(client, sessions, postgres_session_factory):
    created = sessions.start()
    values, pauses = metrics(), delivery()
    with postgres_session_factory.begin() as database:
        row = TranscriptionMeasurement(
            session_id=created.id, question_index=0, measurement_version=MEASUREMENT_VERSION,
            measurement_source=values.source, **values.model_dump(exclude={"source"}),
            delivery_measurement_version="pause-metrics-historical", pause_count=pauses.pause_count,
            total_pause_duration_seconds=pauses.total_pause_duration_seconds,
            longest_pause_seconds=pauses.longest_pause_seconds, pause_unavailable_reason=None,
        )
        database.add(row)
        database.flush()
        identifier = row.id
    attempt = submit(sessions, created.id, measurement=identifier)
    advance(sessions, created.id)
    summary_response = batch(client, [created.id])
    detail_response = client.get(detail_url(created.id), params={"question_index": 0})
    assert summary_response.status_code == detail_response.status_code == 200
    summary = summary_response.json()["summaries"][0]
    measurement = summary["finalized_points"][0]["measurement"]
    assert measurement["measurement_version"] == MEASUREMENT_VERSION
    assert measurement["delivery_metrics"] == {
        **pauses.model_dump(), "version": "pause-metrics-historical", "source": values.source,
    }
    selected = detail_response.json()["selected_question"]["attempts"][0]
    assert selected["attempt_id"] == str(attempt.id)
    assert selected["measurement"] == measurement
    assert str(identifier) not in summary_response.text + detail_response.text
