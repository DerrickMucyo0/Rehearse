"""Offline history contracts: no PostgreSQL fixture or provider execution."""

from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app import auth_http, history, history_routes
from app.auth import AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind
from app.auth_http import (
    AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME,
    AuthenticatedPrincipalDependency, get_auth_session_store,
)
from app.database import DatabaseConfigurationError
from app.history import (
    FinalizedPoint, HistoryAttempt, HistoryBatchRequest, HistoryDetail,
    HistoryDetailQuery, HistoryIntegrityError, HistoryMeasurement, HistorySummaries, HistorySummaryPage,
    QuestionOverview, SelectedQuestion, SessionSummary,
)
from app.main import app
from app.sessions import SessionNotFound
from app.transcription import get_transcription_service


FIRST_ID = UUID("aed74a31-ddc3-4e0a-b2aa-b98ad52f7b61")
SECOND_ID = UUID("ba62e649-a5d9-4d9e-bb7e-22bb8e9e1377")
ATTEMPT_ID = UUID("a9e7b6d2-d1b5-44b8-b790-e887324cd17a")
MEASUREMENT_ID = UUID("dd456c40-c0ae-49da-bb31-bbb04c6bc41c")
CREATED_AT = datetime(2026, 10, 5, 11, tzinfo=timezone.utc)
SUBMITTED_AT = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
SELECTED_ANSWER = "An explicitly requested saved answer."
SUMMARY_FIELDS = {
    "session_id", "scenario_type", "status", "created_at", "completed_at", "current_question_number",
    "total_questions", "finalized_question_count", "questions_practiced_count",
    "total_attempt_count", "total_retry_count", "measured_final_answer_count",
    "last_submitted_at", "last_saved_activity_at", "finalized_points",
}
POINT_FIELDS = {
    "question_index", "attempt_id", "attempt_number", "submitted_at", "measurement",
}
OVERVIEW_FIELDS = {
    "question_index", "question_text", "finalized", "attempt_count", "latest_attempt_id",
    "latest_attempt_number", "final_attempt_id", "final_attempt_number",
}
ATTEMPT_FIELDS = {
    "attempt_id", "attempt_number", "answer_text", "submitted_at", "is_final", "measurement",
}
MEASUREMENT_FIELDS = {
    "measurement_version", "measurement_source", "recognized_word_count",
    "um_count", "uh_count", "filler_unavailable_reason",
    "timed_utterance_span_seconds", "estimated_words_per_minute",
    "timing_unavailable_reason",
    "delivery_metrics",
}
TIMING_REASONS = (
    "missing_timings", "timing_coverage_mismatch", "invalid_timing",
    "invalid_timing_order", "unusable_span",
)
PRIVATE_MARKER = "private-answer-provider-audio-marker"


def measurement_values(**changes):
    values = {
        "measurement_version": "speaking-metrics-v1",
        "measurement_source": "original_transcription",
        "recognized_word_count": 10,
        "um_count": 0,
        "uh_count": 1,
        "filler_unavailable_reason": None,
        "timed_utterance_span_seconds": 12.123456789,
        "estimated_words_per_minute": 49.491231198,
        "timing_unavailable_reason": None,
    }
    values.update(changes)
    return values


def finalized_point():
    return FinalizedPoint(
        question_index=0, attempt_id=ATTEMPT_ID, attempt_number=2,
        submitted_at=SUBMITTED_AT, measurement=HistoryMeasurement(**measurement_values()),
    )


def session_summary():
    return SessionSummary(
        session_id=FIRST_ID, scenario_type="job_interview", status="active", created_at=CREATED_AT, completed_at=None,
        current_question_number=2, total_questions=5, finalized_question_count=1,
        questions_practiced_count=1, total_attempt_count=2, total_retry_count=1,
        measured_final_answer_count=1, last_submitted_at=SUBMITTED_AT,
        last_saved_activity_at=SUBMITTED_AT, finalized_points=[finalized_point()],
    )


