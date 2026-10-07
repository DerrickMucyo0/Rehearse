"""Adaptive Continue through real HTTP auth and isolated PostgreSQL.

Question generation is synthetic. Every transport entry is forbidden, including
tests of inference-time revocation and competing commits.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app import session_routes
from app.auth import VerifiedExternalIdentity
from app.auth_http import (
    AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME, get_auth_session_store,
)
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database_models import QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement
from app.main import app
from app.roleplay import RoleplayContractError, RoleplayQuestion, RoleplayUnavailable
from app.roleplay_application import continue_application_attempt
from app.roleplay_composition import get_roleplay_adapter
from app.scenarios import SCENARIO_QUESTIONS
from app.sessions import AttemptRequest, ContinueRequest, InterviewSessionService

UNAVAILABLE = {
    "detail": "Interviewer is unavailable right now. Try Continue again.",
    "code": "roleplay_generation_unavailable",
    "write_outcome": "not_applied",
}
STATE_CHANGED = {"detail": "Interview state changed. Recheck before continuing."}


def persisted(factory):
    with factory() as database:
        return {
            model.__tablename__: list(database.execute(
                select(model.__table__).order_by(model.__table__.c.id),
            ).mappings())
            for model in (StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
        }


class Generator:
    def __init__(self):
        self.contexts = []
        self.callback = None

    async def generate(self, context):
        self.contexts.append(context)
        if self.callback is not None:
            await self.callback(context)
        return RoleplayQuestion(
            roleplay_version="live-ai-roleplay-v1",
            next_question=f"What did you learn from step {context.next_question_number - 1}?",
        )


@dataclass
class Harness:
    factory: object
    engine: object
    store: PostgreSQLAuthSessionStore
    issued: object
    generator: Generator
    client: TestClient

    @property
    def service(self):
        return InterviewSessionService(self.factory, self.issued.principal)

    def create(self, scenario="job_interview"):
        response = self.client.post("/api/sessions", json={"scenario_type": scenario})
        assert response.status_code == 201
        return response.json()

    def submit(self, identifier, question=0, revision=0, answer="My persisted answer."):
        response = self.client.post(
            f"/api/sessions/{identifier}/questions/{question}/attempts",
            json={"answer": answer, "expected_last_attempt_number": revision},
        )
        assert response.status_code == 201
        return response.json()

    def proceed(self, identifier, question=0, revision=1):
        return self.client.post(
            f"/api/sessions/{identifier}/questions/{question}/continue",
            json={"expected_last_attempt_number": revision},
        )


@pytest.fixture
def harness(postgres_session_factory, postgres_engine, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live provider requests are forbidden.")

    async def async_blocked(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)
    store = PostgreSQLAuthSessionStore(postgres_session_factory, session_lifetime=timedelta(hours=1))
    issued = store.create(user_id=store.provision_user(identity=VerifiedExternalIdentity(
        issuer="https://roleplay.example.test", subject=str(uuid4()),
    )))
    generator = Generator()
    monkeypatch.setattr(session_routes, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.delitem(app.dependency_overrides, session_routes.get_session_service, raising=False)
    monkeypatch.setitem(app.dependency_overrides, get_auth_session_store, lambda: store)
    monkeypatch.setitem(app.dependency_overrides, get_roleplay_adapter, lambda: generator)
    with TestClient(app, raise_server_exceptions=False, headers={
        "Cookie": f"{AUTH_SESSION_COOKIE_NAME}={issued.credential}",
        AUTH_REQUEST_CONTEXT_HEADER: issued.principal.request_context,
    }) as client:
        yield Harness(postgres_session_factory, postgres_engine, store, issued, generator, client)


@pytest.mark.parametrize("scenario", tuple(SCENARIO_QUESTIONS))
def test_http_creation_persists_only_deterministic_first_question_without_inference(harness, scenario):
    created = harness.create(scenario)
    assert created["question_engine"] == "live-ai-roleplay-v1"
    assert created["questions"] == [SCENARIO_QUESTIONS[scenario][0]]
    assert created["total_questions"] == 5
    assert created["current_question_index"] == 0
    assert harness.generator.contexts == []
    assert harness.client.get(f"/api/sessions/{created['id']}").json() == created


def test_five_questions_complete_with_four_generations_and_no_sixth_question(harness):
    created = harness.create("thesis_defense")
    identifier = created["id"]
    for index in range(5):
        submitted = harness.submit(identifier, index, answer=f"Answer {index + 1}.")
        assert submitted["attempt"]["attempt_number"] == 1
        assert len(harness.generator.contexts) == min(index, 4)
        response = harness.proceed(identifier, index)
        assert response.status_code == 200
        current = response.json()
        assert current["current_question_index"] == index + 1
        assert len(current["questions"]) == min(index + 2, 5)
        assert current["status"] == ("completed" if index == 4 else "active")
        assert len(harness.generator.contexts) == min(index + 1, 4)
        if index < 4:
            context = harness.generator.contexts[-1]
            assert context.scenario_type == "thesis_defense"
            assert context.next_question_number == index + 2
            assert [turn.answer for turn in context.turns] == [f"Answer {n + 1}." for n in range(index + 1)]
    assert current["current_question"] is None
    assert current["total_questions"] == 5
    assert harness.proceed(identifier, 4).status_code == 409
    assert len(harness.generator.contexts) == 4


def test_generation_uses_latest_persisted_retry_and_no_open_database_connection(harness):
    created = harness.create()
    harness.submit(created["id"], answer="Original answer.")
    harness.submit(created["id"], revision=1, answer="Exact final retry answer.")

    async def outside_transaction(context):
        assert harness.engine.pool.checkedout() == 0
        assert context.turns[0].answer == "Exact final retry answer."
        assert set(context.model_dump()) == {
            "context_version", "scenario_type", "next_question_number", "turns",
        }
        assert set(context.turns[0].model_dump()) == {"question_number", "question", "answer"}

    harness.generator.callback = outside_transaction
    response = harness.proceed(created["id"], revision=2)
    assert response.status_code == 200
    assert response.json()["answers"] == ["Exact final retry answer."]


@pytest.mark.parametrize("error", [
    RoleplayUnavailable("unavailable"), RoleplayUnavailable("timeout"),
    RoleplayUnavailable("http_status", 429), RoleplayUnavailable("transport"),
    RoleplayContractError(), RuntimeError("PRIVATE_PROVIDER_CONTENT"),
])
def test_known_generation_failure_is_sanitized_no_write_and_allows_explicit_retry(harness, error):
    created = harness.create()
    harness.submit(created["id"])
    before = persisted(harness.factory)

    async def fail(context):
        raise error

    harness.generator.callback = fail
    response = harness.proceed(created["id"])
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert response.headers["Cache-Control"] == "no-store"
    assert "PRIVATE_PROVIDER_CONTENT" not in response.text
    assert persisted(harness.factory) == before
    assert len(harness.generator.contexts) == 1
    harness.generator.callback = None
    assert harness.proceed(created["id"]).status_code == 200
    assert len(harness.generator.contexts) == 2


def test_logout_during_inference_prevents_generated_question_persistence(harness):
    created = harness.create()
    harness.submit(created["id"])
    before = persisted(harness.factory)

    async def revoke(context):
        await run_in_threadpool(harness.store.revoke, auth_session_id=harness.issued.principal.auth_session_id)

    harness.generator.callback = revoke
    response = harness.proceed(created["id"])
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
    assert response.headers["Cache-Control"] == "no-store"
    assert persisted(harness.factory) == before
    assert len(harness.generator.contexts) == 1


def test_retry_committed_during_inference_is_detected_before_adaptive_write(harness):
    created = harness.create()
    harness.submit(created["id"])

    async def retry(context):
        await run_in_threadpool(
            harness.service.submit_attempt, UUID(created["id"]), 0,
            AttemptRequest(answer="A concurrent retry.", expected_last_attempt_number=1),
        )

    harness.generator.callback = retry
    response = harness.proceed(created["id"])
    assert response.status_code == 409
    assert response.json() == STATE_CHANGED
    saved = harness.service.get(UUID(created["id"]))
    assert saved.current_question_index == 0
    assert len(saved.questions) == 1
    assert saved.answers == []
    assert saved.current_question_latest_attempt_number == 2


def test_competing_continue_publishes_only_one_question_and_loser_gets_fixed_409(harness):
    created = harness.create()
    harness.submit(created["id"])
    barrier = Barrier(2)

    async def compete(context):
        await run_in_threadpool(barrier.wait, timeout=10)

    harness.generator.callback = compete
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: harness.proceed(created["id"]), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert next(response for response in responses if response.status_code == 409).json() == STATE_CHANGED
    saved = harness.service.get(UUID(created["id"]))
    assert saved.current_question_index == 1
    assert len(saved.questions) == 2
    assert len(saved.answers) == 1
    assert len(harness.generator.contexts) == 2


def test_legacy_continue_and_restoration_never_generate_questions(harness):
    legacy = harness.service.start("salary_negotiation")
    harness.submit(str(legacy.id))
    response = harness.proceed(str(legacy.id))
    assert response.status_code == 200
    assert response.json()["question_engine"] == "deterministic-v1"
    assert response.json()["questions"] == list(legacy.questions)
    assert harness.generator.contexts == []


@pytest.mark.parametrize("path,body,status", [
    ("not-a-uuid/questions/0/continue", {"expected_last_attempt_number": 1}, 422),
    ("{id}/questions/-1/continue", {"expected_last_attempt_number": 1}, 422),
    ("{id}/questions/0/continue", {"expected_last_attempt_number": 0}, 409),
    ("{id}/questions/0/continue", {"expected_last_attempt_number": 1, "next_question": "Injected"}, 422),
    ("{id}/questions/0/continue", {"expected_last_attempt_number": 1}, 409),
])
def test_invalid_or_unsubmitted_continue_cannot_generate(harness, path, body, status):
    created = harness.create()
    before = persisted(harness.factory)
    response = harness.client.post("/api/sessions/" + path.format(id=created["id"]), json=body)
    assert response.status_code == status
    assert harness.generator.contexts == []
    assert persisted(harness.factory) == before


def test_caller_cancellation_before_commit_propagates_without_write(harness):
    created = harness.create()
    harness.submit(created["id"])
    before = persisted(harness.factory)

    async def cancel(context):
        raise asyncio.CancelledError

    harness.generator.callback = cancel
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(continue_application_attempt(
            harness.service, harness.generator, session_id=UUID(created["id"]), question_index=0,
            request=ContinueRequest(expected_last_attempt_number=1),
            principal_guard=lambda database: harness.store.revalidate_in_transaction(
                database=database, principal=harness.issued.principal,
            ),
        ))
    assert persisted(harness.factory) == before
    assert len(harness.generator.contexts) == 1
