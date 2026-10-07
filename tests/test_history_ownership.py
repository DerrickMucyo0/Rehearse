"""Owner-scoped History through real PostgreSQL and real HTTP authentication.

The application receives only the isolated shared factory. Neither principals
nor History services are overridden. Owned measurements enter through the
normal service, with one persisted legacy fixture. History never invokes a
provider or mutates rows.
"""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import inspect
from itertools import count
import json
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select, update

from app import auth_http, auth_routes, history_routes, session_routes
from app.auth import AuthenticatedPrincipal, IssuedAuthSession, VerifiedExternalIdentity
from app.auth_http import AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database_models import (
    AuthSession, QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement, User,
)
from app.delivery_metrics import DeliveryMetrics
from app.history import HistoryReadService
from app.main import app
from app.roleplay_composition import get_roleplay_adapter
from app.semantic_diagnosis_composition import get_semantic_diagnosis_adapter
from app.sessions import AttemptRequest, ContinueRequest, InterviewSessionService, QUESTIONS, SessionNotFound
from app.speaking_metrics import SpeakingMetrics
from app.transcription import get_transcription_service

OPERATIONS = ("discovery", "batch", "detail")
PRIVATE = "PRIVATE_HISTORY_OWNERSHIP_SENTINEL"
NOT_FOUND = {"detail": "Session or question not found."}
INVALID = {"detail": "Invalid history request."}
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
SUMMARY_FIELDS = {
    "session_id", "scenario_type", "question_engine", "status", "created_at", "completed_at", "current_question_number",
    "total_questions", "finalized_question_count", "questions_practiced_count",
    "total_attempt_count", "total_retry_count", "measured_final_answer_count",
    "last_submitted_at", "last_saved_activity_at", "finalized_points",
}


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("History ownership tests must not make provider requests.")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


def headers(login: IssuedAuthSession):
    return {
        "Cookie": f"{AUTH_SESSION_COOKIE_NAME}={login.credential}",
        AUTH_REQUEST_CONTEXT_HEADER: login.principal.request_context,
    }


