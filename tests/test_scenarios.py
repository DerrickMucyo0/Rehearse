"""Scenario selection, legacy creation, immutable snapshots and owned history."""
from datetime import datetime, timezone
from types import MappingProxyType
from typing import get_args
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Text, select

from app import history_routes, scenarios, session_routes
from app.auth import AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind
from app.auth_http import (
    AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME, AuthenticatedPrincipalDependency,
    get_auth_session_store,
)
from app.database_models import StoredInterviewSession, User
from app.history import HistoryIntegrityError, HistoryReadService
from app.main import app
from app.roleplay import RoleplayContext, RoleplayQuestion
from app.roleplay_composition import get_roleplay_adapter
from app.scenarios import SCENARIO_QUESTIONS, ScenarioType, questions_for_scenario
from app.sessions import (
    QUESTIONS, AttemptRequest, ContinueRequest, InterviewSession, InterviewSessionService,
    SessionNotFound, StartSessionRequest,
)

SCENARIO_TYPES = (
    "job_interview", "public_speaking", "thesis_defense", "salary_negotiation",
)
JOB_QUESTIONS = (
    "Tell me about yourself.",
    "Tell me about a challenging problem you solved.",
    "Tell me about a time you worked with a team.",
    "Why are you interested in this opportunity?",
    "What is one project you are proud of and why?",
)
INVALID_SCENARIOS = (None, "", "interview", "Job_interview", "job_interview ",
                     " public_speaking", "salary_negotiation\x00", 1, True, [], {})


def test_catalog_has_only_four_deterministic_immutable_five_question_sets():
    assert get_args(ScenarioType) == SCENARIO_TYPES
    assert tuple(SCENARIO_QUESTIONS) == SCENARIO_TYPES
    assert QUESTIONS is SCENARIO_QUESTIONS["job_interview"]
    assert QUESTIONS == JOB_QUESTIONS
    assert len(set(SCENARIO_QUESTIONS.values())) == 4
    for scenario_type in SCENARIO_TYPES:
        questions = questions_for_scenario(scenario_type)
        assert questions is SCENARIO_QUESTIONS[scenario_type]
        assert type(questions) is tuple and len(questions) == 5
        assert len(set(questions)) == 5
        assert all(type(question) is str and question.strip() == question and question for question in questions)
    with pytest.raises(TypeError):
        SCENARIO_QUESTIONS["job_interview"] = ("Changed",) * 5


@pytest.mark.parametrize("value", INVALID_SCENARIOS)
def test_domain_rejects_unknown_or_non_text_scenarios_without_coercion(value):
    with pytest.raises(ValueError, match="^Unsupported practice scenario\\.$"):
        questions_for_scenario(value)


@pytest.mark.parametrize("value", INVALID_SCENARIOS)
def test_direct_start_validates_scenario_before_opening_a_transaction(value):
    class Factory:
        def begin(self):
            raise AssertionError("Invalid scenarios must not open a transaction.")

    principal = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context="test")
    service = InterviewSessionService(Factory(), principal)
    with pytest.raises(ValueError, match="^Unsupported practice scenario\\.$"):
        service.start(value)


def test_start_request_defaults_and_stored_mapping_match_legacy_job_selection():
    assert StartSessionRequest().model_dump() == {"scenario_type": "job_interview"}
    table = StoredInterviewSession.__table__
    column = table.c.scenario_type
    assert isinstance(column.type, Text) and column.type.collation == "C"
    assert not column.nullable
    assert column.default.arg == str(column.server_default.arg) == "job_interview"
    check = next(constraint for constraint in table.constraints
                 if isinstance(constraint, CheckConstraint) and constraint.name == "ck_sessions_scenario_type")
    for scenario_type in SCENARIO_TYPES:
        assert f"'{scenario_type}'" in str(check.sqltext)
        assert StoredInterviewSession(questions=QUESTIONS, scenario_type=scenario_type).scenario_type == scenario_type