def question_overview(index=0):
    return QuestionOverview(
        question_index=index, question_text=f"Question {index + 1}", finalized=index == 0,
        attempt_count=2 if index == 0 else 0,
        latest_attempt_id=ATTEMPT_ID if index == 0 else None,
        latest_attempt_number=2 if index == 0 else None,
        final_attempt_id=ATTEMPT_ID if index == 0 else None,
        final_attempt_number=2 if index == 0 else None,
    )


def history_attempt():
    return HistoryAttempt(
        attempt_id=ATTEMPT_ID, attempt_number=2, answer_text=SELECTED_ANSWER,
        submitted_at=SUBMITTED_AT, is_final=True,
        measurement=HistoryMeasurement(**measurement_values()),
    )


def selected_question():
    return SelectedQuestion(
        question_index=0, attempts=[history_attempt()], has_more=False,
        next_after_attempt_number=None,
    )


def history_detail(selected=False):
    return HistoryDetail(
        summary=session_summary(), questions=[question_overview(index) for index in range(5)],
        selected_question=selected_question() if selected else None,
    )


def history_summaries():
    return HistorySummaries(summaries=[session_summary()], missing_session_ids=[SECOND_ID])


def history_summary_page():
    return HistorySummaryPage(items=[session_summary()], next_cursor=None)


class OfflineHistoryService:
    """Concrete typed read results; no engine, ORM session, or provider exists."""

    def __init__(self):
        self.calls = []
        self.error = None
        self.unrequested_answer = PRIVATE_MARKER
        self.raw_provider_payload = PRIVATE_MARKER

    def get_summaries(self, session_ids):
        self.calls.append(("summaries", session_ids))
        if self.error is not None:
            raise self.error
        return HistorySummaries(
            summaries=[session_summary()] if FIRST_ID in session_ids else [],
            missing_session_ids=[identifier for identifier in session_ids if identifier != FIRST_ID],
        )

    def get_detail(self, session_id, question_index=None, after_attempt_number=None, limit=10):
        self.calls.append(("detail", session_id, question_index, after_attempt_number, limit))
        if self.error is not None:
            raise self.error
        return history_detail(selected=question_index is not None)

    def get_discovery(self, limit=10, cursor=None):
        self.calls.append(("discovery", limit, cursor))
        if self.error is not None:
            raise self.error
        return history_summary_page()


@pytest.fixture
def offline_client(monkeypatch) -> Iterator[tuple[TestClient, OfflineHistoryService]]:
    service = OfflineHistoryService()
    expected_principal = AuthenticatedPrincipal(
        user_id=UUID("00000000-0000-4000-8000-000000000021"),
        auth_session_id=UUID("00000000-0000-4000-8000-000000000022"),
        request_context="synthetic-offline-history-context",
    )

    class Store:
        def resolve(self, *, credential):
            if credential != "synthetic-offline-history-credential":
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            return expected_principal

        def revalidate(self, *, principal):
            raise AssertionError("Read-only History has no post-provider revalidation phase.")

    def history_override(principal: AuthenticatedPrincipalDependency):
        assert principal is expected_principal
        return service

    def forbidden_factory(*args, **kwargs):
        raise AssertionError("History reads must not resolve a database or transcription factory.")

    monkeypatch.setattr(history_routes, "get_database_session_factory", forbidden_factory)
    monkeypatch.setattr(auth_http, "get_database_session_factory", forbidden_factory)
    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[history_routes.get_history_service] = history_override
    app.dependency_overrides[get_auth_session_store] = lambda: Store()
    app.dependency_overrides[get_transcription_service] = forbidden_factory
    try:
        with TestClient(app, headers={
            "Cookie": f"{AUTH_SESSION_COOKIE_NAME}=synthetic-offline-history-credential",
            AUTH_REQUEST_CONTEXT_HEADER: expected_principal.request_context,
        }) as client:
            yield client, service
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.mark.parametrize("ids", [[], [str(FIRST_ID)] * 51])
def test_batch_enforces_raw_input_size_before_deduplication(ids):
    with pytest.raises(ValidationError):
        HistoryBatchRequest(session_ids=ids)


