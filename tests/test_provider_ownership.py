"""Owned provider requests with real PostgreSQL authentication and fake inference.

Only the provider boundaries are replaced. Controlled callbacks mutate committed
state during inference; no live provider, browser auth lifecycle, or History work
is exercised. Actual application auth/service dependencies share the test factory.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import inspect
from itertools import count
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, insert, select, text, update
from starlette.requests import Request

from app import auth_http, session_routes, sessions as session_module
from app.auth import AuthenticatedPrincipal, IssuedAuthSession, VerifiedExternalIdentity
from app.auth_http import AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database_models import AuthSession, QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement
from app.diagnosis import DiagnosisContext
from app.main import app
from app.nvidia_semantic_diagnosis import NVIDIASemanticDiagnosisTimeout, NVIDIASemanticDiagnosisUnavailable
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_adapter import SemanticDiagnosisAdapterContractError
from app.semantic_diagnosis_composition import get_semantic_diagnosis_adapter
from app.sessions import AttemptRequest, ContinueRequest, InterviewSessionService, QUESTIONS
from app.speaking_metrics import measure_transcription
from app.transcription import (
    TranscriptionFailed, TranscriptionResult, TranscriptionTimeout, TranscriptionUnavailable,
    get_transcription_service,
)

PROVIDER_ROUTES = ("audio", "transcription", "diagnosis")
INFERENCE_ROUTES = ("transcription", "diagnosis")
PRIVATE = "PRIVATE_PROVIDER_OWNERSHIP_FAILURE_SENTINEL"
RAW_AUDIO = b"PRIVATE_SYNTHETIC_AUDIO_NOT_PERSISTED"
PERSISTED_ANSWER = "\n  Café / 咖啡 — e\u0301\tsecond line\r\n\u00a0"
DIAGNOSIS_MARKER = "PROTECTED_DIAGNOSIS_RESULT_SENTINEL"


@pytest.fixture(autouse=True)
def no_external_provider_requests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Provider ownership tests must not make external requests.")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


def headers(login: IssuedAuthSession):
    return {
        "Cookie": f"{AUTH_SESSION_COOKIE_NAME}={login.credential}",
        AUTH_REQUEST_CONTEXT_HEADER: login.principal.request_context,
    }


def protected_rows(factory):
    with factory() as database:
        return {
            model.__tablename__: {
                row["id"]: dict(row)
                for row in database.execute(select(model.__table__)).mappings()
            }
            for model in (StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
        }


def diagnosis_result():
    return SemanticDiagnosis(
        addressed_question="partially", addressed_question_reason="Synthetic reason.",
        strengths=("Synthetic strength.",), missing_information=("Synthetic missing detail.",),
        structure="mixed", structure_feedback="Synthetic structure feedback.",
        next_focus="specificity", next_focus_reason="Synthetic focus reason.",
        retry_instruction=DIAGNOSIS_MARKER,
    )


class FakeTranscriber:
    def __init__(self):
        self.calls = []
        self.during = None
        self.error = None
        self.observe = None
        self.result = TranscriptionResult(text="Um, hello world.", language="eng", words=[
            {"text": "Um,", "start": 0.0, "end": 0.25},
            {"text": "hello", "start": 0.5, "end": 1.0},
            {"text": "world.", "start": 1.0, "end": 1.5},
        ])

    async def transcribe(self, audio, filename):
        self.calls.append((audio, filename, await audio.read()))
        self.observe()
        if self.during is not None:
            self.during()
        self.observe()
        if self.error is not None:
            raise self.error
        return self.result


class FakeDiagnoser:
    def __init__(self):
        self.calls = []
        self.during = None
        self.error = None
        self.observe = None
        self.result = diagnosis_result()

    async def diagnose(self, context):
        self.calls.append(context)
        self.observe()
        if self.during is not None:
            self.during()
        self.observe()
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class Harness:
    factory: object
    engine: object
    client: TestClient
    store: PostgreSQLAuthSessionStore
    logins: list[IssuedAuthSession]
    identifiers: tuple[UUID, UUID]
    attempt_ids: tuple[UUID, UUID]
    transcriber: FakeTranscriber
    diagnoser: FakeDiagnoser
    resolved: list
    revalidated: list
    active_transactions: set
    provider_observations: list
    revalidation_error: Exception | None = None
    after_mutation: dict | None = None

    def provider(self, operation):
        return self.transcriber if operation == "transcription" else self.diagnoser

    def service(self, actor=0):
        return InterviewSessionService(self.factory, self.logins[actor].principal)

    def request(self, operation, identifier=None, *, actor=0, question=0, attempt=1,
                revision=1, auth_headers=None, extra_fields=None, body=None):
        identifier = self.identifiers[actor] if identifier is None else identifier
        supplied = headers(self.logins[actor]) if auth_headers is None else auth_headers
        if operation == "diagnosis":
            return self.client.post(
                f"/api/sessions/{identifier}/questions/{question}/attempts/{attempt}/diagnosis",
                headers=supplied, json=body,
            )
        fields = {"question_index": str(question)}
        if operation == "transcription":
            fields["expected_last_attempt_number"] = str(revision)
        fields.update(extra_fields or {})
        suffix = "transcriptions" if operation == "transcription" else "audio"
        return self.client.post(
            f"/api/sessions/{identifier}/{suffix}", headers=supplied, data=fields,
            files={"audio": ("../../private-recording.webm", RAW_AUDIO, "audio/webm;codecs=opus")},
        )


@pytest.fixture
def harness(postgres_session_factory, postgres_engine, monkeypatch):
    credential_numbers, context_numbers = count(), count()
    store = PostgreSQLAuthSessionStore(
        postgres_session_factory, session_lifetime=timedelta(hours=1),
        credential_generator=lambda: f"PROVIDER_TOKEN_{next(credential_numbers)}_" + "t" * 48,
        request_context_generator=lambda: f"PROVIDER_CONTEXT_{next(context_numbers)}_" + "c" * 48,
    )
    logins = [store.create(user_id=store.provision_user(identity=VerifiedExternalIdentity(
        issuer="https://provider-ownership.example.test", subject=subject,
    ))) for subject in ("user-a", "user-b")]
    identifiers = tuple(InterviewSessionService(postgres_session_factory, login.principal).start().id for login in logins)
    attempt_ids = tuple(uuid4() for _ in identifiers)
    with postgres_session_factory.begin() as database:
        for identifier, attempt_id in zip(identifiers, attempt_ids):
            # Historical text deliberately bypasses submission-time trimming.
            database.execute(insert(QuestionAttempt).values(
                id=attempt_id, session_id=identifier, question_index=0, attempt_number=1,
                answer_text=PERSISTED_ANSWER,
            ))
    transcriber, diagnoser = FakeTranscriber(), FakeDiagnoser()
    monkeypatch.setattr(session_routes, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.setattr(auth_http, "get_database_session_factory", lambda: postgres_session_factory)
    for dependency in (
        session_routes.get_session_service, auth_http.get_auth_session_store,
        auth_http.require_authenticated_principal,
    ):
        monkeypatch.delitem(app.dependency_overrides, dependency, raising=False)
    monkeypatch.setitem(app.dependency_overrides, get_transcription_service, lambda: transcriber)
    monkeypatch.setitem(app.dependency_overrides, get_semantic_diagnosis_adapter, lambda: diagnoser)
    resolved, revalidated, active, observations = [], [], set(), []
    holder = {}
    original_resolve = PostgreSQLAuthSessionStore.resolve
    original_revalidate = PostgreSQLAuthSessionStore.revalidate

    def recorded_resolve(self, *, credential):
        resolved.append((credential, self))
        return original_resolve(self, credential=credential)

    def recorded_revalidate(self, *, principal):
        revalidated.append((principal, self))
        if holder["harness"].revalidation_error is not None:
            raise holder["harness"].revalidation_error
        return original_revalidate(self, principal=principal)

    monkeypatch.setattr(PostgreSQLAuthSessionStore, "resolve", recorded_resolve)
    monkeypatch.setattr(PostgreSQLAuthSessionStore, "revalidate", recorded_revalidate)

    def transaction_begin(connection):
        active.add(id(connection))

    def transaction_end(connection):
        active.discard(id(connection))

    def outside_transactions():
        observations.append((len(active), postgres_engine.pool.checkedout()))
        assert active == set()
        assert postgres_engine.pool.checkedout() == 0

    transcriber.observe = diagnoser.observe = outside_transactions
    for name, callback in (("begin", transaction_begin), ("commit", transaction_end), ("rollback", transaction_end)):
        event.listen(postgres_engine, name, callback)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            supplied = Harness(
                postgres_session_factory, postgres_engine, client, store, logins,
                identifiers, attempt_ids, transcriber, diagnoser, resolved, revalidated, active, observations,
            )
            holder["harness"] = supplied
            yield supplied
    finally:
        for name, callback in (("begin", transaction_begin), ("commit", transaction_end), ("rollback", transaction_end)):
            event.remove(postgres_engine, name, callback)


def assert_private_failure(response, status, detail, harness):
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    for private in (
        PRIVATE, PERSISTED_ANSWER, DIAGNOSIS_MARKER, RAW_AUDIO.decode(),
        *(login.credential for login in harness.logins),
        *(login.principal.request_context for login in harness.logins),
    ):
        assert private not in response.text


def assert_original_principal_revalidated_once(harness, principal):
    assert len(harness.revalidated) == 1
    actual, store = harness.revalidated[0]
    assert type(actual) is AuthenticatedPrincipal
    assert actual == principal
    assert len(harness.resolved) == 1
    assert store is harness.resolved[0][1]


@pytest.mark.parametrize("operation", PROVIDER_ROUTES)
@pytest.mark.parametrize("failure,status,detail", [
    ("missing", 401, "Authentication required."),
    ("invalid", 401, "Authentication required."),
    ("revoked", 401, "Authentication required."),
    ("expired", 401, "Authentication required."),
    ("wrong-context", 403, "Invalid authentication request context."),
    ("missing-context", 403, "Invalid authentication request context."),
    ("unavailable", 503, "Authentication is temporarily unavailable."),
])
def test_all_provider_routes_reject_auth_failures_before_inference(harness, monkeypatch, operation, failure, status, detail):
    supplied = headers(harness.logins[0])
    if failure == "missing":
        supplied = {}
    elif failure == "invalid":
        supplied["Cookie"] = f"{AUTH_SESSION_COOKIE_NAME}=invalid-local-credential"
    elif failure == "revoked":
        harness.store.revoke(auth_session_id=harness.logins[0].principal.auth_session_id)
    elif failure == "expired":
        now = datetime.now(timezone.utc)
        with harness.factory.begin() as database:
            database.execute(update(AuthSession).where(AuthSession.id == harness.logins[0].principal.auth_session_id).values(
                created_at=now - timedelta(days=2), expires_at=now - timedelta(days=1),
            ))
    elif failure == "wrong-context":
        supplied[AUTH_REQUEST_CONTEXT_HEADER] = harness.logins[1].principal.request_context
    elif failure == "missing-context":
        del supplied[AUTH_REQUEST_CONTEXT_HEADER]
    else:
        def unavailable():
            raise RuntimeError(PRIVATE)
        monkeypatch.setattr(auth_http, "get_database_session_factory", unavailable)
    before = protected_rows(harness.factory)
    response = harness.request(operation, auth_headers=supplied)
    assert_private_failure(response, status, detail, harness)
    assert response.headers["Cache-Control"] == "no-store"
    assert harness.transcriber.calls == harness.diagnoser.calls == []
    assert harness.revalidated == []
    assert protected_rows(harness.factory) == before


@pytest.mark.parametrize("operation", PROVIDER_ROUTES)
@pytest.mark.parametrize("actor", [0, 1], ids=["user-a", "user-b"])
def test_foreign_missing_and_legacy_provider_resources_are_indistinguishable_before_inference(harness, operation, actor):
    with harness.factory.begin() as database:
        legacy = StoredInterviewSession(questions=QUESTIONS, user_id=None)
        database.add(legacy)
        database.flush()
        legacy_id = legacy.id
    before = protected_rows(harness.factory)
    responses = [harness.request(operation, identifier, actor=actor)
                 for identifier in (harness.identifiers[1 - actor], uuid4(), legacy_id)]
    for response in responses:
        assert_private_failure(response, 404, "Session not found.", harness)
    assert len({response.content for response in responses}) == 1
    assert harness.transcriber.calls == harness.diagnoser.calls == []
    assert harness.revalidated == []
    assert protected_rows(harness.factory) == before
    assert before["interview_sessions"][legacy_id]["user_id"] is None


@pytest.mark.parametrize("operation", PROVIDER_ROUTES)
def test_authorized_provider_route_preserves_contract_and_closes_transaction_before_work(harness, operation):
    before = protected_rows(harness.factory)
    response = harness.request(operation)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    after = protected_rows(harness.factory)
    assert after["interview_sessions"] == before["interview_sessions"]
    assert after["question_attempts"] == before["question_attempts"]
    if operation == "audio":
        assert response.json() == {
            "session_id": str(harness.identifiers[0]), "question_index": 0,
            "filename": "answer-1.webm", "content_type": "audio/webm;codecs=opus",
            "size_bytes": len(RAW_AUDIO), "status": "accepted",
        }
        assert harness.transcriber.calls == harness.diagnoser.calls == []
        assert harness.revalidated == []
        assert after == before
    else:
        provider = harness.provider(operation)
        assert len(provider.calls) == 1
        assert_original_principal_revalidated_once(harness, harness.logins[0].principal)
        assert harness.provider_observations == [(0, 0), (0, 0)]
        if operation == "diagnosis":
            assert response.json() == provider.result.model_dump(mode="json")
            assert after == before
        else:
            body = response.json()
            assert body["session_id"] == str(harness.identifiers[0])
            assert body["text"] == provider.result.text
            assert body["words"] == provider.result.model_dump(mode="json")["words"]
            assert body["metrics"]["source"] == body["delivery_metrics"]["source"] == "original_transcription"
            assert body["metrics"]["recognized_word_count"] == 3
            assert body["metrics"]["um_count"] == 1
            identifier = UUID(body["measurement_id"])
            assert set(after["transcription_measurements"]) == {identifier}
            assert after["transcription_measurements"][identifier]["session_id"] == harness.identifiers[0]
            assert provider.calls[0][1:] == ("answer-1.webm", RAW_AUDIO)
            assert provider.calls[0][0].file.closed


def test_diagnosis_uses_exact_persisted_answer_without_identity_or_browser_context_claims(harness):
    response = harness.request("diagnosis", body={
        "answer": "Forged browser answer.", "user_id": str(harness.logins[1].principal.user_id),
        "request_context": harness.logins[1].principal.request_context,
    })
    assert response.status_code == 200
    assert len(harness.diagnoser.calls) == 1
    context = harness.diagnoser.calls[0]
    assert context.answer == PERSISTED_ANSWER
    assert context.question == QUESTIONS[0]
    assert context.question_index == 0 and context.attempt_number == 1
    assert set(context.model_dump()) == {
        "context_version", "question", "answer", "question_index", "attempt_number",
        "measurement", "previous_attempt",
    }
    encoded = context.model_dump_json()
    for private in (
        "Forged browser answer.", "https://provider-ownership.example.test", "user-a", "user-b",
        *(str(login.principal.user_id) for login in harness.logins),
        *(str(login.principal.auth_session_id) for login in harness.logins),
        *(login.principal.request_context for login in harness.logins),
        *(login.credential for login in harness.logins),
    ):
        assert private not in encoded


def test_foreign_attempt_numbers_and_invalid_owned_targets_do_not_trigger_diagnosis(harness):
    with harness.factory.begin() as database:
        database.add(QuestionAttempt(
            session_id=harness.identifiers[1], question_index=1, attempt_number=7,
            answer_text="PRIVATE_FOREIGN_ATTEMPT_ANSWER",
        ))
    before = protected_rows(harness.factory)
    foreign = harness.request("diagnosis", harness.identifiers[1], question=1, attempt=7)
    missing = harness.request("diagnosis", uuid4(), question=1, attempt=7)
    assert_private_failure(foreign, 404, "Session not found.", harness)
    assert foreign.content == missing.content
    for question, attempt, detail in ((0, 7, "Attempt not found."), (1, 7, "Attempt not found."), (99, 7, "Question not found.")):
        assert_private_failure(harness.request("diagnosis", question=question, attempt=attempt), 404, detail, harness)
    assert harness.diagnoser.calls == []
    assert harness.revalidated == []
    assert protected_rows(harness.factory) == before


@pytest.mark.parametrize("operation,state", [
    ("audio", "wrong-question"), ("audio", "completed"),
    ("transcription", "wrong-question"), ("transcription", "completed"),
    ("transcription", "stale-revision"),
])
def test_invalid_owned_upload_state_fails_before_provider_work(harness, operation, state):
    question, revision = (1, 1) if state == "wrong-question" else (0, 0 if state == "stale-revision" else 1)
    if state == "completed":
        with harness.factory.begin() as database:
            database.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == harness.identifiers[0]).values(
                status="completed", current_question_index=5, completed_at=datetime.now(timezone.utc),
            ))
    before = protected_rows(harness.factory)
    response = harness.request(operation, question=question, revision=revision)
    assert response.status_code == 409
    assert harness.transcriber.calls == harness.diagnoser.calls == []
    assert harness.revalidated == []
    assert protected_rows(harness.factory) == before


@pytest.mark.parametrize("operation", ["audio", "transcription"])
def test_foreign_measurement_context_cannot_be_supplied_to_upload_routes(harness, operation):
    foreign_measurement = harness.service(1).create_measurement(
        harness.identifiers[1], 0, measure_transcription("Foreign measurement.", "eng", []),
        expected_last_attempt_number=1,
    )
    before = protected_rows(harness.factory)
    response = harness.request(operation, extra_fields={"measurement_id": str(foreign_measurement)})
    # Multipart limits may reject the extra field before shape validation.
    assert response.status_code in (400, 422)
    assert harness.transcriber.calls == harness.diagnoser.calls == []
    assert protected_rows(harness.factory) == before


@pytest.mark.parametrize("operation", INFERENCE_ROUTES)
@pytest.mark.parametrize("race,status,detail", [
    ("revoked", 401, "Authentication required."),
    ("expired", 401, "Authentication required."),
    ("context-changed", 403, "Invalid authentication request context."),
    ("switch-revoked", 401, "Authentication required."),
    ("revalidation-unavailable", 503, "Authentication is temporarily unavailable."),
])
def test_authentication_generation_races_discard_results_without_replay_or_persistence(harness, operation, race, status, detail):
    principal = harness.logins[0].principal
    before = protected_rows(harness.factory)

    def during_inference():
        if race in ("revoked", "switch-revoked"):
            harness.store.revoke(auth_session_id=principal.auth_session_id)
        elif race == "expired":
            now = datetime.now(timezone.utc)
            with harness.factory.begin() as database:
                database.execute(update(AuthSession).where(AuthSession.id == principal.auth_session_id).values(
                    created_at=now - timedelta(days=2), expires_at=now - timedelta(days=1),
                ))
        elif race == "context-changed":
            with harness.factory.begin() as database:
                database.execute(update(AuthSession).where(AuthSession.id == principal.auth_session_id).values(
                    request_context="new-independent-login-context",
                ))
        else:
            harness.revalidation_error = RuntimeError(PRIVATE)
        if race == "switch-revoked":
            harness.logins[1] = harness.store.create(user_id=harness.logins[1].principal.user_id)
            harness.client.cookies.set(AUTH_SESSION_COOKIE_NAME, harness.logins[1].credential)

    provider = harness.provider(operation)
    provider.during = during_inference
    response = harness.request(operation)
    assert_private_failure(response, status, detail, harness)
    assert response.headers["Cache-Control"] == "no-store"
    assert len(provider.calls) == 1
    assert_original_principal_revalidated_once(harness, principal)
    assert protected_rows(harness.factory) == before
    if operation == "transcription":
        assert provider.calls[0][0].file.closed
    if race == "switch-revoked":
        replacement = harness.client.get(f"/api/sessions/{harness.identifiers[1]}", headers=headers(harness.logins[1]))
        assert replacement.status_code == 200


@pytest.mark.parametrize("operation", INFERENCE_ROUTES)
def test_new_browser_account_during_inference_never_changes_the_initiating_principal(harness, operation):
    principal = harness.logins[0].principal
    before = protected_rows(harness.factory)

    def switch_browser_account():
        harness.logins[1] = harness.store.create(user_id=harness.logins[1].principal.user_id)
        harness.client.cookies.set(AUTH_SESSION_COOKIE_NAME, harness.logins[1].credential)

    provider = harness.provider(operation)
    provider.during = switch_browser_account
    response = harness.request(operation)
    assert response.status_code == 200
    assert len(provider.calls) == 1
    assert_original_principal_revalidated_once(harness, principal)
    after = protected_rows(harness.factory)
    assert after["interview_sessions"] == before["interview_sessions"]
    assert after["question_attempts"] == before["question_attempts"]
    if operation == "transcription":
        assert response.json()["session_id"] == str(harness.identifiers[0])
        assert {row["session_id"] for row in after["transcription_measurements"].values()} == {harness.identifiers[0]}
    else:
        assert after == before
        assert response.json() == provider.result.model_dump(mode="json")


def mutate_root(harness, change):
    with harness.factory.begin() as database:
        # Proves that provider preflight retained no lock on this session row.
        database.execute(text("SET LOCAL lock_timeout = '500ms'"))
        if change == "owner-transfer":
            database.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == harness.identifiers[0]).values(
                user_id=harness.logins[1].principal.user_id,
            ))
        elif change == "session-delete":
            database.execute(delete(StoredInterviewSession).where(StoredInterviewSession.id == harness.identifiers[0]))
        elif change == "attempt-delete":
            database.execute(delete(QuestionAttempt).where(QuestionAttempt.id == harness.attempt_ids[0]))
    if change == "retry":
        harness.service().submit_attempt(harness.identifiers[0], 0, AttemptRequest(
            expected_last_attempt_number=1, answer="Concurrent retry answer.",
        ))
    elif change == "continue":
        harness.service().continue_question(harness.identifiers[0], 0, ContinueRequest(expected_last_attempt_number=1))
    harness.after_mutation = protected_rows(harness.factory)


@pytest.mark.parametrize("change,status,detail", [
    ("owner-transfer", 404, "Session not found."),
    ("session-delete", 404, "Session not found."),
    ("retry", 409, "Attempt revision does not match the current question."),
    ("continue", 409, "Answer does not match the current question."),
])
def test_transcription_fresh_owned_state_revalidation_prevents_final_measurement_write(harness, change, status, detail):
    harness.transcriber.during = lambda: mutate_root(harness, change)
    response = harness.request("transcription")
    assert_private_failure(response, status, detail, harness)
    assert len(harness.transcriber.calls) == 1
    assert_original_principal_revalidated_once(harness, harness.logins[0].principal)
    assert harness.after_mutation is not None
    assert protected_rows(harness.factory) == harness.after_mutation
    assert harness.after_mutation["transcription_measurements"] == {}
    assert harness.transcriber.calls[0][0].file.closed


@pytest.mark.parametrize("change,status,detail", [
    ("owner-transfer", 404, "Session not found."),
    ("session-delete", 404, "Session not found."),
    ("attempt-delete", 404, "Attempt not found."),
])
def test_diagnosis_fresh_owned_target_check_discards_changed_or_inaccessible_context(harness, change, status, detail):
    harness.diagnoser.during = lambda: mutate_root(harness, change)
    response = harness.request("diagnosis")
    assert_private_failure(response, status, detail, harness)
    assert len(harness.diagnoser.calls) == 1
    assert_original_principal_revalidated_once(harness, harness.logins[0].principal)
    assert harness.after_mutation is not None
    assert protected_rows(harness.factory) == harness.after_mutation


def test_diagnosis_discards_changed_typed_projection_without_altering_persisted_constraints(harness):
    """A contract fake exercises mismatch handling without rewriting attempts."""
    owned_reader = harness.service()
    calls = []

    class ChangingProjectionReader:
        def get_diagnosis_context(self, session_id, question_index, attempt_number):
            calls.append((session_id, question_index, attempt_number))
            context = owned_reader.get_diagnosis_context(session_id, question_index, attempt_number)
            if len(calls) == 1:
                return context
            return DiagnosisContext(**{
                **context.model_dump(), "answer": "Changed authoritative projection.",
            })

    before = protected_rows(harness.factory)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(session_routes.diagnose_attempt(
            session_id=harness.identifiers[0], question_index=0, attempt_number=1,
            sessions=ChangingProjectionReader(), diagnoser=harness.diagnoser,
            principal=harness.logins[0].principal, auth_store=harness.store,
        ))
    assert caught.value.status_code == 409
    assert caught.value.detail == "Attempt context no longer matches the diagnosis request."
    assert calls == [(harness.identifiers[0], 0, 1)] * 2
    assert len(harness.diagnoser.calls) == 1
    assert harness.diagnoser.calls[0].answer == PERSISTED_ANSWER
    assert harness.revalidated == [(harness.logins[0].principal, harness.store)]
    assert protected_rows(harness.factory) == before


def test_diagnosis_question_advancement_preserves_exact_historical_target_semantics(harness):
    harness.diagnoser.during = lambda: mutate_root(harness, "continue")
    response = harness.request("diagnosis")
    assert response.status_code == 200
    assert response.json() == harness.diagnoser.result.model_dump(mode="json")
    assert len(harness.diagnoser.calls) == 1
    assert harness.diagnoser.calls[0].answer == PERSISTED_ANSWER
    assert harness.after_mutation["interview_sessions"][harness.identifiers[0]]["current_question_index"] == 1
    assert protected_rows(harness.factory) == harness.after_mutation
    assert_original_principal_revalidated_once(harness, harness.logins[0].principal)


@pytest.mark.parametrize("operation", INFERENCE_ROUTES)
@pytest.mark.parametrize("failure,status", [("unavailable", 503), ("timeout", 504), ("failed", 502)])
def test_existing_provider_failure_normalization_does_not_retry_revalidate_or_write(harness, operation, failure, status):
    if operation == "transcription":
        errors = {"unavailable": TranscriptionUnavailable, "timeout": TranscriptionTimeout, "failed": TranscriptionFailed}
        details = {
            "unavailable": "Transcription is not configured on the server.",
            "timeout": "Transcription timed out. Please try again.",
            "failed": "Unable to transcribe this recording. Try again or type your answer.",
        }
    else:
        errors = {"unavailable": NVIDIASemanticDiagnosisUnavailable, "timeout": NVIDIASemanticDiagnosisTimeout,
                  "failed": SemanticDiagnosisAdapterContractError}
        details = {"unavailable": "Semantic diagnosis is not configured.", "timeout": "Semantic diagnosis timed out.",
                   "failed": "Unable to generate semantic diagnosis."}
    provider = harness.provider(operation)
    provider.error = errors[failure](PRIVATE)
    before = protected_rows(harness.factory)
    response = harness.request(operation)
    assert_private_failure(response, status, details[failure], harness)
    assert len(provider.calls) == 1
    assert harness.revalidated == []
    assert protected_rows(harness.factory) == before
    if operation == "transcription":
        assert provider.calls[0][0].file.closed


def multipart_request(identifier):
    supplied = httpx.Request(
        "POST", f"http://testserver/api/sessions/{identifier}/transcriptions",
        data={"question_index": "0", "expected_last_attempt_number": "1"},
        files={"audio": ("private.webm", RAW_AUDIO, "audio/webm")},
    )
    body = supplied.read()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": supplied.url.path,
        "headers": [(name.lower(), value) for name, value in supplied.headers.raw],
        "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("test", 1),
    }, receive)


@pytest.mark.parametrize("operation", INFERENCE_ROUTES)
def test_provider_cancellation_propagates_without_post_validation_or_final_writes(harness, operation):
    provider = harness.provider(operation)
    cancellation = asyncio.CancelledError(PRIVATE)
    provider.error = cancellation
    before = protected_rows(harness.factory)
    shared = {
        "session_id": harness.identifiers[0], "sessions": harness.service(),
        "principal": harness.logins[0].principal, "auth_store": harness.store,
    }
    if operation == "transcription":
        invocation = session_routes.transcribe_audio(
            **shared, request=multipart_request(harness.identifiers[0]), transcriber=provider,
        )
    else:
        invocation = session_routes.diagnose_attempt(**shared, question_index=0, attempt_number=1, diagnoser=provider)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(invocation)
    assert caught.value is cancellation
    assert len(provider.calls) == 1
    assert harness.revalidated == []
    assert protected_rows(harness.factory) == before
    if operation == "transcription":
        assert provider.calls[0][0].file.closed


def test_provider_uuid_compatibility_is_removed_and_all_provider_dependencies_require_a_principal():
    for name in ("get_transitional_provider_session_service", "TransitionalProviderService"):
        assert not hasattr(session_routes, name)
    for name in ("TransitionalProviderSessionService", "_TransitionalProviderPersistence"):
        assert not hasattr(session_module, name)
    parameter = inspect.signature(InterviewSessionService).parameters["principal"]
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        InterviewSessionService(object())

    def calls(dependant):
        result = set()
        for dependency in dependant.dependencies:
            result.add(dependency.call)
            result.update(calls(dependency))
        return result

    covered = {session_routes.accept_audio, session_routes.transcribe_audio, session_routes.diagnose_attempt}
    seen = set()
    for route in iter_route_contexts(app.routes):
        if getattr(route, "endpoint", None) not in covered:
            continue
        seen.add(route.endpoint)
        dependencies = calls(route.dependant)
        assert session_routes.get_session_service in dependencies
        assert auth_http.require_authenticated_principal in dependencies
        assert auth_http.get_auth_session_store in dependencies
    assert seen == covered