def persisted(factory):
    with factory() as database:
        return {
            model.__tablename__: {
                row["id"]: dict(row)
                for row in database.execute(select(model.__table__)).mappings()
            }
            for model in (User, AuthSession, StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
        }


@contextmanager
def observed_sql(engine):
    statements = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", observe)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def child_statements(statements):
    return [(sql, parameters) for sql, parameters in statements
            if "question_attempts" in sql.lower() or "transcription_measurements" in sql.lower()]


def parameter_values(value):
    if isinstance(value, dict):
        return [item for nested in value.values() for item in parameter_values(nested)]
    if isinstance(value, (list, tuple)):
        return [item for nested in value for item in parameter_values(nested)]
    return [value]


@dataclass
class Harness:
    factory: object
    engine: object
    client: TestClient
    store: PostgreSQLAuthSessionStore
    logins: tuple[IssuedAuthSession, ...]
    identifiers: tuple[tuple[UUID, ...], tuple[UUID, ...]]
    legacy: UUID
    resolved: list
    provider_calls: list

    def sessions(self, actor=0):
        return InterviewSessionService(self.factory, self.logins[actor].principal)

    def history(self, actor=0):
        return HistoryReadService(self.factory, self.logins[actor].principal)

    def request(self, operation, *, actor=0, identifier=None, identifiers=None, auth_headers=None, params=None):
        supplied = headers(self.logins[actor]) if auth_headers is None else auth_headers
        if operation == "discovery":
            return self.client.get("/api/history/summaries", headers=supplied, params=params)
        if operation == "batch":
            requested = self.identifiers[actor] if identifiers is None else identifiers
            return self.client.post("/api/history/summaries", headers=supplied, params=params,
                                    json={"session_ids": [str(item) for item in requested]})
        identifier = self.identifiers[actor][0] if identifier is None else identifier
        return self.client.get(f"/api/sessions/{identifier}/history-detail", headers=supplied, params=params)


@pytest.fixture
def harness(postgres_session_factory, postgres_engine, monkeypatch):
    credentials, contexts = count(), count()
    store = PostgreSQLAuthSessionStore(
        postgres_session_factory, session_lifetime=timedelta(hours=1),
        credential_generator=lambda: f"HISTORY_TOKEN_{next(credentials)}_" + "t" * 48,
        request_context_generator=lambda: f"HISTORY_CONTEXT_{next(contexts)}_" + "c" * 48,
    )
    logins = tuple(store.create(user_id=store.provision_user(identity=VerifiedExternalIdentity(
        issuer="https://history-ownership.example.test", subject=subject,
    ))) for subject in ("user-a", "user-b", "empty-owner"))
    identifiers = tuple(tuple(InterviewSessionService(postgres_session_factory, logins[actor].principal).start().id
                              for _ in range(size)) for actor, size in ((0, 3), (1, 2)))
    legacy = uuid4()
    with postgres_session_factory.begin() as database:
        for position, identifier in enumerate((*identifiers[0], *identifiers[1])):
            database.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == identifier)
                             .values(created_at=BASE_TIME + timedelta(days=position)))
        database.execute(insert(StoredInterviewSession).values(
            id=legacy, user_id=None, questions=tuple(f"{PRIVATE}-legacy-{index}" for index in range(5)),
            current_question_index=5, status="completed", created_at=BASE_TIME,
            completed_at=BASE_TIME + timedelta(days=6),
        ))
        measurement = TranscriptionMeasurement(
            session_id=legacy, question_index=0, measurement_version="speaking-metrics-v1",
            measurement_source="original_transcription", recognized_word_count=314159,
            um_count=0, uh_count=0, filler_unavailable_reason=None,
            timed_utterance_span_seconds=1.0, estimated_words_per_minute=60.0,
            timing_unavailable_reason=None,
        )
        database.add(measurement)
        database.flush()
        database.execute(insert(QuestionAttempt), [{
            "id": uuid4(), "session_id": legacy, "question_index": index,
            "attempt_number": 1, "answer_text": f"{PRIVATE}-legacy-answer-{index}",
            "submitted_at": BASE_TIME + timedelta(days=1, seconds=index),
            "measurement_id": measurement.id if index == 0 else None,
        } for index in range(5)])

    monkeypatch.setattr(history_routes, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.setattr(session_routes, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.setattr(auth_http, "get_database_session_factory", lambda: postgres_session_factory)
    for dependency in (history_routes.get_history_service, auth_http.get_auth_session_store,
                       auth_http.require_authenticated_principal):
        monkeypatch.delitem(app.dependency_overrides, dependency, raising=False)
    provider_calls = []

    def forbidden_provider():
        provider_calls.append("provider dependency")
        raise AssertionError("History must not construct providers.")

    monkeypatch.setitem(app.dependency_overrides, get_transcription_service, forbidden_provider)
    monkeypatch.setitem(app.dependency_overrides, get_semantic_diagnosis_adapter, forbidden_provider)
    monkeypatch.setitem(app.dependency_overrides, get_roleplay_adapter, forbidden_provider)
    resolved = []
    original_resolve = PostgreSQLAuthSessionStore.resolve

    def recorded_resolve(self, *, credential):
        resolved.append((credential, self))
        return original_resolve(self, credential=credential)

    def forbidden_revalidate(self, *, principal):
        raise AssertionError("History must not invent post-provider revalidation.")

    monkeypatch.setattr(PostgreSQLAuthSessionStore, "resolve", recorded_resolve)
    monkeypatch.setattr(PostgreSQLAuthSessionStore, "revalidate", forbidden_revalidate)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield Harness(postgres_session_factory, postgres_engine, client, store, logins,
                      identifiers, legacy, resolved, provider_calls)
    assert provider_calls == []


def metrics(**changes):
    values = {
        "recognized_word_count": 4, "um_count": 0, "uh_count": 1,
        "filler_unavailable_reason": None, "timed_utterance_span_seconds": 1.234567890123,
        "estimated_words_per_minute": 194.40000174967392, "timing_unavailable_reason": None,
    }
    values.update(changes)
    return SpeakingMetrics(**values)


def zero_delivery():
    return DeliveryMetrics(pause_count=0, total_pause_duration_seconds=0.0,
                           longest_pause_seconds=0.0, unavailable_reason=None)


def submit(service, identifier, *, question=0, revision=0, answer="Answer", measurement=None):
    return service.submit_attempt(identifier, question, AttemptRequest(
        expected_last_attempt_number=revision, answer=answer, measurement_id=measurement,
    )).attempt


def measured(service, identifier, *, question=0, revision=0, answer="Answer", values=None, delivery=None):
    measurement = service.create_measurement(identifier, question, values or metrics(),
                                             expected_last_attempt_number=revision, delivery_metrics=delivery)
    return submit(service, identifier, question=question, revision=revision,
                  answer=answer, measurement=measurement)


def advance(service, identifier, question=0, revision=1):
    return service.continue_question(identifier, question, ContinueRequest(expected_last_attempt_number=revision))


def rich_active(service, identifier, prefix):
    first = submit(service, identifier, answer=f"{prefix}-first")
    final = measured(service, identifier, revision=1, answer=f"{prefix}-final")
    advance(service, identifier, revision=2)
    current_measured = measured(service, identifier, question=1, answer=f"{prefix}-current-measured",
                                values=metrics(recognized_word_count=777))
    current = submit(service, identifier, question=1, revision=1, answer=f"{prefix}-current-typed")
    return (first, final, current_measured, current)


def rich_completed(service, identifier, prefix):
    superseded = measured(service, identifier, answer=f"{prefix}-superseded",
                          values=metrics(recognized_word_count=999))
    final_zero = submit(service, identifier, revision=1, answer=f"{prefix}-typed-final")
    advance(service, identifier, revision=2)
    final_one = measured(service, identifier, question=1, answer=f"{prefix}-one")
    advance(service, identifier, 1)
    unavailable = metrics(recognized_word_count=0, um_count=None, uh_count=None,
                          filler_unavailable_reason="unsupported_language", timed_utterance_span_seconds=None,
                          estimated_words_per_minute=None, timing_unavailable_reason="missing_timings")
    final_two = measured(service, identifier, question=2, answer=f"{prefix}-two", values=unavailable)
    advance(service, identifier, 2)
    final_three = submit(service, identifier, question=3, answer=f"{prefix}-three")
    advance(service, identifier, 3)
    final_four = measured(service, identifier, question=4, answer=f"{prefix}-four", delivery=zero_delivery())
    advance(service, identifier, 4)
    return (superseded, final_zero, final_one, final_two, final_three, final_four)


@pytest.fixture
def rich_history(harness):
    facts = {}
    for actor, prefix in ((0, "OWNER_A"), (1, PRIVATE + "_OWNER_B")):
        service = harness.sessions(actor)
        facts[(actor, "active")] = rich_active(service, harness.identifiers[actor][0], prefix)
        facts[(actor, "completed")] = rich_completed(service, harness.identifiers[actor][1], prefix)
    return harness, facts


def assert_no_auth_metadata(response, harness):
    for login in harness.logins:
        for value in (str(login.principal.user_id), str(login.principal.auth_session_id),
                      login.principal.request_context, login.credential):
            assert value not in response.text
    assert "https://history-ownership.example.test" not in response.text
    assert "provider_subject" not in response.text
    assert "user_id" not in response.text


def assert_fixed(response, status, detail):
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure,status,detail", [
    ("missing", 401, "Authentication required."),
    ("invalid", 401, "Authentication required."),
    ("revoked", 401, "Authentication required."),
    ("expired", 401, "Authentication required."),
    ("missing-context", 403, "Invalid authentication request context."),
    ("wrong-context", 403, "Invalid authentication request context."),
    ("unavailable", 503, "Authentication is temporarily unavailable."),
])
def test_all_history_routes_use_closed_real_authentication_failures(harness, monkeypatch, operation, failure, status, detail):
    supplied = headers(harness.logins[0])
    if failure == "missing":
        supplied = {}
    elif failure == "invalid":
        supplied["Cookie"] = f"{AUTH_SESSION_COOKIE_NAME}={PRIVATE}"
    elif failure == "revoked":
        harness.store.revoke(auth_session_id=harness.logins[0].principal.auth_session_id)
    elif failure == "expired":
        with harness.factory.begin() as database:
            database.execute(update(AuthSession).where(AuthSession.id == harness.logins[0].principal.auth_session_id)
                             .values(created_at=datetime.now(timezone.utc) - timedelta(days=2),
                                     expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
    elif failure == "missing-context":
        supplied.pop(AUTH_REQUEST_CONTEXT_HEADER)
    elif failure == "wrong-context":
        supplied[AUTH_REQUEST_CONTEXT_HEADER] = PRIVATE
    else:
        def unavailable():
            raise RuntimeError(PRIVATE)
        monkeypatch.setattr(auth_http, "get_database_session_factory", unavailable)
    before = persisted(harness.factory)
    with observed_sql(harness.engine) as statements:
        response = harness.request(operation, auth_headers=supplied)
    assert_fixed(response, status, detail)
    assert_no_auth_metadata(response, harness)
    assert not any("interview_sessions" in sql.lower() for sql, _ in statements)
    assert child_statements(statements) == []
    assert persisted(harness.factory) == before
    assert harness.provider_calls == []


@pytest.mark.parametrize("actor", [0, 1], ids=["user-a", "user-b"])
def test_discovery_and_batch_preserve_exact_owner_history_facts(rich_history, actor):
    harness, facts = rich_history
    before = persisted(harness.factory)
    discovery = harness.request("discovery", actor=actor)
    batch = harness.request("batch", actor=actor)
    assert discovery.status_code == batch.status_code == 200
    page = discovery.json()
    assert set(page) == {"items", "next_cursor"}
    assert page["next_cursor"] is None
    expected_ids = sorted(harness.identifiers[actor],
                          key=lambda identifier: (before["interview_sessions"][identifier]["created_at"], identifier.int),
                          reverse=True)
    assert [item["session_id"] for item in page["items"]] == [str(identifier) for identifier in expected_ids]
    assert {item["session_id"]: item for item in page["items"]} == {
        item["session_id"]: item for item in batch.json()["summaries"]
    }
    assert batch.json()["missing_session_ids"] == []
    summaries = {item["session_id"]: item for item in page["items"]}
    active = summaries[str(harness.identifiers[actor][0])]
    complete = summaries[str(harness.identifiers[actor][1])]
    assert all(set(item) == SUMMARY_FIELDS for item in page["items"])
    assert all(item["scenario_type"] == "job_interview" for item in page["items"])
    assert all(item["question_engine"] == "deterministic-v1" for item in page["items"])
    assert (active["status"], active["current_question_number"], active["completed_at"]) == ("active", 2, None)
    assert (active["finalized_question_count"], active["questions_practiced_count"], active["total_attempt_count"],
            active["total_retry_count"], active["measured_final_answer_count"]) == (1, 2, 4, 2, 1)
    final = facts[(actor, "active")][1]
    assert active["finalized_points"][0]["attempt_id"] == str(final.id)
    assert active["finalized_points"][0]["attempt_number"] == 2
    assert active["finalized_points"][0]["measurement"]["recognized_word_count"] == 4
    assert datetime.fromisoformat(active["last_submitted_at"]) == facts[(actor, "active")][-1].submitted_at
    assert active["last_saved_activity_at"] == active["last_submitted_at"]
    assert (complete["status"], complete["current_question_number"]) == ("completed", None)
    assert (complete["finalized_question_count"], complete["questions_practiced_count"], complete["total_attempt_count"],
            complete["total_retry_count"], complete["measured_final_answer_count"]) == (5, 5, 6, 1, 3)
    assert complete["last_saved_activity_at"] == complete["completed_at"]
    points = complete["finalized_points"]
    assert [point["attempt_id"] for point in points] == [str(attempt.id) for attempt in facts[(actor, "completed")][1:]]
    assert points[0]["measurement"] is points[3]["measurement"] is None
    assert points[1]["measurement"]["timed_utterance_span_seconds"] == 1.234567890123
    assert points[1]["measurement"]["estimated_words_per_minute"] == 194.40000174967392
    assert points[2]["measurement"]["recognized_word_count"] == 0
    assert points[2]["measurement"]["um_count"] is None
    assert points[2]["measurement"]["timing_unavailable_reason"] == "missing_timings"
    assert points[4]["measurement"]["delivery_metrics"]["pause_count"] == 0
    assert points[4]["measurement"]["delivery_metrics"]["total_pause_duration_seconds"] == 0.0
    assert all(point["measurement"] is None or point["measurement"]["measurement_source"] == "original_transcription"
               for point in points)
    for response in (discovery, batch):
        assert response.headers["Cache-Control"] == "no-store"
        assert_no_auth_metadata(response, harness)
        assert "answer_text" not in response.text
        for foreign in (*harness.identifiers[1 - actor], harness.legacy):
            assert str(foreign) not in response.text
    assert persisted(harness.factory) == before
    assert harness.provider_calls == []


@pytest.mark.parametrize("actor", [0, 1], ids=["user-a", "user-b"])
def test_owner_detail_retains_finalization_snapshot_and_attempt_pagination(rich_history, actor):
    harness, facts = rich_history
    identifier = harness.identifiers[actor][0]
    before = persisted(harness.factory)
    first = harness.request("detail", actor=actor, params={"question_index": 0, "limit": 1})
    second = harness.request("detail", actor=actor, params={"question_index": 0, "limit": 1, "after_attempt_number": 1})
    current = harness.request("detail", actor=actor, params={"question_index": 1})
    assert first.status_code == second.status_code == current.status_code == 200
    first_page, second_page = first.json()["selected_question"], second.json()["selected_question"]
    assert (first_page["has_more"], first_page["next_after_attempt_number"]) == (True, 1)
    assert (second_page["has_more"], second_page["next_after_attempt_number"]) == (False, None)
    assert first_page["attempts"][0]["attempt_id"] == str(facts[(actor, "active")][0].id)
    assert first_page["attempts"][0]["is_final"] is False
    assert second_page["attempts"][0]["attempt_id"] == str(facts[(actor, "active")][1].id)
    assert second_page["attempts"][0]["is_final"] is True
    assert second_page["attempts"][0]["measurement"] == first.json()["summary"]["finalized_points"][0]["measurement"]
    assert [item["question_text"] for item in first.json()["questions"]] == list(QUESTIONS)
    assert first.json()["questions"][0]["final_attempt_number"] == 2
    assert current.json()["questions"][1]["final_attempt_id"] is None
    assert all(attempt["is_final"] is False for attempt in current.json()["selected_question"]["attempts"])
    assert current.json()["selected_question"]["attempts"][0]["measurement"]["recognized_word_count"] == 777
    for response in (first, second, current):
        assert response.headers["Cache-Control"] == "no-store"
        assert_no_auth_metadata(response, harness)
        assert str(harness.legacy) not in response.text
        for foreign in harness.identifiers[1 - actor]:
            assert str(foreign) not in response.text
    assert persisted(harness.factory) == before
    assert harness.provider_calls == []


@pytest.mark.parametrize("actor", [0, 1], ids=["user-a", "user-b"])
def test_foreign_nonexistent_and_legacy_detail_are_identical_before_child_queries(rich_history, actor):
    harness, _ = rich_history
    before = persisted(harness.factory)
    responses = []
    for identifier in (*harness.identifiers[1 - actor], uuid4(), harness.legacy):
        with observed_sql(harness.engine) as statements:
            response = harness.request("detail", actor=actor, identifier=identifier,
                                       params={"question_index": 4, "after_attempt_number": 71})
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        assert response.headers["Cache-Control"] == "no-store"
        assert child_statements(statements) == []
        roots = [sql for sql, _ in statements if "interview_sessions" in sql.lower()]
        assert roots
        assert all("user_id" in sql.lower() and "where" in sql.lower() for sql in roots)
        assert_no_auth_metadata(response, harness)
        assert PRIVATE not in response.text
        assert "completed" not in response.text
        assert "measurement" not in response.text
        responses.append(response)
    assert len({(response.status_code, response.content) for response in responses}) == 1
    assert persisted(harness.factory) == before
    assert harness.provider_calls == []


def test_post_mixed_ids_filter_aggregates_and_preserve_existing_activity_sort_and_missing_order(rich_history):
    harness, _ = rich_history
    active, complete, empty = harness.identifiers[0]
    foreign = harness.identifiers[1][1]
    missing = uuid4()
    requested = [empty, foreign, active, missing, harness.legacy, complete, foreign, missing]
    before = persisted(harness.factory)
    with observed_sql(harness.engine) as statements:
        response = harness.request("batch", identifiers=requested)
    assert response.status_code == 200
    payload = response.json()
    assert payload["missing_session_ids"] == [str(foreign), str(missing), str(harness.legacy)]
    by_id = {item["session_id"]: item for item in payload["summaries"]}
    expected = sorted((active, complete, empty), key=lambda identifier: identifier.int)
    expected.sort(key=lambda identifier: datetime.fromisoformat(by_id[str(identifier)]["last_saved_activity_at"]), reverse=True)
    assert [item["session_id"] for item in payload["summaries"]] == [str(identifier) for identifier in expected]
    assert set(by_id) == {str(identifier) for identifier in harness.identifiers[0]}
    aggregate_queries = child_statements(statements)
    assert aggregate_queries
    for _, parameters in aggregate_queries:
        values = parameter_values(parameters)
        assert foreign not in values and missing not in values and harness.legacy not in values
    for value in (PRIVATE, "314159", "answer_text", "OWNER_B"):
        assert value not in response.text
    assert persisted(harness.factory) == before


def test_post_all_unavailable_ids_have_same_representation_and_skip_all_aggregates(harness):
    foreign, missing, legacy = harness.identifiers[1][0], uuid4(), harness.legacy
    requested = [missing, legacy, foreign, missing]
    with observed_sql(harness.engine) as statements:
        response = harness.request("batch", identifiers=requested)
    assert response.status_code == 200
    assert response.json() == {"summaries": [], "missing_session_ids": [str(missing), str(legacy), str(foreign)]}
    assert child_statements(statements) == []
    assert response.headers["Cache-Control"] == "no-store"


def add_empty_sessions(harness, actor, count_to_add, timestamp):
    identifiers = tuple(uuid4() for _ in range(count_to_add))
    with harness.factory.begin() as database:
        database.execute(insert(StoredInterviewSession), [{
            "id": identifier, "user_id": harness.logins[actor].principal.user_id,
            "questions": QUESTIONS, "current_question_index": 0, "status": "active",
            "created_at": timestamp, "completed_at": None,
        } for identifier in identifiers])
    return identifiers


def discovery_ids(page):
    return [UUID(item["session_id"]) for item in page["items"]]


def test_discovery_paginates_static_dataset_created_desc_uuid_desc_without_duplicates_or_omissions(harness):
    # Equal timestamps cross several page boundaries; UUID DESC is the tie break.
    tied = BASE_TIME + timedelta(days=20)
    additional = add_empty_sessions(harness, 0, 20, tied)
    add_empty_sessions(harness, 1, 4, tied)
    before = persisted(harness.factory)
    expected = sorted((*harness.identifiers[0], *additional), key=lambda identifier: (
        before["interview_sessions"][identifier]["created_at"], identifier.int,
    ), reverse=True)
    seen, cursors, cursor = [], set(), None
    while True:
        params = {"limit": 4}
        if cursor is not None:
            params["cursor"] = cursor
        response = harness.request("discovery", params=params)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        page = response.json()
        page_ids = discovery_ids(page)
        assert page_ids == expected[len(seen):len(seen) + 4]
        assert len(page_ids) == min(4, len(expected) - len(seen))
        seen.extend(page_ids)
        assert_no_auth_metadata(response, harness)
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert type(cursor) is str and cursor and cursor not in cursors
        cursors.add(cursor)
        assert len(seen) < len(expected)
    assert seen == expected
    assert len(set(seen)) == len(expected) == 23
    assert persisted(harness.factory) == before
    assert harness.provider_calls == []


def test_discovery_default_and_limit_boundaries_and_empty_owner(harness):
    add_empty_sessions(harness, 0, 20, BASE_TIME + timedelta(days=10))
    for params, expected_size in ((None, 10), ({"limit": 1}, 1), ({"limit": 20}, 20)):
        response = harness.request("discovery", params=params)
        assert response.status_code == 200
        assert len(response.json()["items"]) == expected_size
        assert response.json()["next_cursor"] is not None
    response = harness.request("discovery", actor=2)
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    assert harness.history(2).get_discovery().model_dump(mode="json") == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": -1}, {"limit": 21}, {"limit": "1.5"}, {"limit": "true"},
    {"limit": ""}, {"user_id": str(uuid4())}, {"session_ids": str(uuid4())}, {"all_sessions": "true"},
])
def test_discovery_rejects_limit_and_membership_claims_without_history_queries(harness, params):
    with observed_sql(harness.engine) as statements:
        response = harness.request("discovery", params=params)
    assert_fixed(response, 422, INVALID["detail"])
    assert not any("interview_sessions" in sql.lower() for sql, _ in statements)
    assert child_statements(statements) == []


