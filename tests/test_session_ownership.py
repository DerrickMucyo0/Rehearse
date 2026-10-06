"""Core interview ownership through real local authentication and PostgreSQL.

The actual application dependencies share the isolated test factory. No auth
principal or interview service is injected into the protected HTTP routes.
History and browser authentication lifecycle remain outside this slice.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import count
import inspect
from pathlib import Path
from threading import Barrier
from typing import get_type_hints
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi.routing import iter_route_contexts
from sqlalchemy import event, select, update

from app import auth_http, session_routes, sessions as session_module
from app.auth import AuthenticatedPrincipal, IssuedAuthSession, VerifiedExternalIdentity
from app.auth_http import AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database_models import (
    MEASUREMENT_VERSION, AuthSession, QuestionAttempt, StoredInterviewSession,
    TranscriptionMeasurement,
)
from app.main import app
from app.sessions import QUESTIONS, InterviewSessionService

CORE_OPERATIONS = ("get", "submit", "list", "continue", "comparison")
NOT_FOUND = {"detail": "Session not found."}
PRIVATE_DETAIL = "PRIVATE_OWNERSHIP_DATABASE_FAILURE_SENTINEL"


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Core ownership tests must not make provider requests.")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


def headers(issued: IssuedAuthSession):
    return {
        "Cookie": f"{AUTH_SESSION_COOKIE_NAME}={issued.credential}",
        AUTH_REQUEST_CONTEXT_HEADER: issued.principal.request_context,
    }


def persisted(factory):
    with factory() as database:
        return {
            model.__tablename__: {
                row["id"]: dict(row)
                for row in database.execute(select(model.__table__)).mappings()
            }
            for model in (StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
        }


@dataclass
class Harness:
    factory: object
    engine: object
    client: TestClient
    store: PostgreSQLAuthSessionStore
    logins: tuple[IssuedAuthSession, IssuedAuthSession]

    def create(self, actor=0):
        response = self.client.post("/api/sessions", headers=headers(self.logins[actor]))
        assert response.status_code == 201
        assert response.headers["Location"] == f"/api/sessions/{response.json()['id']}"
        return UUID(response.json()["id"])

    def request(self, operation, identifier, actor=0, *, question=0, revision=0,
                answer="Submitted answer.", measurement_id=None, auth_headers=None):
        supplied = headers(self.logins[actor]) if auth_headers is None else auth_headers
        root = f"/api/sessions/{identifier}"
        question_root = f"{root}/questions/{question}"
        if operation == "get":
            return self.client.get(root, headers=supplied)
        if operation == "list":
            return self.client.get(f"{question_root}/attempts", headers=supplied)
        if operation == "comparison":
            return self.client.get(f"{question_root}/comparison", headers=supplied)
        if operation == "continue":
            return self.client.post(f"{question_root}/continue", headers=supplied, json={
                "expected_last_attempt_number": revision,
            })
        payload = {"expected_last_attempt_number": revision, "answer": answer}
        if measurement_id is not None:
            payload["measurement_id"] = str(measurement_id)
        return self.client.post(f"{question_root}/attempts", headers=supplied, json=payload)


@pytest.fixture
def harness(postgres_session_factory, postgres_engine, monkeypatch):
    credential_numbers, context_numbers = count(), count()
    store = PostgreSQLAuthSessionStore(
        postgres_session_factory, session_lifetime=timedelta(hours=1),
        credential_generator=lambda: f"OWNERSHIP_TOKEN_{next(credential_numbers)}_" + "t" * 48,
        request_context_generator=lambda: f"OWNERSHIP_CONTEXT_{next(context_numbers)}_" + "c" * 48,
    )
    logins = tuple(store.create(user_id=store.provision_user(identity=VerifiedExternalIdentity(
        issuer="https://ownership.example.test", subject=subject,
    ))) for subject in ("user-a", "user-b"))
    monkeypatch.setattr(session_routes, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.setattr(auth_http, "get_database_session_factory", lambda: postgres_session_factory)
    # Exercise production authentication and owner-bound service composition,
    # even if another shared test fixture has installed temporary overrides.
    for dependency in (
        session_routes.get_session_service,
        auth_http.get_auth_session_store,
        auth_http.require_authenticated_principal,
    ):
        monkeypatch.delitem(app.dependency_overrides, dependency, raising=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield Harness(postgres_session_factory, postgres_engine, client, store, logins)


def legacy_session(factory):
    with factory.begin() as database:
        row = StoredInterviewSession(questions=QUESTIONS, user_id=None)
        database.add(row)
        database.flush()
        return row.id


def measurement(factory, identifier, *, question=0):
    with factory.begin() as database:
        row = TranscriptionMeasurement(
            session_id=identifier, question_index=question,
            measurement_version=MEASUREMENT_VERSION, measurement_source="original_transcription",
            recognized_word_count=2, um_count=0, uh_count=0,
            filler_unavailable_reason=None, timed_utterance_span_seconds=1.0,
            estimated_words_per_minute=120.0, timing_unavailable_reason=None,
        )
        database.add(row)
        database.flush()
        return row.id


def assert_not_found(response):
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert response.content == b'{"detail":"Session not found."}'


@pytest.mark.parametrize("actor", [0, 1], ids=["user-a", "user-b"])
def test_owned_core_operations_preserve_retry_continue_and_comparison(harness, actor):
    identifier = harness.create(actor)
    created = harness.request("get", identifier, actor).json()
    assert created["current_question_index"] == 0
    assert created["answers"] == []
    assert created["current_question_latest_attempt_number"] == 0
    assert harness.request("list", identifier, actor).json() == []
    first = harness.request("submit", identifier, actor, answer="  First answer.  ")
    second = harness.request("submit", identifier, actor, revision=1, answer="Second answer.")
    assert first.status_code == second.status_code == 201
    assert first.json()["attempt"]["attempt_number"] == 1
    assert first.json()["attempt"]["answer"] == "First answer."
    assert second.json()["attempt"]["attempt_number"] == 2
    assert second.json()["session"]["current_question_index"] == 0
    assert second.json()["session"]["answers"] == []
    listed = harness.request("list", identifier, actor)
    assert listed.status_code == 200
    assert listed.json() == [first.json()["attempt"], second.json()["attempt"]]
    compared = harness.request("comparison", identifier, actor)
    assert compared.status_code == 200
    assert compared.json()["before_attempt"]["id"] == first.json()["attempt"]["id"]
    assert compared.json()["after_attempt"]["id"] == second.json()["attempt"]["id"]
    stale = harness.request("continue", identifier, actor, revision=1)
    assert stale.status_code == 409
    continued = harness.request("continue", identifier, actor, revision=2)
    assert continued.status_code == 200
    assert continued.json()["current_question_index"] == 1
    assert continued.json()["answers"] == ["Second answer."]
    assert harness.request("get", identifier, actor).json() == continued.json()
    rows = persisted(harness.factory)["interview_sessions"]
    assert rows[identifier]["user_id"] == harness.logins[actor].principal.user_id
    assert all(row["user_id"] is not None for row in rows.values())


def test_browser_owner_claims_cannot_change_application_creation_owner(harness):
    claimed = harness.logins[1].principal
    supplied = {**headers(harness.logins[0]), "X-User-ID": str(claimed.user_id),
                "X-Auth-Session-ID": str(claimed.auth_session_id)}
    response = harness.client.post(f"/api/sessions?user_id={claimed.user_id}", headers=supplied,
                                   json={"user_id": str(claimed.user_id)})
    assert response.status_code == 201
    identifier = UUID(response.json()["id"])
    assert persisted(harness.factory)["interview_sessions"][identifier]["user_id"] == harness.logins[0].principal.user_id
    assert_not_found(harness.request("get", identifier, 1))


@pytest.mark.parametrize("actor", [0, 1], ids=["user-a", "user-b"])
@pytest.mark.parametrize("operation", CORE_OPERATIONS)
def test_foreign_nonexistent_and_null_owned_resources_have_identical_404s(harness, actor, operation):
    foreign = harness.create(1 - actor)
    legacy = legacy_session(harness.factory)
    missing = uuid4()
    before = persisted(harness.factory)
    responses = [harness.request(operation, identifier, actor, question=99, revision=731)
                 for identifier in (foreign, missing, legacy)]
    for response in responses:
        assert_not_found(response)
    assert len({(response.status_code, response.content) for response in responses}) == 1
    assert persisted(harness.factory) == before
    assert before["interview_sessions"][legacy]["user_id"] is None
    assert StoredInterviewSession.__table__.c.user_id.nullable is True


@pytest.mark.parametrize("operation", CORE_OPERATIONS)
def test_foreign_root_precedes_children_revisions_measurements_and_completion(harness, operation):
    foreign = harness.create(1)
    foreign_measurement = measurement(harness.factory, foreign)
    with harness.factory.begin() as database:
        database.add(QuestionAttempt(
            session_id=foreign, question_index=0, attempt_number=7,
            answer_text="PRIVATE_FOREIGN_FINAL_ANSWER", measurement_id=foreign_measurement,
        ))
        database.execute(update(StoredInterviewSession).where(StoredInterviewSession.id == foreign).values(
            status="completed", current_question_index=5, completed_at=datetime.now(timezone.utc),
        ))
    before = persisted(harness.factory)
    statements = []

    def observed(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(harness.engine, "before_cursor_execute", observed)
    try:
        response = harness.request(operation, foreign, question=99, revision=999,
                                   measurement_id=foreign_measurement)
    finally:
        event.remove(harness.engine, "before_cursor_execute", observed)
    assert_not_found(response)
    assert "PRIVATE_FOREIGN_FINAL_ANSWER" not in response.text
    assert str(harness.logins[1].principal.user_id) not in response.text
    root_queries = [statement for statement in statements if "interview_sessions" in statement]
    assert len(root_queries) == 1
    where = root_queries[0].split("where", 1)[1]
    assert "interview_sessions.id" in where and "interview_sessions.user_id" in where
    assert not any("from question_attempts" in statement or "from transcription_measurements" in statement
                   for statement in statements)
    assert persisted(harness.factory) == before


@pytest.mark.parametrize("operation", CORE_OPERATIONS)
def test_owned_root_is_part_of_every_core_select_and_lock(harness, operation):
    identifier = harness.create()
    if operation == "continue":
        assert harness.request("submit", identifier).status_code == 201
    queries = []

    def observed(connection, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if lowered.startswith("select") and "from interview_sessions" in lowered:
            queries.append((lowered, parameters))

    event.listen(harness.engine, "before_cursor_execute", observed)
    try:
        response = harness.request(operation, identifier, revision=1 if operation == "continue" else 0)
    finally:
        event.remove(harness.engine, "before_cursor_execute", observed)
    assert response.status_code == (201 if operation == "submit" else 200)
    assert queries
    for statement, parameters in queries:
        where = statement.split("where", 1)[1]
        assert "interview_sessions.id" in where and "interview_sessions.user_id" in where
        assert identifier in parameters.values()
        assert harness.logins[0].principal.user_id in parameters.values()
    locks = [statement for statement, _ in queries if "for update" in statement]
    assert bool(locks) is (operation in ("submit", "continue"))


def test_foreign_measurements_never_attach_and_owned_provenance_is_preserved(harness):
    owned, foreign = harness.create(), harness.create(1)
    owned_measurement = measurement(harness.factory, owned)
    foreign_measurement = measurement(harness.factory, foreign)
    wrong_question = measurement(harness.factory, owned, question=1)
    before = persisted(harness.factory)
    failures = [harness.request("submit", owned, measurement_id=identifier)
                for identifier in (foreign_measurement, wrong_question, uuid4())]
    assert all(response.status_code == 409 for response in failures)
    assert all(response.json() == {"detail": "Measurement cannot be attached to this answer."} for response in failures)
    assert len({response.content for response in failures}) == 1
    assert persisted(harness.factory) == before
    accepted = harness.request("submit", owned, measurement_id=owned_measurement,
                               answer="Edited answer does not replace original measurements.")
    assert accepted.status_code == 201
    assert accepted.json()["attempt"]["measurement_id"] == str(owned_measurement)
    after = persisted(harness.factory)
    assert after["transcription_measurements"] == before["transcription_measurements"]
    assert after["question_attempts"][UUID(accepted.json()["attempt"]["id"])]["measurement_id"] == owned_measurement
    duplicate = harness.request("submit", owned, revision=1, measurement_id=owned_measurement)
    assert duplicate.status_code == 409
    assert duplicate.json() == failures[0].json()
    assert persisted(harness.factory) == after


def test_mandatory_principal_cannot_be_replaced_by_an_anonymous_or_claimed_owner(harness):
    with pytest.raises(TypeError):
        InterviewSessionService(harness.factory)
    for claimed in (None, harness.logins[0].principal.user_id, str(harness.logins[0].principal.user_id),
                    {"user_id": harness.logins[0].principal.user_id}):
        with pytest.raises(TypeError):
            InterviewSessionService(harness.factory, claimed)
    service = InterviewSessionService(harness.factory, harness.logins[0].principal)
    created = service.start()
    assert persisted(harness.factory)["interview_sessions"][created.id]["user_id"] == harness.logins[0].principal.user_id


def test_services_are_request_scoped_and_never_reuse_another_requests_principal(harness, monkeypatch):
    own = (harness.create(), harness.create(1))
    constructed = []

    def owner_service(factory, principal):
        service = InterviewSessionService(factory, principal)
        constructed.append((factory, principal, service))
        return service

    monkeypatch.setattr(session_routes, "InterviewSessionService", owner_service)
    for actor in (0, 1, 0):
        assert harness.request("get", own[actor], actor).status_code == 200
    assert len(constructed) == 3
    assert len({id(service) for _, _, service in constructed}) == 3
    assert [principal.user_id for _, principal, _ in constructed] == [
        harness.logins[actor].principal.user_id for actor in (0, 1, 0)
    ]
    assert all(factory is harness.factory and type(principal) is AuthenticatedPrincipal
               for factory, principal, _ in constructed)


@pytest.mark.parametrize("operation", ("create", *CORE_OPERATIONS))
def test_every_core_route_requires_authentication(harness, operation):
    foreign = harness.create(1)
    response = (harness.client.post("/api/sessions") if operation == "create"
                else harness.request(operation, foreign, auth_headers={}))
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("failure,status,detail", [
    ("missing", 401, "Authentication required."),
    ("invalid", 401, "Authentication required."),
    ("revoked", 401, "Authentication required."),
    ("expired", 401, "Authentication required."),
    ("wrong-context", 403, "Invalid authentication request context."),
    ("missing-context", 403, "Invalid authentication request context."),
    ("account-switch", 403, "Invalid authentication request context."),
    ("unavailable", 503, "Authentication is temporarily unavailable."),
])
def test_authentication_failures_precede_owner_operations(harness, monkeypatch, failure, status, detail):
    foreign = harness.create(1)
    supplied = headers(harness.logins[0])
    if failure == "missing":
        supplied = {}
    elif failure == "invalid":
        supplied["Cookie"] = f"{AUTH_SESSION_COOKIE_NAME}=unrecognized-credential"
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
    elif failure == "account-switch":
        supplied = {**headers(harness.logins[1]), AUTH_REQUEST_CONTEXT_HEADER: harness.logins[0].principal.request_context}
    elif failure == "unavailable":
        def unavailable():
            raise RuntimeError(PRIVATE_DETAIL)
        monkeypatch.setattr(auth_http, "get_database_session_factory", unavailable)
    queries = []

    def observed(connection, cursor, statement, parameters, context, executemany):
        if "interview_sessions" in statement.lower():
            queries.append(statement)

    event.listen(harness.engine, "before_cursor_execute", observed)
    try:
        response = harness.request("get", foreign, auth_headers=supplied)
    finally:
        event.remove(harness.engine, "before_cursor_execute", observed)
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert response.headers["Cache-Control"] == "no-store"
    assert queries == []
    for private in (PRIVATE_DETAIL, harness.logins[0].credential, harness.logins[0].principal.request_context):
        assert private not in response.text


@pytest.mark.parametrize("operation", ["submit", "continue"])
def test_foreign_operation_does_not_lock_a_row_before_rejecting_it(harness, operation):
    foreign = harness.create(1)

    def bound_wait(connection):
        connection.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")

    with harness.factory.begin() as holder:
        holder.scalar(select(StoredInterviewSession).where(StoredInterviewSession.id == foreign).with_for_update())
        event.listen(harness.engine, "begin", bound_wait)
        try:
            response = harness.request(operation, foreign)
        finally:
            event.remove(harness.engine, "begin", bound_wait)
        assert_not_found(response)


@pytest.mark.parametrize("independent", [False, True], ids=["same-session-revision-race", "independent-users"])
def test_concurrent_core_writes_preserve_owner_isolation_and_existing_revision_semantics(harness, independent):
    first = harness.create()
    second = harness.create(1) if independent else first
    barrier = Barrier(2)
    observed = []

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "from interview_sessions" in lowered and "for update" in lowered:
            observed.append(statement)
            barrier.wait(timeout=10)

    event.listen(harness.engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(harness.request, "submit", first, 0, answer="Concurrent answer A."),
                pool.submit(harness.request, "submit", second, 1 if independent else 0, answer="Concurrent answer B."),
            ]
            responses = [future.result(timeout=15) for future in futures]
    finally:
        event.remove(harness.engine, "before_cursor_execute", synchronize)
    assert len(observed) == 2
    if independent:
        assert [response.status_code for response in responses] == [201, 201]
        for actor, identifier, answer in ((0, first, "Concurrent answer A."), (1, second, "Concurrent answer B.")):
            listed = harness.request("list", identifier, actor)
            assert listed.status_code == 200
            assert [(row["attempt_number"], row["answer"]) for row in listed.json()] == [(1, answer)]
            assert_not_found(harness.request("get", identifier, 1 - actor))
    else:
        assert sorted(response.status_code for response in responses) == [201, 409]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json() == {"detail": "Attempt revision does not match the current question."}
        listed = harness.request("list", first).json()
        assert len(listed) == 1 and listed[0]["attempt_number"] == 1
        assert listed[0]["answer"] in {"Concurrent answer A.", "Concurrent answer B."}


def test_runtime_has_no_anonymous_provider_service_or_optional_principal_path():
    obsolete = (
        "TransitionalProviderSessionService", "_TransitionalProviderPersistence",
        "get_transitional_provider_session_service", "TransitionalProviderService", "_SessionPersistence",
    )
    runtime_root = Path(session_module.__file__).parent
    for path in runtime_root.rglob("*.py"):
        source = path.read_text()
        for name in obsolete:
            assert name not in source, f"{path.name} retains the obsolete {name} compatibility path."
    assert InterviewSessionService.__bases__ == (object,)
    constructor = inspect.signature(InterviewSessionService.__init__)
    assert tuple(constructor.parameters) == ("self", "session_factory", "principal")
    assert constructor.parameters["principal"].default is inspect.Parameter.empty
    assert get_type_hints(InterviewSessionService.__init__)["principal"] is AuthenticatedPrincipal
    predicate_source = inspect.getsource(InterviewSessionService._session_predicate)
    assert "StoredInterviewSession.id" in predicate_source
    assert "StoredInterviewSession.user_id" in predicate_source
    assert "self._principal.user_id" in predicate_source


def test_all_session_and_history_routes_use_owned_authenticated_dependencies():
    from app.history_routes import get_history_service

    def dependency_calls(dependant):
        result = set()
        for dependency in dependant.dependencies:
            result.add(dependency.call)
            result.update(dependency_calls(dependency))
        return result

    core = {
        session_routes.start_session, session_routes.get_session, session_routes.submit_attempt,
        session_routes.get_attempts, session_routes.continue_question, session_routes.get_comparison,
    }
    providers = {session_routes.accept_audio, session_routes.transcribe_audio, session_routes.diagnose_attempt}
    seen_core, seen_providers, seen_history = set(), set(), set()
    for route in iter_route_contexts(app.routes):
        if not hasattr(route, "dependant"):
            continue
        dependencies = dependency_calls(route.dependant)
        if route.endpoint in core | providers:
            direct = {dependency.call for dependency in route.dependant.dependencies}
            assert session_routes.get_session_service in direct
            signature = inspect.signature(route.endpoint)
            assert signature.parameters["sessions"].annotation == session_routes.SessionService
        if route.endpoint in core:
            seen_core.add(route.endpoint)
            assert session_routes.get_session_service in dependencies
            assert auth_http.require_authenticated_principal in dependencies
            assert auth_http.get_auth_session_store in dependencies
        elif route.endpoint in providers:
            seen_providers.add(route.endpoint)
            assert session_routes.get_session_service in dependencies
            assert auth_http.require_authenticated_principal in dependencies
            assert auth_http.get_auth_session_store in dependencies
            if route.endpoint in (session_routes.transcribe_audio, session_routes.diagnose_attempt):
                assert auth_http.require_authenticated_principal in direct
                assert auth_http.get_auth_session_store in direct
                assert signature.parameters["principal"].annotation == auth_http.AuthenticatedPrincipalDependency
                assert signature.parameters["auth_store"].annotation == auth_http.AuthSessionStoreDependency
        elif route.path in ("/api/history/summaries", "/api/sessions/{session_id}/history-detail"):
            seen_history.add((route.path, tuple(sorted(route.methods))))
            assert get_history_service in dependencies
            assert session_routes.get_session_service not in dependencies
            assert auth_http.require_authenticated_principal in dependencies
            assert auth_http.get_auth_session_store in dependencies
    assert seen_core == core and len(seen_core) == 6
    assert seen_providers == providers and len(seen_providers) == 3
    assert seen_history == {
        ("/api/history/summaries", ("GET",)), ("/api/history/summaries", ("POST",)),
        ("/api/sessions/{session_id}/history-detail", ("GET",)),
    }