@pytest.mark.parametrize("value", INVALID_SCENARIOS)
def test_request_and_mapping_reject_invalid_scenario_values(value):
    with pytest.raises(ValidationError):
        StartSessionRequest(scenario_type=value)
    with pytest.raises(ValueError, match="Unsupported practice scenario"):
        StoredInterviewSession(questions=QUESTIONS, scenario_type=value)


class OfflineSessionService:
    def __init__(self):
        self.calls = []

    def start_adaptive(self, scenario_type="job_interview"):
        self.calls.append(scenario_type)
        return InterviewSession(id=uuid4(), scenario_type=scenario_type,
                                question_engine="live-ai-roleplay-v1",
                                questions=list(questions_for_scenario(scenario_type)[:1]))


class CatalogRoleplayAdapter:
    """Returns catalog fixtures only to exercise the persisted HTTP lifecycle."""

    async def generate(self, context: RoleplayContext) -> RoleplayQuestion:
        return RoleplayQuestion(
            roleplay_version="live-ai-roleplay-v1",
            next_question=questions_for_scenario(context.scenario_type)[context.next_question_number - 1],
        )


@pytest.fixture
def offline_client(monkeypatch):
    service = OfflineSessionService()
    principal = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context="scenario-context")

    class Store:
        def resolve(self, *, credential):
            if credential != "scenario-credential":
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            return principal

    def owned_service(resolved: AuthenticatedPrincipalDependency):
        assert resolved is principal
        return service

    def blocked(*args, **kwargs):
        raise AssertionError("Offline scenario tests must not connect to PostgreSQL or providers.")

    async def async_blocked(*args, **kwargs):
        blocked()

    monkeypatch.setattr(psycopg, "connect", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[session_routes.get_session_service] = owned_service
    app.dependency_overrides[get_auth_session_store] = lambda: Store()
    monkeypatch.setitem(app.dependency_overrides, get_roleplay_adapter, lambda: CatalogRoleplayAdapter())
    try:
        with TestClient(app, headers={
            "Cookie": f"{AUTH_SESSION_COOKIE_NAME}=scenario-credential",
            AUTH_REQUEST_CONTEXT_HEADER: principal.request_context,
        }) as client:
            yield client, service
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("body", [None, {}, *({"scenario_type": value} for value in SCENARIO_TYPES)])
def test_creation_accepts_bodyless_empty_and_each_explicit_scenario(offline_client, body):
    client, service = offline_client
    response = client.post("/api/sessions", **({} if body is None else {"json": body}))
    assert response.status_code == 201
    session = response.json()
    expected = "job_interview" if body is None else body.get("scenario_type", "job_interview")
    assert service.calls == [expected]
    assert session["scenario_type"] == expected
    assert session["question_engine"] == "live-ai-roleplay-v1"
    assert session["total_questions"] == 5
    assert session["questions"] == list(questions_for_scenario(expected)[:1])
    assert session["current_question"] == session["questions"][0]
    assert response.headers["Location"] == f"/api/sessions/{session['id']}"


@pytest.mark.parametrize("body", [
    *({"scenario_type": value} for value in INVALID_SCENARIOS),
    {"user_id": str(uuid4())}, {"scenario_type": "public_speaking", "question_count": 2},
    {"scenario_type": "thesis_defense", "questions": ["Injected"] * 5}, [], "public_speaking", 42,
])
def test_invalid_or_extra_creation_fields_return_422_without_starting(offline_client, body):
    client, service = offline_client
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 422
    assert service.calls == []


def test_malformed_creation_json_is_422_without_starting(offline_client):
    client, service = offline_client
    response = client.post("/api/sessions", content='{ "scenario_type": ',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert service.calls == []


def test_scenario_creation_still_requires_authenticated_owner(offline_client):
    client, service = offline_client
    client.headers.pop("Cookie")
    response = client.post("/api/sessions", json={"scenario_type": "public_speaking"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
    assert service.calls == []


def stored_history_root(scenario_type):
    return {
        "session_id": uuid4(), "scenario_type": scenario_type,
        "question_engine": "deterministic-v1", "status": "active",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "completed_at": None,
        "current_question_index": 0, "questions": tuple(f"Stored snapshot question {index}" for index in range(5)),
        "question_index": None,
    }


@pytest.mark.parametrize("scenario_type", SCENARIO_TYPES)
def test_history_projects_stored_scenario_and_snapshot_without_regenerating_questions(scenario_type):
    stored = stored_history_root(scenario_type)
    projected = HistoryReadService._project_session([stored])
    assert projected.summary.scenario_type == scenario_type
    assert [question.question_text for question in projected.questions] == list(stored["questions"])


@pytest.mark.parametrize("scenario_type", INVALID_SCENARIOS)
def test_history_rejects_unknown_stored_scenarios_as_integrity_errors(scenario_type):
    with pytest.raises(HistoryIntegrityError) as caught:
        HistoryReadService._project_session([stored_history_root(scenario_type)])
    assert str(caught.value) == ""


@pytest.fixture
def scenario_client(
    postgres_session_factory, authenticated_principal, authenticated_http_headers,
    authenticated_session_override, monkeypatch,
):
    sessions = InterviewSessionService(postgres_session_factory, authenticated_principal)
    history = HistoryReadService(postgres_session_factory, authenticated_principal)

    def blocked(*args, **kwargs):
        raise AssertionError("Scenario persistence tests must not invoke providers.")

    async def async_blocked(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[session_routes.get_session_service] = authenticated_session_override(sessions)
    app.dependency_overrides[history_routes.get_history_service] = authenticated_session_override(history)
    monkeypatch.setitem(app.dependency_overrides, get_roleplay_adapter, lambda: CatalogRoleplayAdapter())
    try:
        with TestClient(app, headers=authenticated_http_headers) as client:
            yield client, sessions, history
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("scenario_type", SCENARIO_TYPES)
def test_every_scenario_persists_retries_completion_and_history(
    scenario_client, postgres_session_factory, authenticated_principal, scenario_type,
):
    client, sessions, history = scenario_client
    created_response = client.post("/api/sessions", json={"scenario_type": scenario_type})
    assert created_response.status_code == 201
    created = created_response.json()
    identifier = UUID(created["id"])
    assert client.get(created_response.headers["Location"]).json() == created
    catalog_questions = questions_for_scenario(scenario_type)
    assert created["question_engine"] == "live-ai-roleplay-v1"
    assert created["total_questions"] == 5
    assert created["questions"] == list(catalog_questions[:1])
    with postgres_session_factory() as database:
        stored = database.get(StoredInterviewSession, identifier)
        assert stored.user_id == authenticated_principal.user_id
        assert stored.scenario_type == scenario_type
        assert stored.question_engine == "live-ai-roleplay-v1"
        assert stored.questions == catalog_questions[:1]

    for index in range(5):
        root = f"/api/sessions/{identifier}/questions/{index}"
        first = client.post(f"{root}/attempts", json={
            "answer": f"First answer {index}", "expected_last_attempt_number": 0,
        })
        assert first.status_code == 201
        assert first.json()["session"]["scenario_type"] == scenario_type
        retry = client.post(f"{root}/attempts", json={
            "answer": f"Final answer {index}", "expected_last_attempt_number": 1,
        })
        assert retry.status_code == 201
        assert retry.json()["session"]["current_question_index"] == index
        assert retry.json()["session"]["questions"] == list(catalog_questions[:index + 1])
        continued = client.post(f"{root}/continue", json={"expected_last_attempt_number": 2})
        assert continued.status_code == 200
        assert continued.json()["scenario_type"] == scenario_type
        assert continued.json()["questions"] == list(catalog_questions[:min(index + 2, 5)])
        assert continued.json()["total_questions"] == 5
    completed = continued.json()
    assert completed["status"] == "completed" and completed["current_question"] is None
    assert completed["questions"] == list(catalog_questions)
    assert completed["answers"] == [f"Final answer {index}" for index in range(5)]
    rebuilt = InterviewSessionService(postgres_session_factory, authenticated_principal)
    assert rebuilt.get(identifier).model_dump(mode="json") == completed
    summary = client.get("/api/history/summaries").json()["items"][0]
    assert summary["scenario_type"] == scenario_type
    assert summary["question_engine"] == "live-ai-roleplay-v1" and summary["total_questions"] == 5
    assert summary["total_attempt_count"] == 10 and summary["total_retry_count"] == 5
    assert summary["finalized_question_count"] == 5
    assert client.post("/api/history/summaries", json={"session_ids": [str(identifier)]}).json()["summaries"] == [summary]
    detail = client.get(f"/api/sessions/{identifier}/history-detail").json()
    assert detail["summary"] == summary
    assert [question["question_text"] for question in detail["questions"]] == completed["questions"]
    assert history.get_detail(identifier).summary.scenario_type == scenario_type


def test_persisted_snapshot_survives_catalog_changes_and_mutated_response(
    scenario_client, monkeypatch,
):
    _, sessions, history = scenario_client
    created = sessions.start("public_speaking")
    original_questions = tuple(created.questions)
    created.questions[0] = "Changed returned copy"
    monkeypatch.setattr(scenarios, "SCENARIO_QUESTIONS", MappingProxyType({
        **SCENARIO_QUESTIONS, "public_speaking": tuple(f"New question {index}" for index in range(5)),
    }))
    restored = sessions.get(created.id)
    assert restored.scenario_type == "public_speaking"
    assert tuple(restored.questions) == original_questions
    submitted = sessions.submit_attempt(created.id, 0, AttemptRequest(
        answer="An answer to the stored question.", expected_last_attempt_number=0,
    ))
    assert submitted.session.current_question == original_questions[0]
    advanced = sessions.continue_question(created.id, 0, ContinueRequest(expected_last_attempt_number=1))
    assert advanced.current_question == original_questions[1]
    assert [question.question_text for question in history.get_detail(created.id).questions] == list(original_questions)
    assert sessions.start("public_speaking").questions == [f"New question {index}" for index in range(5)]


def test_scenario_selection_preserves_owner_isolation_for_sessions_and_history(
    scenario_client, postgres_session_factory, authenticated_principal,
):
    _, owner_sessions, owner_history = scenario_client
    with postgres_session_factory.begin() as database:
        user = User(auth_provider="https://scenario.example.test", provider_subject=str(uuid4()))
        database.add(user)
        database.flush()
        foreign_principal = AuthenticatedPrincipal(
            user_id=user.id, auth_session_id=uuid4(), request_context="foreign-scenario-context",
        )
    foreign_sessions = InterviewSessionService(postgres_session_factory, foreign_principal)
    foreign_history = HistoryReadService(postgres_session_factory, foreign_principal)
    owned = [owner_sessions.start(value) for value in SCENARIO_TYPES]
    foreign = [foreign_sessions.start(value) for value in SCENARIO_TYPES]
    owned_ids, foreign_ids = [session.id for session in owned], [session.id for session in foreign]
    assert {summary.session_id for summary in owner_history.get_discovery().items} == set(owned_ids)
    assert {summary.session_id for summary in foreign_history.get_discovery().items} == set(foreign_ids)
    mixed = owner_history.get_summaries([*owned_ids, *foreign_ids])
    assert {summary.session_id for summary in mixed.summaries} == set(owned_ids)
    assert mixed.missing_session_ids == foreign_ids
    for session in foreign:
        for operation in (
            lambda: owner_sessions.get(session.id),
            lambda: owner_sessions.get_attempts(session.id, 0),
            lambda: owner_sessions.get_comparison(session.id, 0),
            lambda: owner_sessions.submit_attempt(session.id, 0, AttemptRequest(
                answer="Foreign answer", expected_last_attempt_number=0,
            )),
            lambda: owner_sessions.continue_question(session.id, 0, ContinueRequest(
                expected_last_attempt_number=0,
            )),
            lambda: owner_history.get_detail(session.id),
        ):
            with pytest.raises(SessionNotFound):
                operation()
    with postgres_session_factory() as database:
        roots = database.scalars(select(StoredInterviewSession)).all()
        assert {root.id for root in roots if root.user_id == authenticated_principal.user_id} == set(owned_ids)
        assert {root.id for root in roots if root.user_id == foreign_principal.user_id} == set(foreign_ids)