@pytest.mark.parametrize("cursor", [
    "", "not-a-cursor", PRIVATE, "%%%%", "a" * 4096,
    base64.urlsafe_b64encode(b"null").decode().rstrip("="),
    base64.urlsafe_b64encode(b"{}").decode().rstrip("="),
    base64.urlsafe_b64encode(b'{"created_at":"2026-01-01","id":"not-a-uuid"}').decode().rstrip("="),
    base64.urlsafe_b64encode(b'["2026-01-01T00:00:00","00000000-0000-0000-0000-000000000001"]').decode().rstrip("="),
    base64.urlsafe_b64encode(b'["2026-01-01T00:00:00+00:00","not-a-uuid"]').decode().rstrip("="),
])
def test_malformed_discovery_cursor_is_sanitized_and_never_reaches_history_data(harness, cursor):
    before = persisted(harness.factory)
    with observed_sql(harness.engine) as statements:
        response = harness.request("discovery", params={"cursor": cursor})
    assert_fixed(response, 422, INVALID["detail"])
    assert not any("interview_sessions" in sql.lower() for sql, _ in statements)
    assert child_statements(statements) == []
    assert persisted(harness.factory) == before


def test_cursor_contains_only_ordering_facts_and_never_authorizes_another_owner(harness):
    tied = BASE_TIME + timedelta(days=20)
    add_empty_sessions(harness, 0, 4, tied)
    b_new = add_empty_sessions(harness, 1, 4, tied)
    before = persisted(harness.factory)
    first = harness.request("discovery", params={"limit": 1}).json()
    cursor = first["next_cursor"]
    assert type(cursor) is str and cursor
    decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
    cursor_values = json.loads(decoded)
    # Public cursor facts are only timestamp and UUID, never identity or claims.
    assert type(cursor_values) is list and len(cursor_values) == 2
    assert datetime.fromisoformat(cursor_values[0]).utcoffset() == timedelta(0)
    assert cursor_values[1] == first["items"][-1]["session_id"]
    for login in harness.logins:
        assert str(login.principal.user_id) not in decoded
        assert str(login.principal.auth_session_id) not in decoded
        assert login.principal.request_context not in decoded
        assert login.credential not in decoded
    response = harness.request("discovery", actor=1, params={"limit": 20, "cursor": cursor})
    assert response.status_code == 200
    boundary = before["interview_sessions"][UUID(first["items"][-1]["session_id"])]
    expected = sorted((*harness.identifiers[1], *b_new), key=lambda identifier: (
        before["interview_sessions"][identifier]["created_at"], identifier.int,
    ), reverse=True)
    expected = [identifier for identifier in expected if (
        before["interview_sessions"][identifier]["created_at"], identifier.int,
    ) < (boundary["created_at"], boundary["id"].int)]
    assert discovery_ids(response.json()) == expected
    assert response.json()["next_cursor"] is None
    for identifier in (*harness.identifiers[0], harness.legacy):
        assert str(identifier) not in response.text
    assert harness.request("discovery", actor=2, params={"cursor": cursor}).json() == {"items": [], "next_cursor": None}
    assert persisted(harness.factory) == before


