"""HTTP authentication boundary and owned core-route response contracts.

The probe app exists only in this module. Stores are injected; the one PostgreSQL
integration reuses the dedicated fixture and checks that auth never reads history.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import inspect
from threading import Barrier
import traceback
from typing import get_args
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app import auth_http
from app.auth import (
    AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind,
    AuthSessionStore, IssuedAuthSession, VerifiedExternalIdentity,
)
from app.auth_http import (
    AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME, AuthenticatedPrincipalDependency,
    get_auth_session_store, require_authenticated_principal,
)
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database import DatabaseConfigurationError
from app.database_models import AuthSession, StoredInterviewSession, User

TOKEN_A = "PRIVATE_BROWSER_TOKEN_A_" + "a" * 43
TOKEN_B = "PRIVATE_BROWSER_TOKEN_B_" + "b" * 43
CONTEXT_A = "PRIVATE_REQUEST_CONTEXT_A_" + "a" * 43
CONTEXT_B = "PRIVATE_REQUEST_CONTEXT_B_" + "b" * 43
PRIVATE_DETAIL = "PRIVATE_DATABASE_OR_PROVIDER_ERROR_SENTINEL"
PRINCIPAL_A = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context=CONTEXT_A)
PRINCIPAL_B = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context=CONTEXT_B)
UNSET = object()
HTTP_FAILURES = {
    AuthenticationFailureKind.UNAUTHENTICATED: (401, "Authentication required."),
    AuthenticationFailureKind.INVALID_REQUEST_CONTEXT: (403, "Invalid authentication request context."),
    AuthenticationFailureKind.UNAVAILABLE: (503, "Authentication is temporarily unavailable."),
}


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Provider requests are forbidden in HTTP authentication tests")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


class FakeStore:
    def __init__(self):
        self.principals = {TOKEN_A: PRINCIPAL_A, TOKEN_B: PRINCIPAL_B}
        self.calls = []
        self.error = None
        self.output = UNSET
        self.barrier = None

    def resolve(self, *, credential):
        self.calls.append(credential)
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        if self.error is not None:
            raise self.error
        if self.output is not UNSET:
            return self.output
        if credential not in self.principals:
            raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
        return self.principals[credential]

    def create(self, **kwargs):
        raise AssertionError("Request authentication must only resolve credentials")

    def revoke(self, **kwargs):
        raise AssertionError("Request authentication must not revoke sessions")

    def revalidate(self, **kwargs):
        raise AssertionError("Resolution already checks the live local authentication session")


class RevalidationOnlyStore:
    def __init__(self, error=None):
        self.error = error
        self.principals = []

    def revalidate(self, *, principal):
        self.principals.append(principal)
        if self.error is not None:
            raise self.error

    def resolve(self, **kwargs):
        raise AssertionError("Post-provider revalidation must not resolve another credential.")

    def create(self, **kwargs):
        raise AssertionError("Post-provider revalidation must not issue another login.")

    def revoke(self, **kwargs):
        raise AssertionError("Post-provider revalidation must not revoke a login.")

    @property
    def credential(self):
        raise AssertionError("Post-provider revalidation must not read a raw credential.")


def probe_app(store=UNSET):
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    observed = []

    @application.get("/probe")
    def probe(principal: AuthenticatedPrincipalDependency):
        observed.append(principal)
        # Deliberately expose only UUIDs; the context is an internal binding value.
        return {"user_id": str(principal.user_id), "auth_session_id": str(principal.auth_session_id)}

    if store is not UNSET:
        application.dependency_overrides[get_auth_session_store] = lambda: store
    return application, observed


@dataclass
class Harness:
    application: FastAPI
    client: TestClient
    store: FakeStore
    observed: list


@pytest.fixture
def harness():
    store = FakeStore()
    application, observed = probe_app(store)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield Harness(application, client, store, observed)


def request_headers(credential=TOKEN_A, context=CONTEXT_A):
    headers = {}
    if credential is not None:
        headers["Cookie"] = f"{AUTH_SESSION_COOKIE_NAME}={credential}"
    if context is not None:
        headers[AUTH_REQUEST_CONTEXT_HEADER] = context
    return headers


def expected_identity(principal):
    return {"user_id": str(principal.user_id), "auth_session_id": str(principal.auth_session_id)}


def assert_http_failure(response, kind):
    status, message = HTTP_FAILURES[kind]
    assert response.status_code == status
    assert response.json() == {"detail": message}
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers
    for private in (TOKEN_A, TOKEN_B, CONTEXT_A, CONTEXT_B, PRIVATE_DETAIL, sha256(TOKEN_A.encode()).hexdigest()):
        assert private not in response.text


def direct_request(credential=TOKEN_A, context=CONTEXT_A):
    request = Request({"type": "http", "method": "GET", "path": "/probe", "headers": []})
    # Supply already-decoded values for Unicode/control-character edge cases that
    # HTTP clients cannot represent unchanged in ordinary wire header encoding.
    request._cookies = {} if credential is None else {AUTH_SESSION_COOKIE_NAME: credential}
    request._headers = {} if context is None else {AUTH_REQUEST_CONTEXT_HEADER: context}
    return request


def assert_private_http_exception(operation, kind, *private_values):
    with pytest.raises(HTTPException) as caught:
        operation()
    error = caught.value
    status, message = HTTP_FAILURES[kind]
    assert error.status_code == status
    assert error.detail == message
    assert error.headers["Cache-Control"] == "no-store"
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for private in (TOKEN_A, TOKEN_B, CONTEXT_A, CONTEXT_B, PRIVATE_DETAIL, *private_values):
        assert private not in str(error)
        assert private not in repr(error)
        assert private not in rendered
    return error


def assert_direct_failure(request, store, kind, *private_values):
    return assert_private_http_exception(
        lambda: require_authenticated_principal(request=request, response=Response(), store=store),
        kind, *private_values,
    )


def test_transport_names_are_fixed_and_fake_store_injection_returns_exact_frozen_principal(harness):
    assert AUTH_SESSION_COOKIE_NAME == "rehearse_auth_session"
    assert AUTH_REQUEST_CONTEXT_HEADER == "X-Rehearse-Auth-Context"
    response = harness.client.get("/probe", headers=request_headers())
    assert response.status_code == 200
    assert response.json() == expected_identity(PRINCIPAL_A)
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert harness.observed == [PRINCIPAL_A]
    assert harness.observed[0] is PRINCIPAL_A
    assert type(harness.observed[0]) is AuthenticatedPrincipal
    with pytest.raises(FrozenInstanceError):
        harness.observed[0].user_id = uuid4()
    with pytest.raises(FrozenInstanceError):
        harness.observed[0].request_context = "different-context"
    assert harness.store.calls == [TOKEN_A]
    assert TOKEN_A not in response.text and CONTEXT_A not in response.text


@pytest.mark.parametrize("credential", [None, "", " ", "\t", " \t "])
def test_missing_or_blank_cookie_is_unauthenticated_without_resolving(harness, credential):
    response = harness.client.get("/probe", headers=request_headers(credential=credential))
    assert_http_failure(response, AuthenticationFailureKind.UNAUTHENTICATED)
    assert harness.store.calls == []
    assert harness.observed == []


@pytest.mark.parametrize("credential", ["invalid-credential", "expired-credential", "revoked-credential"])
def test_invalid_expired_and_revoked_credentials_have_the_same_fixed_response(harness, credential):
    response = harness.client.get("/probe", headers=request_headers(credential=credential, context=None))
    assert_http_failure(response, AuthenticationFailureKind.UNAUTHENTICATED)
    assert harness.store.calls == [credential]
    assert harness.observed == []


@pytest.mark.parametrize("kind", list(HTTP_FAILURES))
def test_approved_store_failures_map_to_fixed_private_http_responses(harness, kind):
    harness.store.error = AuthenticationFailure(kind)
    response = harness.client.get("/probe", headers=request_headers())
    assert_http_failure(response, kind)
    assert harness.observed == []


@pytest.mark.parametrize("context", [None, "", " ", "\t", "wrong-context", CONTEXT_A.lower(), " " + CONTEXT_A, CONTEXT_A + " "])
def test_request_context_is_required_and_compared_without_case_or_whitespace_normalization(harness, context):
    response = harness.client.get("/probe", headers=request_headers(context=context))
    assert_http_failure(response, AuthenticationFailureKind.INVALID_REQUEST_CONTEXT)
    assert harness.store.calls == [TOKEN_A]
    assert harness.observed == []


@pytest.mark.parametrize("headers,query", [
    ({AUTH_REQUEST_CONTEXT_HEADER: CONTEXT_A}, ""),
    ({"Authorization": "Bearer " + TOKEN_A}, ""),
    ({"X-User-ID": str(PRINCIPAL_A.user_id)}, ""),
    ({"X-Auth-Session-ID": str(PRINCIPAL_A.auth_session_id)}, ""),
    ({"X-Interview-Session-ID": str(uuid4())}, ""),
    ({"Cookie": "other_session=" + TOKEN_A, AUTH_REQUEST_CONTEXT_HEADER: CONTEXT_A}, ""),
    ({AUTH_REQUEST_CONTEXT_HEADER: CONTEXT_A}, f"?{AUTH_SESSION_COOKIE_NAME}={TOKEN_A}"),
    ({AUTH_REQUEST_CONTEXT_HEADER: CONTEXT_A}, f"?credential={TOKEN_A}&user_id={PRINCIPAL_A.user_id}"),
    ({"Authorization": "Bearer " + TOKEN_A, "X-User-ID": str(PRINCIPAL_A.user_id),
      "X-Auth-Session-ID": str(PRINCIPAL_A.auth_session_id), AUTH_REQUEST_CONTEXT_HEADER: CONTEXT_A}, ""),
])
def test_headers_other_cookies_and_query_claims_cannot_replace_the_auth_cookie(harness, headers, query):
    response = harness.client.get("/probe" + query, headers=headers)
    assert_http_failure(response, AuthenticationFailureKind.UNAUTHENTICATED)
    assert harness.store.calls == []
    assert harness.observed == []


def test_cross_tab_account_switch_requires_the_new_login_context(harness):
    initial = harness.client.get("/probe", headers=request_headers())
    stale_tab = harness.client.get("/probe", headers=request_headers(TOKEN_B, CONTEXT_A))
    switched = harness.client.get("/probe", headers=request_headers(TOKEN_B, CONTEXT_B))
    assert initial.status_code == switched.status_code == 200
    assert initial.json() == expected_identity(PRINCIPAL_A)
    assert_http_failure(stale_tab, AuthenticationFailureKind.INVALID_REQUEST_CONTEXT)
    assert switched.json() == expected_identity(PRINCIPAL_B)
    assert harness.observed == [PRINCIPAL_A, PRINCIPAL_B]
    assert harness.store.calls == [TOKEN_A, TOKEN_B, TOKEN_B]


def test_sequential_requests_resolve_live_credentials_each_time_without_cached_principals(harness):
    first = harness.client.get("/probe", headers=request_headers())
    second = harness.client.get("/probe", headers=request_headers(TOKEN_B, CONTEXT_B))
    del harness.store.principals[TOKEN_A]
    revoked = harness.client.get("/probe", headers=request_headers())
    still_live = harness.client.get("/probe", headers=request_headers(TOKEN_B, CONTEXT_B))
    assert first.json() == expected_identity(PRINCIPAL_A)
    assert second.json() == still_live.json() == expected_identity(PRINCIPAL_B)
    assert_http_failure(revoked, AuthenticationFailureKind.UNAUTHENTICATED)
    assert harness.store.calls == [TOKEN_A, TOKEN_B, TOKEN_A, TOKEN_B]
    assert harness.observed == [PRINCIPAL_A, PRINCIPAL_B, PRINCIPAL_B]


def test_concurrent_requests_for_different_users_and_sessions_remain_isolated():
    store = FakeStore()
    store.barrier = Barrier(2)
    application, observed = probe_app(store)
    with TestClient(application) as first, TestClient(application) as second:
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [
                pool.submit(first.get, "/probe", headers=request_headers(TOKEN_A, CONTEXT_A)),
                pool.submit(second.get, "/probe", headers=request_headers(TOKEN_B, CONTEXT_B)),
            ]
            responses = [request.result(timeout=15) for request in requests]
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json() for response in responses] == [expected_identity(PRINCIPAL_A), expected_identity(PRINCIPAL_B)]
    assert set(store.calls) == {TOKEN_A, TOKEN_B} and len(store.calls) == 2
    assert set(observed) == {PRINCIPAL_A, PRINCIPAL_B} and len(observed) == 2


@pytest.mark.parametrize("supplied,stored,matches", [
    ("caf\u00e9", "caf\u00e9", True), ("caf\u00e9", "cafe\u0301", False),
    ("\u30ed\u30b0\u30a4\u30f3", "\u30ed\u30b0\u30a4\u30f3", True),
    (" Context ", " Context ", True), ("Context", " Context ", False),
])
def test_context_comparison_uses_exact_utf8_bytes_and_timing_safe_comparison(monkeypatch, supplied, stored, matches):
    principal = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context=stored)
    store = FakeStore()
    store.principals[TOKEN_A] = principal
    original = auth_http.secrets.compare_digest
    compared = []

    def compare(first, second):
        compared.append((first, second))
        return original(first, second)

    monkeypatch.setattr(auth_http.secrets, "compare_digest", compare)
    request, response = direct_request(context=supplied), Response()
    if matches:
        assert require_authenticated_principal(request=request, response=response, store=store) is principal
        assert response.headers["Cache-Control"] == "no-store"
    else:
        assert_direct_failure(request, store, AuthenticationFailureKind.INVALID_REQUEST_CONTEXT, supplied, stored)
    assert len(compared) == 1
    assert all(type(value) is bytes for value in compared[0])
    assert compared[0] in ((supplied.encode("utf-8"), stored.encode("utf-8")),
                           (stored.encode("utf-8"), supplied.encode("utf-8")))


def test_http_boundary_forwards_the_opaque_cookie_unchanged_without_hashing_or_decoding():
    credential = " opaque.token-with-exact-case+/= "
    store = FakeStore()
    store.principals = {credential: PRINCIPAL_A}
    principal = require_authenticated_principal(request=direct_request(credential=credential), response=Response(), store=store)
    assert principal is PRINCIPAL_A
    assert store.calls == [credential]


class DerivedPrincipal(AuthenticatedPrincipal):
    pass


@pytest.mark.parametrize("result", [
    pytest.param(None, id="null"), pytest.param(TOKEN_A, id="credential"),
    pytest.param(PRINCIPAL_A.user_id, id="uuid"),
    pytest.param({"user_id": PRINCIPAL_A.user_id, "auth_session_id": PRINCIPAL_A.auth_session_id,
                  "request_context": CONTEXT_A}, id="claims"),
    pytest.param(User(auth_provider="https://identity.example.test", provider_subject="subject"), id="user-model"),
    pytest.param(AuthSession(token_hash=b"x" * 32, request_context=CONTEXT_A), id="auth-model"),
    pytest.param(IssuedAuthSession(principal=PRINCIPAL_A, credential=TOKEN_A), id="issued-session"),
    pytest.param(DerivedPrincipal(user_id=PRINCIPAL_A.user_id, auth_session_id=PRINCIPAL_A.auth_session_id,
                                 request_context=CONTEXT_A), id="principal-subclass"),
])
def test_malformed_store_results_fail_closed_as_unavailable(harness, result):
    harness.store.output = result
    response = harness.client.get("/probe", headers=request_headers())
    assert_http_failure(response, AuthenticationFailureKind.UNAVAILABLE)
    assert harness.observed == []


def test_unexpected_store_details_never_reach_http_bodies_logs_or_exception_chains(harness, caplog, capsys):
    digest = sha256(TOKEN_A.encode()).hexdigest()
    harness.store.error = RuntimeError(PRIVATE_DETAIL + TOKEN_A + CONTEXT_A + digest)
    response = harness.client.get("/probe", headers=request_headers())
    assert_http_failure(response, AuthenticationFailureKind.UNAVAILABLE)
    assert_direct_failure(direct_request(), harness.store, AuthenticationFailureKind.UNAVAILABLE, digest)
    captured = capsys.readouterr()
    rendered = caplog.text + captured.out + captured.err + response.text
    for private in (PRIVATE_DETAIL, TOKEN_A, CONTEXT_A, digest):
        assert private not in rendered


@pytest.mark.parametrize("kind", list(HTTP_FAILURES))
def test_expected_auth_failures_become_sanitized_http_exceptions_without_chains(kind):
    store = FakeStore()
    store.error = AuthenticationFailure(kind)
    assert_direct_failure(direct_request(), store, kind)


@pytest.mark.parametrize("metadata", ["missing-kind", "string-kind", "object-kind", "subclass-kind-override"])
def test_malformed_or_subclassed_auth_failures_cannot_escape_fixed_unavailable_mapping(harness, metadata):
    class DerivedFailure(AuthenticationFailure):
        @property
        def kind(self):
            raise RuntimeError(PRIVATE_DETAIL)

    error = AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
    if metadata == "missing-kind":
        del error._kind
    elif metadata == "string-kind":
        error._kind = "unauthenticated"
    elif metadata == "object-kind":
        error._kind = object()
    else:
        error = DerivedFailure(AuthenticationFailureKind.UNAUTHENTICATED)
    error.args = (PRIVATE_DETAIL + TOKEN_A + CONTEXT_A,)
    harness.store.error = error
    response = harness.client.get("/probe", headers=request_headers())
    assert_http_failure(response, AuthenticationFailureKind.UNAVAILABLE)
    assert_direct_failure(direct_request(), harness.store, AuthenticationFailureKind.UNAVAILABLE)
    assert harness.observed == []


@pytest.mark.parametrize("error", [
    pytest.param(asyncio.CancelledError(), id="cancelled"),
    pytest.param(KeyboardInterrupt(), id="interrupt"), pytest.param(SystemExit(), id="exit"),
])
def test_baseexception_cancellation_and_shutdown_are_not_swallowed(error):
    store = FakeStore()
    store.error = error
    with pytest.raises(type(error)) as caught:
        require_authenticated_principal(request=direct_request(), response=Response(), store=store)
    assert caught.value is error


def test_auth_store_dependency_alias_preserves_the_existing_injectable_boundary():
    store_type, dependency = get_args(auth_http.AuthSessionStoreDependency)
    assert store_type is AuthSessionStore
    assert dependency.dependency is get_auth_session_store
    signature = inspect.signature(require_authenticated_principal)
    assert signature.parameters["store"].annotation == auth_http.AuthSessionStoreDependency


def test_post_provider_revalidation_passes_the_same_frozen_principal_without_reading_credentials(monkeypatch):
    store = RevalidationOnlyStore()

    def forbidden(*args, **kwargs):
        raise AssertionError("Post-provider revalidation must not inspect the browser cookie.")

    monkeypatch.setattr(auth_http, "_cookie_credential", forbidden)
    assert not inspect.iscoroutinefunction(auth_http.revalidate_authenticated_principal)
    assert tuple(inspect.signature(auth_http.revalidate_authenticated_principal).parameters) == ("principal", "store")
    assert auth_http.revalidate_authenticated_principal(principal=PRINCIPAL_A, store=store) is None
    assert store.principals == [PRINCIPAL_A]
    assert store.principals[0] is PRINCIPAL_A
    for field, replacement in (
        ("user_id", uuid4()), ("auth_session_id", uuid4()), ("request_context", "replacement-context"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(store.principals[0], field, replacement)


@pytest.mark.parametrize("kind", list(HTTP_FAILURES))
def test_post_provider_revalidation_maps_approved_failures_to_fixed_private_http_responses(kind):
    store = RevalidationOnlyStore(AuthenticationFailure(kind))
    assert_private_http_exception(
        lambda: auth_http.revalidate_authenticated_principal(principal=PRINCIPAL_A, store=store), kind,
    )
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @application.get("/post-provider")
    def release_result():
        auth_http.revalidate_authenticated_principal(principal=PRINCIPAL_A, store=store)
        raise AssertionError("A failed revalidation must not release a protected result.")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/post-provider")
    assert_http_failure(response, kind)
    assert len(store.principals) == 2
    assert all(principal is PRINCIPAL_A for principal in store.principals)


def test_post_provider_revalidation_discards_unexpected_sensitive_errors_without_chains_or_output(caplog, capsys):
    digest = sha256(TOKEN_A.encode()).hexdigest()
    store = RevalidationOnlyStore(RuntimeError(PRIVATE_DETAIL + TOKEN_A + CONTEXT_A + digest))
    assert_private_http_exception(
        lambda: auth_http.revalidate_authenticated_principal(principal=PRINCIPAL_A, store=store),
        AuthenticationFailureKind.UNAVAILABLE, digest,
    )
    assert len(store.principals) == 1 and store.principals[0] is PRINCIPAL_A
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert caplog.records == []


@pytest.mark.parametrize("metadata", ["missing-kind", "string-kind", "object-kind", "subclass-kind-override"])
def test_post_provider_revalidation_keeps_malformed_failure_metadata_in_fixed_unavailable_mapping(metadata):
    class DerivedFailure(AuthenticationFailure):
        @property
        def kind(self):
            raise RuntimeError(PRIVATE_DETAIL)

    error = AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
    if metadata == "missing-kind":
        del error._kind
    elif metadata == "string-kind":
        error._kind = "unauthenticated"
    elif metadata == "object-kind":
        error._kind = object()
    else:
        error = DerivedFailure(AuthenticationFailureKind.UNAUTHENTICATED)
    error.args = (PRIVATE_DETAIL + TOKEN_A + CONTEXT_A,)
    store = RevalidationOnlyStore(error)
    assert_private_http_exception(
        lambda: auth_http.revalidate_authenticated_principal(principal=PRINCIPAL_A, store=store),
        AuthenticationFailureKind.UNAVAILABLE,
    )
    assert len(store.principals) == 1 and store.principals[0] is PRINCIPAL_A


@pytest.mark.parametrize("error", [
    pytest.param(BaseException(PRIVATE_DETAIL), id="base-exception"),
    pytest.param(asyncio.CancelledError(), id="cancelled"),
    pytest.param(KeyboardInterrupt(), id="interrupt"), pytest.param(SystemExit(), id="exit"),
])
def test_post_provider_revalidation_propagates_cancellation_and_shutdown_unchanged(error):
    store = RevalidationOnlyStore(error)
    with pytest.raises(type(error)) as caught:
        auth_http.revalidate_authenticated_principal(principal=PRINCIPAL_A, store=store)
    assert caught.value is error
    assert len(store.principals) == 1 and store.principals[0] is PRINCIPAL_A


@pytest.mark.parametrize("credential", [None, "", " "])
def test_production_store_dependency_keeps_missing_cookie_unauthenticated_when_database_is_unset(monkeypatch, credential):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    application, observed = probe_app()
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/probe", headers=request_headers(credential=credential))
    assert_http_failure(response, AuthenticationFailureKind.UNAUTHENTICATED)
    assert observed == []


@pytest.mark.parametrize("source", ["configuration", "factory", "constructor"])
def test_production_store_setup_failures_are_private_unavailable_responses_without_exception_chains(monkeypatch, source, caplog, capsys):
    detail = PRIVATE_DETAIL + TOKEN_A + CONTEXT_A
    error = DatabaseConfigurationError(detail) if source == "configuration" else RuntimeError(detail)

    def failed(*args, **kwargs):
        raise error

    if source == "constructor":
        monkeypatch.setattr(auth_http, "get_database_session_factory", lambda: object())
        monkeypatch.setattr(auth_http, "PostgreSQLAuthSessionStore", failed)
    else:
        monkeypatch.setattr(auth_http, "get_database_session_factory", failed)
    application, observed = probe_app()
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/probe", headers=request_headers())
    assert_http_failure(response, AuthenticationFailureKind.UNAVAILABLE)
    assert_private_http_exception(lambda: get_auth_session_store(direct_request()), AuthenticationFailureKind.UNAVAILABLE)
    assert observed == []
    captured = capsys.readouterr()
    for private in (PRIVATE_DETAIL, TOKEN_A, CONTEXT_A):
        assert private not in caplog.text + captured.out + captured.err


@pytest.mark.parametrize("source", ["factory", "constructor"])
@pytest.mark.parametrize("error", [
    pytest.param(asyncio.CancelledError(), id="cancelled"),
    pytest.param(KeyboardInterrupt(), id="interrupt"), pytest.param(SystemExit(), id="exit"),
])
def test_production_store_dependency_propagates_cancellation_and_shutdown(monkeypatch, source, error):
    def failed(*args, **kwargs):
        raise error

    if source == "constructor":
        monkeypatch.setattr(auth_http, "get_database_session_factory", lambda: object())
        monkeypatch.setattr(auth_http, "PostgreSQLAuthSessionStore", failed)
    else:
        monkeypatch.setattr(auth_http, "get_database_session_factory", failed)
    with pytest.raises(type(error)) as caught:
        get_auth_session_store(direct_request())
    assert caught.value is error


def test_unencodable_received_context_fails_as_fixed_invalid_context():
    store = FakeStore()
    supplied = "private-context-\ud800"
    assert_direct_failure(direct_request(context=supplied), store, AuthenticationFailureKind.INVALID_REQUEST_CONTEXT)
    assert store.calls == [TOKEN_A]


def test_real_postgresql_store_resolves_revokes_and_expires_without_interview_ownership_queries(postgres_engine, postgres_session_factory):
    now = [datetime(2026, 1, 2, tzinfo=timezone.utc)]
    lifetime = timedelta(minutes=30)
    credentials, contexts = iter((TOKEN_A, TOKEN_B)), iter((CONTEXT_A, CONTEXT_B))
    store = PostgreSQLAuthSessionStore(
        postgres_session_factory, session_lifetime=lifetime, clock=lambda: now[0],
        credential_generator=lambda: next(credentials), request_context_generator=lambda: next(contexts),
    )
    users = [store.provision_user(identity=VerifiedExternalIdentity(issuer="https://identity.example.test", subject=subject))
             for subject in ("user-a", "user-b")]
    first, second = [store.create(user_id=user_id) for user_id in users]
    application, observed = probe_app(store)
    ownership_queries = []

    def forbid_ownership(connection, cursor, statement, parameters, context, executemany):
        if any(table in statement.lower() for table in ("interview_sessions", "question_attempts", "transcription_measurements")):
            ownership_queries.append(True)
            raise AssertionError("Authentication must not inspect interview ownership")

    event.listen(postgres_engine, "before_cursor_execute", forbid_ownership)
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            valid_a = client.get("/probe", headers=request_headers(TOKEN_A, CONTEXT_A))
            stale_tab = client.get("/probe", headers=request_headers(TOKEN_B, CONTEXT_A))
            valid_b = client.get("/probe", headers=request_headers(TOKEN_B, CONTEXT_B))
            assert valid_a.status_code == valid_b.status_code == 200
            assert valid_a.json() == expected_identity(first.principal)
            assert valid_b.json() == expected_identity(second.principal)
            assert_http_failure(stale_tab, AuthenticationFailureKind.INVALID_REQUEST_CONTEXT)
            store.revoke(auth_session_id=first.principal.auth_session_id)
            assert_http_failure(client.get("/probe", headers=request_headers(TOKEN_A, CONTEXT_A)),
                                AuthenticationFailureKind.UNAUTHENTICATED)
            assert client.get("/probe", headers=request_headers(TOKEN_B, CONTEXT_B)).json() == expected_identity(second.principal)
            now[0] += lifetime
            assert_http_failure(client.get("/probe", headers=request_headers(TOKEN_B, CONTEXT_B)),
                                AuthenticationFailureKind.UNAUTHENTICATED)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", forbid_ownership)
    assert ownership_queries == []
    assert observed == [first.principal, second.principal, second.principal]


def test_health_keeps_its_existing_bytes_and_headers_without_authentication(monkeypatch):
    from app.main import app

    def forbidden(*args, **kwargs):
        raise AssertionError("Anonymous health must not execute authentication.")

    monkeypatch.setitem(app.dependency_overrides, get_auth_session_store, forbidden)
    monkeypatch.setitem(app.dependency_overrides, require_authenticated_principal, forbidden)
    assert app.user_middleware == []
    with TestClient(app) as client:
        response = client.get("/api/health")
    expected = b'{"status":"ok","service":"rehearse-api"}'
    assert response.status_code == 200
    assert response.content == expected
    assert dict(response.headers) == {
        "content-length": str(len(expected)), "content-type": "application/json",
    }


def test_authenticated_session_routes_preserve_response_bytes_and_add_private_cache_headers(
    postgres_session_factory, authenticated_principal, monkeypatch,
):
    from app.main import app
    from app import session_routes
    from app.sessions import QUESTIONS

    store = FakeStore()
    store.principals = {TOKEN_A: authenticated_principal}
    monkeypatch.setattr(session_routes, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.setitem(app.dependency_overrides, get_auth_session_store, lambda: store)
    headers = request_headers(TOKEN_A, authenticated_principal.request_context)
    with TestClient(app) as client:
        created = client.post("/api/sessions", headers=headers)
        assert created.status_code == 201
        body = created.json()
        location = f"/api/sessions/{body['id']}"
        assert body == {
            "id": body["id"], "scenario_type": "job_interview",
            "question_engine": "live-ai-roleplay-v1", "total_questions": 5,
            "status": "active", "current_question_index": 0,
            "questions": list(QUESTIONS[:1]), "answers": [],
            "current_question_latest_attempt_number": 0, "current_question": QUESTIONS[0],
        }
        read = client.get(location, headers=headers)
    assert read.status_code == 200
    assert read.content == created.content
    baseline_headers = {
        "content-length": str(len(created.content)), "content-type": "application/json",
        "cache-control": "no-store",
    }
    assert dict(created.headers) == {**baseline_headers, "location": location}
    assert dict(read.headers) == baseline_headers
    assert store.calls == [TOKEN_A, TOKEN_A]
    with postgres_session_factory() as database:
        assert database.get(StoredInterviewSession, UUID(body["id"])).user_id == authenticated_principal.user_id


@pytest.mark.parametrize("constructor_lifetime", [
    pytest.param(timedelta(microseconds=1), id="tiny"),
    pytest.param(timedelta(hours=8), id="eight-hours"),
    pytest.param(timedelta(days=365), id="one-year"),
])
def test_http_store_constructor_lifetime_is_inert_for_stored_expiry(
    postgres_session_factory, monkeypatch, constructor_lifetime,
):
    """Constructor compatibility is not an approved product duration policy.

    Only issuance uses a constructor lifetime. Changing the HTTP store's value
    cannot replace the already-persisted expiry during resolve or revalidate.
    """
    now = [datetime(2026, 1, 2, tzinfo=timezone.utc)]
    issued_at = now[0]
    stored_lifetime = timedelta(minutes=30)
    issuer = PostgreSQLAuthSessionStore(
        postgres_session_factory, session_lifetime=stored_lifetime, clock=lambda: now[0],
        credential_generator=lambda: TOKEN_A, request_context_generator=lambda: CONTEXT_A,
    )
    user_id = issuer.provision_user(identity=VerifiedExternalIdentity(
        issuer="https://identity.example.test", subject="constructor-lifetime-audit",
    ))
    issued = issuer.create(user_id=user_id)

    def persisted_rows():
        with postgres_session_factory() as database:
            return [dict(row) for row in database.execute(select(AuthSession.__table__)).mappings()]

    before = persisted_rows()
    assert len(before) == 1
    assert before[0]["expires_at"] == issued_at + stored_lifetime
    constructed_lifetimes = []

    def forbid_issuance():
        raise AssertionError("The HTTP authentication store must not issue or rotate credentials.")

    def construct_reader(factory, *, session_lifetime):
        assert factory is postgres_session_factory
        constructed_lifetimes.append(session_lifetime)
        return PostgreSQLAuthSessionStore(
            factory, session_lifetime=session_lifetime, clock=lambda: now[0],
            credential_generator=forbid_issuance, request_context_generator=forbid_issuance,
        )

    monkeypatch.setattr(auth_http, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.setattr(auth_http, "PostgreSQLAuthSessionStore", construct_reader)
    monkeypatch.setattr(auth_http, "AUTH_SESSION_LIFETIME", constructor_lifetime)

    for offset in (timedelta(minutes=15), stored_lifetime - timedelta(microseconds=1)):
        now[0] = issued_at + offset
        reader = get_auth_session_store(direct_request())
        assert reader.resolve(credential=issued.credential) == issued.principal
        assert reader.revalidate(principal=issued.principal) is None
        assert require_authenticated_principal(
            request=direct_request(), response=Response(), store=reader,
        ) == issued.principal

    for offset in (stored_lifetime, stored_lifetime + timedelta(microseconds=1)):
        now[0] = issued_at + offset
        reader = get_auth_session_store(direct_request())
        for operation in (
            lambda: reader.resolve(credential=issued.credential),
            lambda: reader.revalidate(principal=issued.principal),
        ):
            with pytest.raises(AuthenticationFailure) as caught:
                operation()
            assert caught.value.kind is AuthenticationFailureKind.UNAUTHENTICATED
        assert_direct_failure(direct_request(), reader, AuthenticationFailureKind.UNAUTHENTICATED)

    assert constructed_lifetimes == [constructor_lifetime] * 4
    assert persisted_rows() == before