def test_batch_accepts_fifty_raw_ids_and_canonicalizes_duplicate_uuid_order():
    request = HistoryBatchRequest(session_ids=[
        str(SECOND_ID).upper(), str(FIRST_ID), str(SECOND_ID),
        str(FIRST_ID).upper(), *([str(FIRST_ID)] * 46),
    ])
    assert request.session_ids == [SECOND_ID, FIRST_ID]
    assert request.model_dump(mode="json") == {
        "session_ids": [str(SECOND_ID), str(FIRST_ID)],
    }


@pytest.mark.parametrize("values", [
    {}, {"session_ids": None}, {"session_ids": str(FIRST_ID)},
    {"session_ids": ["not-a-uuid"]}, {"session_ids": [123]},
    {"session_ids": [str(FIRST_ID)], "answer": PRIVATE_MARKER},
    {"session_ids": [str(FIRST_ID)], "all_sessions": True},
])
def test_batch_rejects_malformed_or_extra_request_fields(values):
    with pytest.raises(ValidationError):
        HistoryBatchRequest.model_validate(values)


def test_detail_defaults_and_boundary_selectors_are_explicit():
    assert HistoryDetailQuery().model_dump() == {
        "question_index": None, "after_attempt_number": None, "limit": 10,
    }
    for limit in (1, 20):
        query = HistoryDetailQuery(
            question_index=0, after_attempt_number=1, limit=limit,
        )
        assert query.question_index == 0
        assert query.after_attempt_number == 1
        assert query.limit == limit


@pytest.mark.parametrize("values", [
    {"question_index": -1}, {"question_index": 0, "after_attempt_number": 0},
    {"question_index": 0, "after_attempt_number": -1},
    {"after_attempt_number": 1}, {"limit": 0}, {"limit": 21},
    {"question_index": "invalid"}, {"limit": "invalid"},
    {"question_index": 0, "answer": PRIVATE_MARKER},
])
def test_detail_rejects_invalid_cursor_bounds_and_extra_fields(values):
    with pytest.raises(ValidationError):
        HistoryDetailQuery.model_validate(values)


def test_measurement_is_a_closed_non_sensitive_projection_preserving_precision():
    measurement = HistoryMeasurement(**measurement_values())
    payload = measurement.model_dump(mode="json")
    assert set(payload) == MEASUREMENT_FIELDS
    assert payload["timed_utterance_span_seconds"] == 12.123456789
    assert payload["estimated_words_per_minute"] == 49.491231198
    assert payload["um_count"] == 0
    assert payload["filler_unavailable_reason"] is None
    assert not any(isinstance(value, UUID) for value in measurement.model_dump().values())
    assert str(FIRST_ID) not in measurement.model_dump_json()
    assert payload["delivery_metrics"] is None


@pytest.mark.parametrize("field", [
    "id", "measurement_id", "session_id", "attempt_id", "answer", "answer_text",
    "text", "transcript", "language", "words", "audio", "provider_response",
])
def test_measurement_rejects_identifiers_answers_and_provider_payloads(field):
    with pytest.raises(ValidationError):
        HistoryMeasurement(**measurement_values(**{field: PRIVATE_MARKER}))


@pytest.mark.parametrize("field", ["recognized_word_count", "um_count", "uh_count"])
def test_measured_zero_remains_an_available_integer(field):
    measurement = HistoryMeasurement(**measurement_values(**{field: 0}))
    assert getattr(measurement, field) == 0
    assert type(getattr(measurement, field)) is int
    assert measurement.filler_unavailable_reason is None