def test_post_activity_uuid_ascending_ties_remain_distinct_from_discovery_uuid_desc(harness):
    tied = BASE_TIME + timedelta(days=25)
    identifiers = add_empty_sessions(harness, 0, 3, tied)
    batch = harness.request("batch", identifiers=list(reversed(identifiers)))
    discovery = harness.request("discovery", params={"limit": 3})
    assert batch.status_code == discovery.status_code == 200
    assert [UUID(item["session_id"]) for item in batch.json()["summaries"]] == sorted(identifiers, key=lambda item: item.int)
    assert discovery_ids(discovery.json()) == sorted(identifiers, key=lambda item: item.int, reverse=True)


def test_owned_sparse_history_keeps_count_final_number_and_last_submitted_distinct(harness):
    identifier = harness.sessions().start().id
    first, final = uuid4(), uuid4()
    earlier, later = BASE_TIME + timedelta(days=1), BASE_TIME + timedelta(days=2)
    with harness.factory.begin() as database:
        database.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == identifier)
                         .values(created_at=BASE_TIME))
        database.execute(insert(QuestionAttempt), [
            {"id": first, "session_id": identifier, "question_index": 0, "attempt_number": 1,
             "answer_text": "Historical first", "submitted_at": later},
            {"id": final, "session_id": identifier, "question_index": 0, "attempt_number": 3,
             "answer_text": "Historical final", "submitted_at": earlier},
        ])
    advance(harness.sessions(), identifier, revision=3)
    before = persisted(harness.factory)
    batch = harness.request("batch", identifiers=[identifier])
    summary = batch.json()["summaries"][0]
    assert (summary["total_attempt_count"], summary["total_retry_count"]) == (2, 1)
    assert datetime.fromisoformat(summary["last_submitted_at"]) == later
    assert summary["finalized_points"][0]["attempt_id"] == str(final)
    assert summary["finalized_points"][0]["attempt_number"] == 3
    assert datetime.fromisoformat(summary["finalized_points"][0]["submitted_at"]) == earlier
    first_page = harness.request("detail", identifier=identifier,
                                 params={"question_index": 0, "limit": 1}).json()["selected_question"]
    final_page = harness.request("detail", identifier=identifier, params={
        "question_index": 0, "limit": 1, "after_attempt_number": 1,
    }).json()["selected_question"]
    assert [attempt["attempt_number"] for attempt in first_page["attempts"]] == [1]
    assert first_page["next_after_attempt_number"] == 1
    assert [attempt["attempt_number"] for attempt in final_page["attempts"]] == [3]
    assert final_page["attempts"][0]["is_final"] is True
    assert final_page["has_more"] is False and final_page["next_after_attempt_number"] is None
    assert persisted(harness.factory) == before


