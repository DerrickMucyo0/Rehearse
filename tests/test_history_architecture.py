"""Offline History authentication, pagination parsing and composition contracts."""

import base64
from datetime import datetime, timedelta, timezone
import inspect
import json
from typing import get_type_hints
from uuid import UUID, uuid4

from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
import httpx
import psycopg
import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app import auth_http, database, history, history_routes, session_routes
from app.auth import AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind
from app.main import app

TOKEN = "PRIVATE_HISTORY_AUTH_CREDENTIAL"
CONTEXT = "PRIVATE_HISTORY_LOGIN_CONTEXT"
PRIVATE = "PRIVATE_HISTORY_FAILURE_OR_CURSOR_CONTENT"
PRINCIPAL = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context=CONTEXT)
IDENTIFIER = UUID("abcdefab-1234-4567-89ab-abcdefabcdef")
CREATED_AT = datetime(2026, 2, 3, 4, 5, 6, 123456, tzinfo=timezone.utc)
AUTH_FAILURES = {
    "missing": (401, "Authentication required."),
    "unauthenticated": (401, "Authentication required."),
    "context": (403, "Invalid authentication request context."),
    "unavailable": (503, "Authentication is temporarily unavailable."),
    "unexpected": (503, "Authentication is temporarily unavailable."),
}


@pytest.fixture(autouse=True)
def no_database_or_provider_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline History architecture tests must not connect to a database or provider.")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)


def dependency_calls(dependant):
    calls = set()
    for dependency in dependant.dependencies:
        calls.add(dependency.call)
        calls.update(dependency_calls(dependency))
    return calls


def test_every_backend_data_route_is_authenticated_and_only_health_is_public():
    expected = {
        ("/api/health", "GET"), ("/api/sessions", "POST"),
        ("/api/sessions/{session_id}", "GET"),
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
    observed = set()
    for route in iter_route_contexts(app.routes):
        if not hasattr(route, "dependant"):
            continue
        observed.update((route.path, method) for method in route.methods)
        calls = dependency_calls(route.dependant)
        if route.path == "/api/health":
            assert calls == set()
        else:
            assert auth_http.require_authenticated_principal in calls
            assert auth_http.get_auth_session_store in calls
    assert observed == expected


def test_history_service_requires_an_exact_principal_without_anonymous_constructor():
    constructor = inspect.signature(history.HistoryReadService.__init__)
    assert tuple(constructor.parameters) == ("self", "session_factory", "principal")
    assert constructor.parameters["principal"].default is inspect.Parameter.empty
    assert get_type_hints(history.HistoryReadService.__init__)["principal"] is AuthenticatedPrincipal

    def forbidden_factory():
        raise AssertionError("Invalid principals must not create a database session.")

    with pytest.raises(TypeError):
        history.HistoryReadService(forbidden_factory)
    for claimed in (None, PRINCIPAL.user_id, str(PRINCIPAL.user_id), {"user_id": PRINCIPAL.user_id}):
        with pytest.raises(TypeError):
            history.HistoryReadService(forbidden_factory, claimed)


def test_history_composition_is_request_scoped_and_defers_the_same_shared_sessionmaker(monkeypatch):
    helper = database.get_database_session_factory
    assert history_routes.get_database_session_factory is session_routes.get_database_session_factory is helper
    assert auth_http.get_database_session_factory is helper
    factory = sessionmaker()
    looked_up, constructed = [], []

    def shared_factory():
        looked_up.append(factory)
        return factory

    def capture(operation_factory, principal):
        service = object()
        constructed.append((operation_factory, principal, service))
        return service

    def forbidden(*args, **kwargs):
        raise AssertionError("History composition must reuse the shared database factory.")

    monkeypatch.setattr(database, "create_database_engine", forbidden)
    monkeypatch.setattr(database, "create_session_factory", forbidden)
    monkeypatch.setattr(history_routes, "get_database_session_factory", shared_factory)
    monkeypatch.setattr(history_routes, "HistoryReadService", capture)
    replacement = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context="replacement-context")
    services = [history_routes.get_history_service(principal) for principal in (PRINCIPAL, replacement, PRINCIPAL)]
    assert looked_up == []
    assert len({id(service) for service in services}) == 3
    assert [principal for _, principal, _ in constructed] == [PRINCIPAL, replacement, PRINCIPAL]
    assert [service for _, _, service in constructed] == services
    sessions = []
    try:
        for operation_factory, _, _ in constructed:
            sessions.append(operation_factory())
        assert looked_up == [factory, factory, factory]
        assert all(isinstance(session, Session) and session.bind is None for session in sessions)
        assert len({id(session) for session in sessions}) == 3
    finally:
        for session in sessions:
            session.close()


class Store:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def resolve(self, *, credential):
        self.calls.append(credential)
        if self.failure is not None:
            raise self.failure
        return PRINCIPAL


def private_headers():
    return {
        "Cookie": f"{auth_http.AUTH_SESSION_COOKIE_NAME}={TOKEN}",
        auth_http.AUTH_REQUEST_CONTEXT_HEADER: CONTEXT,
    }