@pytest.mark.parametrize("field", ["recognized_word_count", "um_count", "uh_count"])
@pytest.mark.parametrize("value", [True, "1", 1.0, -1])
def test_measurement_counts_require_strict_nonnegative_integers(field, value):
    with pytest.raises(ValidationError):
        HistoryMeasurement(**measurement_values(**{field: value}))


@pytest.mark.parametrize("field", [
    "timed_utterance_span_seconds", "estimated_words_per_minute",
])
@pytest.mark.parametrize("value", [True, "1.0", 0.0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_measurement_timing_requires_finite_positive_numbers(field, value):
    with pytest.raises(ValidationError):
        HistoryMeasurement(**measurement_values(**{field: value}))


@pytest.mark.parametrize("reason", TIMING_REASONS)
def test_unavailable_values_and_existing_reasons_remain_null(reason):
    measurement = HistoryMeasurement(**measurement_values(
        um_count=None, uh_count=None, filler_unavailable_reason="unsupported_language",
        timed_utterance_span_seconds=None, estimated_words_per_minute=None,
        timing_unavailable_reason=reason,
    ))
    payload = measurement.model_dump(mode="json")
    assert payload["recognized_word_count"] == 10
    assert payload["um_count"] is None
    assert payload["uh_count"] is None
    assert payload["filler_unavailable_reason"] == "unsupported_language"
    assert payload["timed_utterance_span_seconds"] is None
    assert payload["estimated_words_per_minute"] is None
    assert payload["timing_unavailable_reason"] == reason


@pytest.mark.parametrize("changes", [
    {"measurement_version": "   "}, {"filler_unavailable_reason": "unknown"},
    {"timing_unavailable_reason": "unknown"},
])
def test_measurement_rejects_blank_version_and_unknown_reasons(changes):
    with pytest.raises(ValidationError):
        HistoryMeasurement(**measurement_values(**changes))


def test_measurement_projection_is_frozen():
    measurement = HistoryMeasurement(**measurement_values())
    with pytest.raises(ValidationError):
        measurement.um_count = 99
    assert measurement.um_count == 0


@pytest.mark.parametrize("changes", [
    {"measurement_source": "edited_answer"},
    {"um_count": None}, {"uh_count": None},
    {"filler_unavailable_reason": "unsupported_language"},
    {"um_count": None, "filler_unavailable_reason": "unsupported_language"},
    {"timed_utterance_span_seconds": None}, {"estimated_words_per_minute": None},
    {"timing_unavailable_reason": "missing_timings"},
    {"timed_utterance_span_seconds": None, "timing_unavailable_reason": "missing_timings"},
])
def test_measurement_rejects_incoherent_availability_and_unsupported_source(changes):
    with pytest.raises(ValidationError):
        HistoryMeasurement(**measurement_values(**changes))


@pytest.mark.parametrize("build", [
    finalized_point, session_summary, history_summaries, history_summary_page, question_overview,
    history_attempt, selected_question, history_detail,
])
def test_all_history_response_models_reject_extra_sensitive_fields(build):
    projection = build()
    with pytest.raises(ValidationError):
        type(projection).model_validate({
            **projection.model_dump(), "provider_response": PRIVATE_MARKER,
        })


@pytest.mark.parametrize(("build", "field", "value"), [
    (finalized_point, "attempt_number", 3),
    (session_summary, "total_attempt_count", 99),
    (history_summaries, "missing_session_ids", []),
    (history_summary_page, "next_cursor", "replacement-cursor"),
    (question_overview, "attempt_count", 99),
    (history_attempt, "answer_text", PRIVATE_MARKER),
    (selected_question, "has_more", True),
    (history_detail, "selected_question", selected_question()),
])
def test_history_response_models_are_frozen(build, field, value):
    projection = build()
    with pytest.raises(ValidationError):
        setattr(projection, field, value)


def test_overview_projection_contains_no_answers_or_measurement_identifiers():
    summary = session_summary().model_dump(mode="json")
    point = summary["finalized_points"][0]
    overview = question_overview().model_dump(mode="json")
    assert set(summary) == SUMMARY_FIELDS
    assert summary["scenario_type"] == "job_interview"
    assert set(point) == POINT_FIELDS
    assert set(point["measurement"]) == MEASUREMENT_FIELDS
    assert set(overview) == OVERVIEW_FIELDS
    for projection in (summary, overview):
        rendered = str(projection)
        assert SELECTED_ANSWER not in rendered
        assert PRIVATE_MARKER not in rendered
        assert str(MEASUREMENT_ID) not in rendered
        assert "measurement_id" not in rendered
        assert "answer_text" not in rendered
        assert "provider_response" not in rendered


@pytest.mark.parametrize("field", [
    "finalized_question_count", "questions_practiced_count", "total_attempt_count",
    "total_retry_count", "measured_final_answer_count",
])
@pytest.mark.parametrize("value", [True, "1", -1])
def test_summary_counts_are_strict_nonnegative_integers(field, value):
    with pytest.raises(ValidationError):
        SessionSummary.model_validate({**session_summary().model_dump(), field: value})


@pytest.mark.parametrize("field", [
    "created_at", "last_submitted_at", "last_saved_activity_at",
])
def test_summary_requires_timezone_aware_persisted_timestamps(field):
    with pytest.raises(ValidationError):
        SessionSummary.model_validate({
            **session_summary().model_dump(), field: datetime(2026, 10, 5, 12),
        })


def test_batch_endpoint_uses_only_canonical_named_ids_and_closed_typed_results(offline_client):
    client, service = offline_client
    response = client.post("/api/history/summaries", json={"session_ids": [
        str(SECOND_ID).upper(), str(FIRST_ID), str(SECOND_ID),
    ]})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert service.calls == [("summaries", [SECOND_ID, FIRST_ID])]
    payload = response.json()
    assert set(payload) == {"summaries", "missing_session_ids"}
    assert payload["missing_session_ids"] == [str(SECOND_ID)]
    assert len(payload["summaries"]) == 1
    assert set(payload["summaries"][0]) == SUMMARY_FIELDS
    assert payload["summaries"][0]["session_id"] == str(FIRST_ID)
    assert PRIVATE_MARKER not in response.text
    assert SELECTED_ANSWER not in response.text
    assert "measurement_id" not in response.text


def test_fifty_identical_batch_ids_are_valid_and_forwarded_once(offline_client):
    client, service = offline_client
    response = client.post("/api/history/summaries", json={
        "session_ids": [str(FIRST_ID)] * 50,
    })
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert service.calls == [("summaries", [FIRST_ID])]
    assert response.json()["missing_session_ids"] == []
    assert len(response.json()["summaries"]) == 1


@pytest.mark.parametrize("body", [
    {}, {"session_ids": []}, {"session_ids": [str(FIRST_ID)] * 51},
    {"session_ids": [PRIVATE_MARKER]}, {"session_ids": str(FIRST_ID)},
    {"session_ids": [str(FIRST_ID)], "answer": PRIVATE_MARKER},
    {"session_ids": [str(FIRST_ID)], "all_sessions": True},
])
def test_invalid_batch_requests_are_sanitized_without_invoking_reads(offline_client, body):
    client, service = offline_client
    response = client.post("/api/history/summaries", json=body)
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Invalid history request."}
    assert PRIVATE_MARKER not in response.text
    assert service.calls == []


def test_batch_rejects_query_parameters_without_invoking_reads(offline_client):
    client, service = offline_client
    response = client.post("/api/history/summaries", params={"scope": PRIVATE_MARKER}, json={
        "session_ids": [str(FIRST_ID)],
    })
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Invalid history request."}
    assert PRIVATE_MARKER not in response.text
    assert service.calls == []


def test_default_detail_has_only_overviews_and_never_exposes_unrequested_answers(offline_client):
    client, service = offline_client
    response = client.get(f"/api/sessions/{FIRST_ID}/history-detail")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert service.calls == [("detail", FIRST_ID, None, None, 10)]
    payload = response.json()
    assert set(payload) == {"summary", "questions", "selected_question"}
    assert payload["selected_question"] is None
    assert len(payload["questions"]) == 5
    assert all(set(question) == OVERVIEW_FIELDS for question in payload["questions"])
    assert SELECTED_ANSWER not in response.text
    assert PRIVATE_MARKER not in response.text
    assert "answer_text" not in response.text
    assert "measurement_id" not in response.text


def test_selected_detail_forwards_explicit_page_selector_and_only_projected_measurement(offline_client):
    client, service = offline_client
    response = client.get(f"/api/sessions/{FIRST_ID}/history-detail", params={
        "question_index": 0, "after_attempt_number": 1, "limit": 1,
    })
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert service.calls == [("detail", FIRST_ID, 0, 1, 1)]
    selected = response.json()["selected_question"]
    assert set(selected) == {"question_index", "attempts", "has_more", "next_after_attempt_number"}
    assert selected["has_more"] is False
    assert selected["next_after_attempt_number"] is None
    assert len(selected["attempts"]) == 1
    attempt = selected["attempts"][0]
    assert set(attempt) == ATTEMPT_FIELDS
    assert attempt["answer_text"] == SELECTED_ANSWER
    assert attempt["attempt_number"] == 2
    assert set(attempt["measurement"]) == MEASUREMENT_FIELDS
    assert attempt["measurement"]["um_count"] == 0
    assert attempt["measurement"]["timed_utterance_span_seconds"] == 12.123456789
    assert PRIVATE_MARKER not in response.text
    assert "measurement_id" not in response.text
    assert str(MEASUREMENT_ID) not in response.text


@pytest.mark.parametrize("params", [
    {"question_index": -1}, {"question_index": PRIVATE_MARKER},
    {"question_index": 0, "after_attempt_number": 0},
    {"question_index": 0, "after_attempt_number": PRIVATE_MARKER},
    {"after_attempt_number": 1}, {"limit": 0}, {"limit": 21},
    {"limit": PRIVATE_MARKER}, {"answer": PRIVATE_MARKER},
])
def test_invalid_detail_requests_are_sanitized_without_invoking_reads(offline_client, params):
    client, service = offline_client
    response = client.get(f"/api/sessions/{FIRST_ID}/history-detail", params=params)
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Invalid history request."}
    assert PRIVATE_MARKER not in response.text
    assert service.calls == []


def test_invalid_detail_uuid_is_sanitized_without_invoking_reads(offline_client):
    client, service = offline_client
    response = client.get(f"/api/sessions/{PRIVATE_MARKER}/history-detail")
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Invalid history request."}
    assert PRIVATE_MARKER not in response.text
    assert service.calls == []


def invalid_stored_measurement():
    try:
        HistoryMeasurement(**measurement_values(recognized_word_count=PRIVATE_MARKER))
    except ValidationError as error:
        return error
    raise AssertionError("Invalid stored measurement was accepted.")


@pytest.mark.parametrize(("error_factory", "status", "detail"), [
    (lambda: SQLAlchemyError(PRIVATE_MARKER), 503, "Session history is temporarily unavailable."),
    (lambda: DatabaseConfigurationError(PRIVATE_MARKER), 503, "Session history is temporarily unavailable."),
    (lambda: HistoryIntegrityError(PRIVATE_MARKER), 500, "Stored session history is inconsistent."),
    (invalid_stored_measurement, 500, "Stored session history is inconsistent."),
    (lambda: SessionNotFound(PRIVATE_MARKER), 404, "Session or question not found."),
    (lambda: RuntimeError(PRIVATE_MARKER), 500, "Session history is temporarily unavailable."),
    (lambda: TypeError(PRIVATE_MARKER), 500, "Session history is temporarily unavailable."),
])
@pytest.mark.parametrize("endpoint", ["summaries", "discovery", "detail"])
def test_read_errors_have_fixed_status_detail_and_no_store(
    offline_client, error_factory, status, detail, endpoint,
):
    client, service = offline_client
    service.error = error_factory()
    if endpoint == "summaries":
        response = client.post("/api/history/summaries", json={"session_ids": [str(FIRST_ID)]})
    elif endpoint == "discovery":
        response = client.get("/api/history/summaries")
    else:
        response = client.get(f"/api/sessions/{FIRST_ID}/history-detail")
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": detail}
    assert PRIVATE_MARKER not in response.text
    assert len(service.calls) == 1


@pytest.mark.parametrize("error_type", [DatabaseConfigurationError, SQLAlchemyError])
def test_configured_factory_failure_is_offline_sanitized_and_not_cached(
    offline_client, monkeypatch, error_type,
):
    client, service = offline_client
    factory_calls = []

    def unavailable_factory():
        factory_calls.append("factory")
        raise error_type(PRIVATE_MARKER)

    app.dependency_overrides.pop(history_routes.get_history_service)
    monkeypatch.setattr(history_routes, "get_database_session_factory", unavailable_factory)
    for _ in range(2):
        response = client.post("/api/history/summaries", json={"session_ids": [str(FIRST_ID)]})
        assert response.status_code == 503
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {"detail": "Session history is temporarily unavailable."}
        assert PRIVATE_MARKER not in response.text
    assert factory_calls == ["factory", "factory"]
    assert service.calls == []


@pytest.mark.parametrize("endpoint", ["summaries", "discovery", "detail"])
def test_invalid_request_needs_no_database_configuration_or_factory(offline_client, endpoint):
    client, service = offline_client
    app.dependency_overrides.pop(history_routes.get_history_service)
    if endpoint == "summaries":
        response = client.post("/api/history/summaries", json={"session_ids": []})
    elif endpoint == "discovery":
        response = client.get("/api/history/summaries", params={"limit": 0})
    else:
        response = client.get(f"/api/sessions/{FIRST_ID}/history-detail", params={
            "after_attempt_number": 1,
        })
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Invalid history request."}
    assert service.calls == []


@pytest.mark.parametrize("limit", [None, 1, 20])
def test_discovery_uses_bounded_page_contract_without_changing_session_routes(offline_client, limit):
    client, service = offline_client
    response = client.get("/api/history/summaries", params={} if limit is None else {"limit": limit})
    assert response.status_code == 200
    assert response.json() == history_summary_page().model_dump(mode="json")
    assert set(response.json()) == {"items", "next_cursor"}
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE_MARKER not in response.text and SELECTED_ANSWER not in response.text
    assert service.calls == [("discovery", 10 if limit is None else limit, None)]
    assert client.get("/api/sessions").status_code == 405


@pytest.mark.parametrize(("method", "path", "allowed"), [
    ("PUT", "/api/history/summaries", "POST"),
    ("DELETE", "/api/history/summaries", "POST"),
    ("POST", f"/api/sessions/{FIRST_ID}/history-detail", "GET"),
    ("HEAD", f"/api/sessions/{FIRST_ID}/history-detail", "GET"),
])
def test_unsupported_history_methods_preserve_allow_and_no_store(offline_client, method, path, allowed):
    client, service = offline_client
    response = client.request(method, path)
    assert response.status_code == 405
    assert response.headers["Allow"] == allowed
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE_MARKER not in response.text
    assert service.calls == []


DELIVERY_FIELDS = {"version", "source", "pause_count", "total_pause_duration_seconds",
                   "longest_pause_seconds", "unavailable_reason"}


def delivery_values(**changes):
    values = {"version": "pause-metrics-v1", "source": "original_transcription", "pause_count": 2,
              "total_pause_duration_seconds": 1.234567890123, "longest_pause_seconds": 0.734567890123,
              "unavailable_reason": None}
    values.update(changes)
    return values


@pytest.mark.parametrize("values", [
    delivery_values(),
    delivery_values(pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0),
    *(delivery_values(pause_count=None, total_pause_duration_seconds=None, longest_pause_seconds=None,
                      unavailable_reason=reason) for reason in TIMING_REASONS),
    delivery_values(version="historical-delivery"),
])
def test_history_nested_delivery_preserves_recorded_states_and_exact_scalar_values(values):
    snapshot = HistoryMeasurement(**measurement_values(delivery_metrics=values))
    payload = snapshot.model_dump(mode="json")
    assert set(payload) == MEASUREMENT_FIELDS
    assert payload["delivery_metrics"] == values
    assert set(payload["delivery_metrics"]) == DELIVERY_FIELDS
    with pytest.raises(ValidationError):
        snapshot.delivery_metrics.pause_count = 99


@pytest.mark.parametrize("changes", [
    {"pause_count": None}, {"total_pause_duration_seconds": None}, {"longest_pause_seconds": None},
    {"unavailable_reason": "invalid_timing"}, {"unavailable_reason": "unknown"},
    {"version": "\t\n"}, {"source": " "}, {"pause_count": True}, {"pause_count": -1},
    {"total_pause_duration_seconds": float("inf")}, {"longest_pause_seconds": float("nan")},
    {"pause_count": 0}, {"longest_pause_seconds": 9.0},
    {"measurement_id": str(MEASUREMENT_ID)}, {"words": []}, {"pause_events": []},
    {"provider_response": PRIVATE_MARKER}, {"audio": PRIVATE_MARKER}, {"answer_text": PRIVATE_MARKER},
])
def test_history_nested_delivery_rejects_mixed_states_and_sensitive_content(changes):
    with pytest.raises(ValidationError):
        HistoryMeasurement(**measurement_values(delivery_metrics=delivery_values(**changes)))


def persisted_projection(**changes):
    values = {
        "measurement_id": MEASUREMENT_ID, "linked_measurement_id": MEASUREMENT_ID,
        **measurement_values(), "delivery_measurement_version": None, "pause_count": None,
        "total_pause_duration_seconds": None, "longest_pause_seconds": None, "pause_unavailable_reason": None,
    }
    values.update(changes)
    return values


def test_history_selects_only_explicit_persisted_columns_and_projects_legacy_truthfully():
    names = {column.name for column in history._measurement_columns()}
    assert names == {
        "linked_measurement_id", *(MEASUREMENT_FIELDS - {"delivery_metrics"}),
        "delivery_measurement_version", "pause_count", "total_pause_duration_seconds",
        "longest_pause_seconds", "pause_unavailable_reason",
    }
    assert history._measurement(persisted_projection()).delivery_metrics is None
    assert history._measurement(persisted_projection(measurement_id=None, linked_measurement_id=None)) is None
    projected = history._measurement(persisted_projection(
        delivery_measurement_version="pause-metrics-v1", pause_count=0,
        total_pause_duration_seconds=0.0, longest_pause_seconds=0.0,
    ))
    assert projected.delivery_metrics.model_dump() == delivery_values(
        pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0,
    )


@pytest.mark.parametrize("changes", [
    {"pause_count": 0}, {"total_pause_duration_seconds": 0.0}, {"longest_pause_seconds": 0.0},
    {"pause_unavailable_reason": "missing_timings"}, {"delivery_measurement_version": "pause-metrics-v1"},
    {"delivery_measurement_version": "pause-metrics-v1", "pause_count": 0,
     "total_pause_duration_seconds": 0.0, "longest_pause_seconds": 0.0,
     "pause_unavailable_reason": "missing_timings"},
])
def test_malformed_delivery_projection_raises_sanitized_history_integrity_error(changes):
    with pytest.raises(HistoryIntegrityError) as caught:
        history._measurement(persisted_projection(**changes))
    assert str(caught.value) == ""