@pytest.mark.parametrize("operation", OPERATIONS)
def test_every_history_request_resolves_live_login_and_cannot_reuse_cached_principal(harness, operation):
    first_a = harness.request(operation)
    first_b = harness.request(operation, actor=1)
    assert first_a.status_code == first_b.status_code == 200
    harness.store.revoke(auth_session_id=harness.logins[0].principal.auth_session_id)
    rejected = harness.request(operation)
    assert_fixed(rejected, 401, "Authentication required.")
    second_b = harness.request(operation, actor=1)
    assert second_b.status_code == 200
    assert second_b.json() == first_b.json()
    assert [credential for credential, _ in harness.resolved] == [
        harness.logins[0].credential, harness.logins[1].credential,
        harness.logins[0].credential, harness.logins[1].credential,
    ]
    assert len({id(store) for _, store in harness.resolved}) == 4
    assert harness.provider_calls == []


def test_history_dependency_constructs_new_owner_bound_service_per_request(harness, monkeypatch):
    constructed = []
    actual = history_routes.HistoryReadService

    class RecordedHistoryService(actual):
        def __init__(self, factory, principal):
            constructed.append((self, principal))
            super().__init__(factory, principal)

    monkeypatch.setattr(history_routes, "HistoryReadService", RecordedHistoryService)
    for actor in (0, 1, 0):
        assert harness.request("discovery", actor=actor).status_code == 200
    assert len({id(service) for service, _ in constructed}) == 3
    assert [principal for _, principal in constructed] == [
        harness.logins[actor].principal for actor in (0, 1, 0)
    ]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_direct_history_reads_use_owned_roots_short_read_only_scopes_and_no_locks(harness, operation):
    states = []

    def observe_state(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SET TRANSACTION"):
            states.append(connection.exec_driver_sql(
                "SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')"
            ).one())

    event.listen(harness.engine, "after_cursor_execute", observe_state)
    before = persisted(harness.factory)
    try:
        with observed_sql(harness.engine) as statements:
            service = harness.history()
            if operation == "discovery":
                service.get_discovery(limit=1)
            elif operation == "batch":
                service.get_summaries([harness.identifiers[0][0], harness.identifiers[1][0], harness.legacy])
            else:
                service.get_detail(harness.identifiers[0][0], question_index=0)
    finally:
        event.remove(harness.engine, "after_cursor_execute", observe_state)
    assert states == [("on", "repeatable read")]
    assert harness.engine.pool.checkedout() == 0
    assert all("FOR UPDATE" not in sql.upper() for sql, _ in statements)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE", "DROP"))
                   for sql, _ in statements)
    data = [(sql, parameters) for sql, parameters in statements if "interview_sessions" in sql.lower()]
    assert data
    assert "user_id" in data[0][0].lower()
    assert harness.logins[0].principal.user_id in parameter_values(data[0][1])
    assert persisted(harness.factory) == before
    assert harness.provider_calls == []