@pytest.mark.parametrize("operation", ["discovery", "batch", "detail"])
@pytest.mark.parametrize("failure", list(AUTH_FAILURES))
def test_history_auth_failures_preserve_fixed_private_bodies_before_service_construction(
    monkeypatch, caplog, capsys, operation, failure,
):
    error = {
        "unauthenticated": AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED),
        "unavailable": AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE),
        "unexpected": RuntimeError(PRIVATE + TOKEN + CONTEXT),
    }.get(failure)
    store = Store(error)
    supplied = {} if failure == "missing" else private_headers()
    if failure == "context":
        supplied[auth_http.AUTH_REQUEST_CONTEXT_HEADER] = "wrong-context"

    def forbidden(*args, **kwargs):
        raise AssertionError("Authentication failures must precede History service construction.")

    monkeypatch.delitem(app.dependency_overrides, history_routes.get_history_service, raising=False)
    monkeypatch.delitem(app.dependency_overrides, auth_http.require_authenticated_principal, raising=False)
    monkeypatch.setitem(app.dependency_overrides, auth_http.get_auth_session_store, lambda: store)
    monkeypatch.setattr(history_routes, "HistoryReadService", forbidden)
    with TestClient(app, raise_server_exceptions=False) as client:
        if operation == "batch":
            response = client.post("/api/history/summaries", headers=supplied, json={"session_ids": [str(IDENTIFIER)]})
        else:
            path = "/api/history/summaries" if operation == "discovery" else f"/api/sessions/{IDENTIFIER}/history-detail"
            response = client.get(path, headers=supplied)
    status, detail = AUTH_FAILURES[failure]
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert response.headers["Cache-Control"] == "no-store"
    assert store.calls == ([] if failure == "missing" else [TOKEN])
    captured = capsys.readouterr()
    for value in (PRIVATE, TOKEN, CONTEXT, str(PRINCIPAL.user_id), str(PRINCIPAL.auth_session_id)):
        assert value not in response.text + caplog.text + captured.out + captured.err


def raw_cursor(payload):
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def test_cursor_contains_only_canonical_utc_time_and_session_uuid():
    offset = timezone(timedelta(hours=5))
    cursor = history._encode_cursor(CREATED_AT.astimezone(offset), IDENTIFIER)
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert decoded == [CREATED_AT.isoformat(), str(IDENTIFIER)]
    assert history._decode_cursor(cursor) == (CREATED_AT, IDENTIFIER)
    assert "=" not in cursor
    assert str(PRINCIPAL.user_id) not in json.dumps(decoded)
    assert TOKEN not in cursor and CONTEXT not in cursor


@pytest.mark.parametrize("cursor", [
    "", " ", "!" + PRIVATE, "\x00" + PRIVATE,
    raw_cursor({"user_id": str(PRINCIPAL.user_id)}),
    raw_cursor([]), raw_cursor([CREATED_AT.isoformat()]),
    raw_cursor([CREATED_AT.isoformat(), str(IDENTIFIER), str(PRINCIPAL.user_id)]),
    raw_cursor([None, str(IDENTIFIER)]), raw_cursor([CREATED_AT.isoformat(), 1]),
    raw_cursor([CREATED_AT.replace(tzinfo=None).isoformat(), str(IDENTIFIER)]),
    raw_cursor([CREATED_AT.isoformat(), PRIVATE]),
    raw_cursor([CREATED_AT.isoformat().replace("+00:00", "Z"), str(IDENTIFIER)]),
    raw_cursor([CREATED_AT.isoformat(), str(IDENTIFIER).upper()]),
    raw_cursor([CREATED_AT.isoformat(), str(IDENTIFIER)]) + "=",
])
def test_cursor_rejects_malformed_or_noncanonical_facts_without_echoing_them(cursor):
    with pytest.raises(history.HistoryCursorError) as caught:
        history._decode_cursor(cursor)
    assert str(caught.value) == "Invalid history request."
    assert caught.value.__cause__ is None
    assert PRIVATE not in str(caught.value)
    if cursor.strip():
        assert cursor not in str(caught.value)


@pytest.mark.parametrize("limit", [0, -1, 21, True, "10", 1.5])
def test_discovery_query_has_strict_bounded_limits(limit):
    with pytest.raises(ValidationError):
        history.HistoryDiscoveryQuery(limit=limit)


def test_discovery_query_default_and_page_contract_are_bounded_and_owner_free():
    assert history.HistoryDiscoveryQuery().limit == 10
    assert history.HISTORY_DISCOVERY_MAX_LIMIT == 20
    assert history.HistoryDiscoveryQuery(limit=1).limit == 1
    assert history.HistoryDiscoveryQuery(limit=20).limit == 20
    assert set(history.HistorySummaryPage.model_fields) == {"items", "next_cursor"}
    assert history.HistorySummaryPage(items=[], next_cursor=None).model_dump() == {"items": [], "next_cursor": None}


def test_public_health_remains_unchanged_without_authentication_or_database_configuration(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Public health must not authenticate or read persisted data.")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    for dependency in (
        auth_http.require_authenticated_principal, auth_http.get_auth_session_store,
        session_routes.get_session_service, history_routes.get_history_service,
    ):
        monkeypatch.setitem(app.dependency_overrides, dependency, forbidden)
    with TestClient(app) as client:
        response = client.get("/api/health")
    expected = b'{"status":"ok","service":"rehearse-api"}'
    assert response.status_code == 200 and response.content == expected
    assert dict(response.headers) == {"content-length": str(len(expected)), "content-type": "application/json"}