def test_direct_history_ownership_is_mandatory_and_foreign_children_are_never_read(harness):
    parameter = inspect.signature(HistoryReadService).parameters["principal"]
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        HistoryReadService(harness.factory)
    class DerivedPrincipal(AuthenticatedPrincipal):
        pass

    source = harness.logins[0].principal
    derived = DerivedPrincipal(user_id=source.user_id, auth_session_id=source.auth_session_id,
                               request_context=source.request_context)
    for invalid in (None, source.user_id, object(), derived):
        with pytest.raises(TypeError):
            HistoryReadService(harness.factory, invalid)
    with observed_sql(harness.engine) as statements:
        with pytest.raises(SessionNotFound):
            harness.history().get_detail(harness.identifiers[1][0], question_index=0)
    assert child_statements(statements) == []
    assert harness.history().get_summaries([harness.identifiers[1][0], harness.legacy]).model_dump(mode="json") == {
        "summaries": [], "missing_session_ids": [str(harness.identifiers[1][0]), str(harness.legacy)],
    }


def test_all_persisted_routes_require_authentication_and_health_remains_public(monkeypatch):
    def dependencies(dependant):
        result = set()
        for dependency in dependant.dependencies:
            result.add(dependency.call)
            result.update(dependencies(dependency))
        return result

    expected_data = {
        ("/api/sessions", "POST"), ("/api/sessions/{session_id}", "GET"),
        ("/api/sessions/{session_id}/questions/{question_index}/attempts", "POST"),
        ("/api/sessions/{session_id}/questions/{question_index}/attempts", "GET"),
        ("/api/sessions/{session_id}/questions/{question_index}/continue", "POST"),
        ("/api/sessions/{session_id}/questions/{question_index}/comparison", "GET"),
        ("/api/sessions/{session_id}/questions/{question_index}/attempts/{attempt_number}/diagnosis", "POST"),
        ("/api/sessions/{session_id}/audio", "POST"),
        ("/api/sessions/{session_id}/transcriptions", "POST"),
        ("/api/history/summaries", "GET"), ("/api/history/summaries", "POST"),
        ("/api/sessions/{session_id}/history-detail", "GET"),
    }
    public_lifecycle = {("/api/auth/login", "GET"), ("/api/auth/callback", "GET")}
    bootstrap = ("/api/auth/me", "GET")
    logout = ("/api/auth/logout", "POST")
    expected = expected_data | public_lifecycle | {bootstrap, logout, ("/api/health", "GET")}
    observed = set()
    data_routes = []
    health_routes = []
    for route in iter_route_contexts(app.routes):
        if not getattr(route, "path", "").startswith("/api/"):
            continue
        endpoints = {(route.path, method) for method in route.methods}
        observed.update(endpoints)
        assert endpoints <= expected, route.path
        calls = dependencies(route.dependant)
        if endpoints == {("/api/health", "GET")}:
            health_routes.append(route)
            assert calls == set()
        elif endpoints <= public_lifecycle:
            assert auth_routes.get_auth_settings in calls
            assert auth_routes.get_login_transaction_store in calls
            assert auth_routes.get_oidc_login_client in calls
            assert auth_http.require_authenticated_principal not in calls
            assert auth_http.get_auth_session_store not in calls
        elif endpoints == {bootstrap}:
            assert auth_routes.get_bootstrap_principal in calls
            assert auth_http.get_auth_session_store in calls
            assert auth_http.require_authenticated_principal not in calls
        elif endpoints == {logout}:
            assert auth_http.require_authenticated_principal in calls
            assert auth_http.get_auth_session_store in calls
        else:
            data_routes.append(route)
            assert endpoints <= expected_data
            assert auth_http.require_authenticated_principal in calls, route.path
            assert auth_http.get_auth_session_store in calls
    assert observed == expected
    assert len(health_routes) == 1
    assert len(data_routes) == 12
    history = [route for route in data_routes if route.path == "/api/history/summaries" or route.path.endswith("/history-detail")]
    assert len(history) == 3
    assert {method for route in history if route.path == "/api/history/summaries" for method in route.methods} == {"GET", "POST"}
    assert all(history_routes.get_history_service in dependencies(route.dependant) for route in history)

    def forbidden_auth():
        raise AssertionError("Health must remain public.")

    monkeypatch.setitem(app.dependency_overrides, auth_http.get_auth_session_store, forbidden_auth)
    monkeypatch.setitem(app.dependency_overrides, auth_http.require_authenticated_principal, forbidden_auth)
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.content == b'{"status":"ok","service":"rehearse-api"}'
    assert dict(response.headers) == {"content-length": "40", "content-type": "application/json"}
