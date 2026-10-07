"""Browser authentication lifecycle with inert OIDC and isolated PostgreSQL.

HTTP uses an HTTPS in-process ASGI client. The OIDC boundary is injected here;
the adapter's separate tests exercise signed local tokens and MockTransport.
Every real external HTTP transport is blocked in this module.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.cookies import SimpleCookie
from threading import Barrier, get_ident
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx
import httpx2
import pytest
from sqlalchemy import event, func, select

from app import auth_routes
from app.auth import (
    AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind,
    IssuedAuthSession, VerifiedExternalIdentity,
)
from app.auth_http import (
    AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME,
    AuthenticatedPrincipalDependency, get_auth_session_store,
)
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database_models import AuthSession, OIDCLoginTransaction, User
from app.oidc_failure import OIDCFailure, OIDCFailureKind
from app.oidc_login import (
    ConsumedLoginTransaction, IssuedLoginTransaction, PostgreSQLOIDCLoginTransactionStore,
)
from app.oidc_verifier import OIDCConfiguration

ORIGIN = "https://rehearse.example.test"
ISSUER = "https://identity.example.test/tenant"
AUTHORIZATION_URI = "https://identity.example.test/authorize"
REDIRECT_URI = ORIGIN + "/api/auth/callback"
CLIENT_ID = "synthetic-rehearse-client"
CLIENT_SECRET = "SYNTHETIC_CLIENT_SECRET_DO_NOT_LOG"
PRIVATE = "PRIVATE_AUTH_LIFECYCLE_DATABASE_OR_PROVIDER_FAILURE"
CODE_A = "SYNTHETIC_AUTHORIZATION_CODE_A"
CODE_B = "SYNTHETIC_AUTHORIZATION_CODE_B"
SUBJECT_A = "Opaque case-sensitive subject A"
SUBJECT_B = "Opaque case-sensitive subject B"
NOW = datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)
LOGIN_FAILURE = {"detail": "Unable to complete login."}
UNAVAILABLE = {"detail": "Authentication is temporarily unavailable."}
UNAUTHENTICATED = {"detail": "Authentication required."}
INVALID_CONTEXT = {"detail": "Invalid authentication request context."}


@pytest.fixture(autouse=True)
def forbid_external_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Auth route tests must not contact external providers.")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.HTTPTransport, "handle_request", forbidden)
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", forbidden_async)


def settings(ttl=3600, issuer=ISSUER):
    return auth_routes.AuthSettings(
        oidc=OIDCConfiguration(
            issuer=issuer, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
        ),
        app_origin=ORIGIN, session_ttl_seconds=ttl,
    )


class Transactions:
    def __init__(self):
        self.pending = {}
        self.created = []
        self.consumed = []
        self.error = None

    def create_login_transaction(self):
        if self.error is not None:
            raise self.error
        ordinal = len(self.created) + 1
        issued = IssuedLoginTransaction(
            state=f"SYNTHETIC_STATE_{ordinal}_" + "s" * 43,
            nonce=f"SYNTHETIC_NONCE_{ordinal}_" + "n" * 43,
            code_challenge="c" * 43,
        )
        self.pending[issued.state] = ConsumedLoginTransaction(
            nonce=issued.nonce, code_verifier=f"SYNTHETIC_VERIFIER_{ordinal}_" + "v" * 43,
        )
        self.created.append(issued)
        return issued

    def consume_login_transaction(self, state):
        self.consumed.append(state)
        if self.error is not None:
            raise self.error
        consumed = self.pending.pop(state, None)
        if consumed is None:
            raise OIDCFailure(OIDCFailureKind.INVALID_STATE)
        return consumed


class Verifier:
    def __init__(self):
        self.authorization_calls = []
        self.verification_calls = []
        self.error = None
        self.authorization_error = None
        self.identity = None
        self.on_verify = None

    async def authorization_url(self, *, transaction):
        self.authorization_calls.append(transaction)
        if self.authorization_error is not None:
            raise self.authorization_error
        return AUTHORIZATION_URI + "?" + urlencode({
            "response_type": "code", "scope": "openid", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI, "state": transaction.state,
            "nonce": transaction.nonce, "code_challenge": transaction.code_challenge,
            "code_challenge_method": "S256",
        })

    async def verify_callback(self, **kwargs):
        self.verification_calls.append(kwargs)
        if self.on_verify is not None:
            self.on_verify(kwargs)
        if self.error is not None:
            raise self.error
        if self.identity is not None:
            return self.identity
        return VerifiedExternalIdentity(
            issuer=ISSUER, subject=SUBJECT_B if kwargs["code"] == CODE_B else SUBJECT_A,
        )


class Sessions:
    def __init__(self):
        self.users = {}
        self.issued = []
        self.provisioned = []
        self.resolved = []
        self.revoked = []
        self.error = None
        self.failure_phase = None

    def _fail(self, phase):
        if self.failure_phase == phase and self.error is not None:
            raise self.error

    def provision_user(self, *, identity):
        self.provisioned.append(identity)
        self._fail("provision")
        return self.users.setdefault((identity.issuer, identity.subject), uuid4())

    def create(self, *, user_id):
        self._fail("create")
        ordinal = len(self.issued) + 1
        issued = IssuedAuthSession(
            principal=AuthenticatedPrincipal(
                user_id=user_id, auth_session_id=uuid4(),
                request_context=f"SYNTHETIC_REQUEST_CONTEXT_{ordinal}_" + "q" * 43,
            ),
            credential=f"SYNTHETIC_AUTH_CREDENTIAL_{ordinal}_" + "t" * 43,
        )
        self.issued.append(issued)
        return issued

    def resolve(self, *, credential):
        self.resolved.append(credential)
        self._fail("resolve")
        for issued in self.issued:
            if issued.credential == credential and issued.principal.auth_session_id not in self.revoked:
                return issued.principal
        raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)

    def revoke(self, *, auth_session_id):
        self._fail("revoke")
        self.revoked.append(auth_session_id)


@dataclass
class Harness:
    application: FastAPI
    client: TestClient
    settings: object
    transactions: object
    verifier: Verifier
    sessions: object
    factory: object = None
    clock: object = None

    def login(self):
        response = self.client.get("/api/auth/login")
        assert response.status_code == 302
        state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
        return response, state

    def callback(self, state, code=CODE_A, **extra):
        return self.client.get("/api/auth/callback", params={"state": state, "code": code, **extra})


def application_for(auth_settings, transactions, verifier, sessions):
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.include_router(auth_routes.router)

    @application.get("/protected")
    def protected(principal: AuthenticatedPrincipalDependency):
        return {"user_id": str(principal.user_id)}

    application.dependency_overrides.update({
        auth_routes.get_auth_settings: lambda: auth_settings,
        auth_routes.get_login_transaction_store: lambda: transactions,
        auth_routes.get_oidc_login_client: lambda: verifier,
        auth_routes.get_login_auth_session_store: lambda: sessions,
        get_auth_session_store: lambda: sessions,
    })
    return application


@pytest.fixture
def harness():
    auth_settings, transactions, verifier, sessions = settings(), Transactions(), Verifier(), Sessions()
    application = application_for(auth_settings, transactions, verifier, sessions)
    with TestClient(application, base_url=ORIGIN, follow_redirects=False) as client:
        yield Harness(application, client, auth_settings, transactions, verifier, sessions)


@pytest.fixture
def postgres_harness(postgres_session_factory):
    auth_settings = settings(ttl=1234)
    clock = [NOW]
    transactions = PostgreSQLOIDCLoginTransactionStore(postgres_session_factory, clock=lambda: clock[0])
    sessions = PostgreSQLAuthSessionStore(
        postgres_session_factory, session_lifetime=auth_settings.session_lifetime,
        clock=lambda: clock[0],
    )
    verifier = Verifier()
    application = application_for(auth_settings, transactions, verifier, sessions)
    with TestClient(application, base_url=ORIGIN, follow_redirects=False) as client:
        yield Harness(application, client, auth_settings, transactions, verifier, sessions, postgres_session_factory, clock)


def cookie_header(response, name):
    for raw in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(raw)
        if name in parsed:
            return parsed[name]
    return None


def assert_cookie(response, name, *, path, max_age, deleted=False, secure=True):
    cookie = cookie_header(response, name)
    assert cookie is not None
    assert cookie["httponly"] is True
    assert bool(cookie["secure"]) is secure
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == path
    assert cookie["domain"] == ""
    assert cookie["max-age"] == str(max_age)
    assert (cookie.value == "") is deleted
    return cookie


def assert_failure(response, status, body, *private):
    assert response.status_code == status
    assert response.json() == body
    assert response.headers["Cache-Control"] == "no-store"
    assert cookie_header(response, AUTH_SESSION_COOKIE_NAME) is None
    for value in (PRIVATE, CLIENT_SECRET, CODE_A, CODE_B, *private):
        if not value:
            continue
        assert value not in response.text
        assert value not in response.headers.get("Location", "")


def test_login_redirect_sets_bound_secure_state_cookie_and_exact_request_fields(harness):
    response, state = harness.login()
    assert response.headers["Cache-Control"] == "no-store"
    transaction = harness.transactions.created[0]
    assert harness.verifier.authorization_calls == [transaction]
    query = parse_qs(urlsplit(response.headers["Location"]).query)
    assert urlsplit(response.headers["Location"]).hostname == "identity.example.test"
    assert query == {
        "response_type": ["code"], "scope": ["openid"], "client_id": [CLIENT_ID],
        "redirect_uri": [REDIRECT_URI], "state": [state], "nonce": [transaction.nonce],
        "code_challenge": [transaction.code_challenge], "code_challenge_method": ["S256"],
    }
    binding = assert_cookie(
        response, auth_routes.OIDC_STATE_COOKIE_NAME,
        path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=600,
    )
    assert binding.value == state
    assert cookie_header(response, AUTH_SESSION_COOKIE_NAME) is None
    assert harness.sessions.issued == []
    assert harness.sessions.provisioned == []
    assert CLIENT_SECRET not in response.headers["Location"]


@pytest.mark.parametrize("query", [
    "return_to=https%3A%2F%2Fevil.example", "next=%2Fprivate", "redirect=%2Fprivate",
    "issuer=https%3A%2F%2Fevil.example", "client_id=evil", "redirect_uri=https%3A%2F%2Fevil.example",
    "unknown=", "unknown",
])
def test_login_rejects_all_caller_configuration_or_return_destinations_before_transaction(harness, query):
    response = harness.client.get("/api/auth/login?" + query)
    assert_failure(response, 400, LOGIN_FAILURE, "evil.example")
    assert harness.transactions.created == []
    assert harness.verifier.authorization_calls == []
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("phase", ["transaction", "authorization"])
def test_login_infrastructure_failures_are_fixed_no_store_without_auth_issuance(harness, phase, capsys):
    error = RuntimeError(PRIVATE + CLIENT_SECRET)
    if phase == "transaction":
        harness.transactions.error = error
    else:
        harness.verifier.authorization_error = error
    response = harness.client.get("/api/auth/login")
    assert_failure(response, 503, UNAVAILABLE)
    assert harness.sessions.issued == []
    assert capsys.readouterr().out == ""


def test_missing_runtime_settings_fail_closed_without_breaking_app_import(harness, monkeypatch):
    for name in (
        "AUTH_OIDC_ISSUER", "AUTH_OIDC_CLIENT_ID", "AUTH_OIDC_CLIENT_SECRET",
        "AUTH_OIDC_REDIRECT_URI", "AUTH_APP_ORIGIN", "AUTH_SESSION_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    harness.application.dependency_overrides.pop(auth_routes.get_auth_settings)
    response = harness.client.get("/api/auth/login")
    assert_failure(response, 503, UNAVAILABLE)
    assert harness.transactions.created == []
    assert harness.verifier.authorization_calls == []


def test_valid_callback_uses_consumed_proof_creates_local_session_and_fixed_redirect(harness):
    _, state = harness.login()
    expected = harness.transactions.pending[state]
    response = harness.callback(state)
    assert response.status_code == 302
    assert response.headers["Location"] == ORIGIN
    assert response.headers["Cache-Control"] == "no-store"
    assert harness.transactions.consumed == [state]
    assert state not in harness.transactions.pending
    assert harness.verifier.verification_calls == [{
        "code": CODE_A, "state": state, "expected_state": state,
        "expected_nonce": expected.nonce, "code_verifier": expected.code_verifier,
    }]
    assert harness.sessions.provisioned == [VerifiedExternalIdentity(ISSUER, SUBJECT_A)]
    issued = harness.sessions.issued[0]
    auth_cookie = assert_cookie(response, AUTH_SESSION_COOKIE_NAME, path="/", max_age=3600)
    assert auth_cookie.value == issued.credential
    assert_cookie(response, auth_routes.OIDC_STATE_COOKIE_NAME, path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=0, deleted=True)
    for value in (issued.credential, issued.principal.request_context, str(issued.principal.user_id), state, expected.nonce, expected.code_verifier):
        assert value not in response.text
        assert value not in response.headers["Location"]
    assert len({issued.credential, issued.principal.request_context, state, expected.nonce, expected.code_verifier, CODE_A}) == 6


@pytest.mark.parametrize("binding", ["missing", "wrong", "other_valid", "case_changed", "leading_space"])
def test_browser_binding_mismatch_never_consumes_or_issues(harness, binding):
    _, state = harness.login()
    if binding == "missing":
        harness.client.cookies.clear()
    elif binding == "other_valid":
        harness.login()
    else:
        altered = {"wrong": "different-state", "case_changed": state.lower(), "leading_space": " " + state}[binding]
        harness.client.cookies.clear()
        harness.client.cookies.set(auth_routes.OIDC_STATE_COOKIE_NAME, altered, domain="rehearse.example.test", path=auth_routes.OIDC_STATE_COOKIE_PATH)
    if binding == "leading_space":
        response = harness.client.get("/api/auth/callback", params={"state": state, "code": CODE_A}, headers={
            "Cookie": f'{auth_routes.OIDC_STATE_COOKIE_NAME}=" {state}"',
        })
    else:
        response = harness.callback(state)
    assert_failure(response, 400, LOGIN_FAILURE, state)
    assert state in harness.transactions.pending
    assert harness.transactions.consumed == []
    assert harness.verifier.verification_calls == []
    assert harness.sessions.provisioned == []
    assert harness.sessions.issued == []
    assert "set-cookie" not in response.headers


def test_attacker_callback_cannot_log_a_victim_into_the_attackers_identity(harness):
    _, attacker_state = harness.login()
    with TestClient(harness.application, base_url=ORIGIN, follow_redirects=False) as victim:
        response = victim.get("/api/auth/callback", params={"state": attacker_state, "code": CODE_B})
        assert_failure(response, 400, LOGIN_FAILURE, attacker_state)
        assert AUTH_SESSION_COOKIE_NAME not in victim.cookies
    assert attacker_state in harness.transactions.pending
    assert harness.transactions.consumed == []
    assert harness.verifier.verification_calls == []
    assert harness.sessions.issued == []


@pytest.mark.parametrize("query", [
    "code=secret", "state=", "state", "state=s&state=s&code=c",
    "state=s&code=c&code=c", "state=s&code=", "state=s&code",
])
def test_callback_malformed_required_query_is_fixed_failure_without_fastapi_value_echo(harness, query):
    response = harness.client.get("/api/auth/callback?" + query)
    assert_failure(response, 400, LOGIN_FAILURE, "secret")
    assert harness.transactions.consumed == []
    assert harness.verifier.verification_calls == []
    assert harness.sessions.issued == []


@pytest.mark.parametrize("query", ["", "code", "code=", "code=a&code=b", "code=%20%20"])
def test_matched_browser_state_with_missing_or_duplicate_code_is_consumed_without_exchange(harness, query):
    _, state = harness.login()
    response = harness.client.get("/api/auth/callback?" + urlencode({"state": state}) + ("&" + query if query else ""))
    assert_failure(response, 400, LOGIN_FAILURE, state)
    assert harness.transactions.consumed == [state]
    assert state not in harness.transactions.pending
    assert harness.verifier.verification_calls == []
    assert harness.sessions.provisioned == []
    assert harness.sessions.issued == []
    assert_cookie(response, auth_routes.OIDC_STATE_COOKIE_NAME, path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=0, deleted=True)


@pytest.mark.parametrize("url", [
    "/authorize", "http://identity.example.test/authorize", "javascript:alert(1)",
    "https://user:password@identity.example.test/authorize", "https://identity.example.test/authorize#fragment",
    "https://identity.example.test:invalid/authorize", "https://identity.example.test/a path",
])
def test_login_rejects_malformed_redirect_output_from_injected_oidc_boundary(harness, url):
    async def malformed(**kwargs):
        return url
    harness.verifier.authorization_url = malformed
    response = harness.client.get("/api/auth/login")
    assert_failure(response, 503, UNAVAILABLE)
    assert "Location" not in response.headers
    assert "set-cookie" not in response.headers
    assert harness.sessions.issued == []


def test_provider_error_without_browser_binding_does_not_consume_a_victims_transaction(harness):
    _, state = harness.login()
    harness.client.cookies.clear()
    response = harness.client.get("/api/auth/callback", params={"state": state, "error": "access_denied", "error_description": PRIVATE})
    assert_failure(response, 400, LOGIN_FAILURE, state)
    assert state in harness.transactions.pending
    assert harness.transactions.consumed == []
    assert harness.verifier.verification_calls == []
    assert "set-cookie" not in response.headers


def test_callback_success_cannot_replay_even_if_original_binding_cookie_is_restored(harness):
    _, state = harness.login()
    assert harness.callback(state).status_code == 302
    harness.client.cookies.set(auth_routes.OIDC_STATE_COOKIE_NAME, state, domain="rehearse.example.test", path=auth_routes.OIDC_STATE_COOKIE_PATH)
    response = harness.callback(state)
    assert_failure(response, 400, LOGIN_FAILURE, state)
    assert len(harness.verifier.verification_calls) == 1
    assert len(harness.sessions.issued) == 1


@pytest.mark.parametrize("error", ["access_denied", "server_error", PRIVATE, ""])
def test_provider_declared_error_consumes_bound_state_without_exchange_or_provider_echo(harness, error):
    _, state = harness.login()
    response = harness.client.get("/api/auth/callback", params={
        "state": state, "error": error, "error_description": PRIVATE + CLIENT_SECRET,
    })
    assert_failure(response, 400, LOGIN_FAILURE, state, error)
    assert harness.transactions.consumed == [state]
    assert state not in harness.transactions.pending
    assert harness.verifier.verification_calls == []
    assert harness.sessions.issued == []
    assert_cookie(response, auth_routes.OIDC_STATE_COOKIE_NAME, path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=0, deleted=True)


@pytest.mark.parametrize("kind,status", [
    (OIDCFailureKind.INVALID_STATE, 400), (OIDCFailureKind.EXCHANGE_FAILED, 400),
    (OIDCFailureKind.INVALID_TOKEN, 400), (OIDCFailureKind.UNAVAILABLE, 503),
])
def test_matched_provider_failure_keeps_consumption_and_clears_binding(harness, kind, status):
    _, state = harness.login()
    harness.verifier.error = OIDCFailure(kind)
    response = harness.callback(state)
    assert_failure(response, status, UNAVAILABLE if status == 503 else LOGIN_FAILURE, state)
    assert state not in harness.transactions.pending
    assert len(harness.verifier.verification_calls) == 1
    assert harness.sessions.provisioned == []
    assert harness.sessions.issued == []
    assert_cookie(response, auth_routes.OIDC_STATE_COOKIE_NAME, path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=0, deleted=True)


@pytest.mark.parametrize("phase", ["consume", "verify", "provision", "create"])
def test_callback_unexpected_errors_are_private_and_no_auth_cookie_can_precede_commit(harness, phase, capsys):
    _, state = harness.login()
    consumed = harness.transactions.pending[state]
    error = RuntimeError(PRIVATE + state + consumed.nonce + consumed.code_verifier + CLIENT_SECRET + CODE_A)
    if phase == "consume":
        harness.transactions.error = error
    elif phase == "verify":
        harness.verifier.error = error
    else:
        harness.sessions.failure_phase = phase
        harness.sessions.error = error
    response = harness.callback(state)
    assert_failure(response, 503, UNAVAILABLE, state, consumed.nonce, consumed.code_verifier)
    assert harness.sessions.issued == []
    if phase == "consume":
        assert state in harness.transactions.pending
        assert cookie_header(response, auth_routes.OIDC_STATE_COOKIE_NAME) is None
    else:
        assert state not in harness.transactions.pending
        assert_cookie(response, auth_routes.OIDC_STATE_COOKIE_NAME, path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=0, deleted=True)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("output", [None, {"issuer": ISSUER, "sub": SUBJECT_A}, SUBJECT_A, uuid4()])
def test_callback_never_provisions_unverified_or_malformed_identity_outputs(harness, output):
    _, state = harness.login()
    async def invalid(**kwargs):
        return output
    harness.verifier.verify_callback = invalid
    response = harness.callback(state)
    assert_failure(response, 400, LOGIN_FAILURE, state)
    assert harness.sessions.provisioned == []
    assert harness.sessions.issued == []


def test_callback_cannot_accept_identity_attested_for_a_different_trusted_issuer(harness):
    _, state = harness.login()
    harness.verifier.identity = VerifiedExternalIdentity("https://attacker-issuer.example.test", SUBJECT_A)
    response = harness.callback(state)
    assert_failure(response, 400, LOGIN_FAILURE, state)
    assert harness.sessions.provisioned == []
    assert harness.sessions.issued == []


@pytest.mark.parametrize("phase", ["proof", "user", "issued", "credential", "principal"])
def test_callback_malformed_internal_outputs_are_unavailable_without_cookie_or_metadata(harness, phase):
    _, state = harness.login()
    if phase == "proof":
        original = harness.transactions.consume_login_transaction
        def malformed_proof(value):
            original(value)
            return {"nonce": PRIVATE, "code_verifier": PRIVATE}
        harness.transactions.consume_login_transaction = malformed_proof
    elif phase == "user":
        harness.sessions.provision_user = lambda **kwargs: str(uuid4())
    else:
        original = harness.sessions.create
        def malformed_issue(**kwargs):
            issued = original(**kwargs)
            if phase == "issued":
                return {"credential": PRIVATE}
            if phase == "credential":
                return IssuedAuthSession(principal=issued.principal, credential="invalid credential")
            return IssuedAuthSession(
                principal=AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context="wrong-user-context"),
                credential=issued.credential,
            )
        harness.sessions.create = malformed_issue
    response = harness.callback(state)
    assert_failure(response, 503, UNAVAILABLE, state)
    assert_cookie(response, auth_routes.OIDC_STATE_COOKIE_NAME, path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=0, deleted=True)
    assert state not in harness.transactions.pending


@pytest.mark.parametrize("headers", [{}, {AUTH_REQUEST_CONTEXT_HEADER: "wrong"}, {"X-User-ID": "attacker", "Authorization": "Bearer attacker"}])
def test_me_is_minimal_cookie_bootstrap_without_request_context_requirement(harness, headers):
    _, state = harness.login()
    assert harness.callback(state).status_code == 302
    principal = harness.sessions.issued[0].principal
    response = harness.client.get("/api/auth/me", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"user_id": str(principal.user_id), "request_context": principal.request_context}
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers
    for value in (harness.sessions.issued[0].credential, str(principal.auth_session_id), ISSUER, SUBJECT_A, CLIENT_SECRET):
        assert value not in response.text


@pytest.mark.parametrize("credential", [None, "", "old-cookie", "revoked-cookie"])
def test_me_without_valid_cookie_is_401_even_with_context_or_user_or_bearer(harness, credential):
    headers = {AUTH_REQUEST_CONTEXT_HEADER: "attacker-context", "X-User-ID": str(uuid4()), "Authorization": "Bearer attacker-token"}
    if credential is not None:
        headers["Cookie"] = f"{AUTH_SESSION_COOKIE_NAME}={credential}"
    response = harness.client.get("/api/auth/me", headers=headers)
    assert_failure(response, 401, UNAUTHENTICATED)


@pytest.mark.parametrize("failure", [
    AuthenticationFailure(AuthenticationFailureKind.INVALID_REQUEST_CONTEXT),
    AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE), RuntimeError(PRIVATE),
])
def test_me_infrastructure_or_impossible_context_failure_never_reports_bootstrap_403(harness, failure):
    _, state = harness.login()
    harness.callback(state)
    harness.sessions.failure_phase, harness.sessions.error = "resolve", failure
    response = harness.client.get("/api/auth/me")
    if type(failure) is AuthenticationFailure and failure.kind is AuthenticationFailureKind.INVALID_REQUEST_CONTEXT:
        assert_failure(response, 401, UNAUTHENTICATED)
    else:
        assert_failure(response, 503, UNAVAILABLE)


@pytest.mark.parametrize("output", [None, {}, {"user_id": str(uuid4()), "request_context": PRIVATE}, PRIVATE])
def test_me_never_generically_serializes_malformed_principals(harness, output):
    _, state = harness.login()
    harness.callback(state)
    harness.sessions.resolve = lambda **kwargs: output
    response = harness.client.get("/api/auth/me")
    assert_failure(response, 503, UNAVAILABLE)


def test_logout_requires_live_cookie_and_context_revokes_only_current_generation(harness):
    _, first = harness.login()
    harness.callback(first)
    original = harness.sessions.issued[0]
    _, second = harness.login()
    harness.callback(second)
    current = harness.sessions.issued[1]
    assert original.principal.user_id == current.principal.user_id
    response = harness.client.post("/api/auth/logout", headers={AUTH_REQUEST_CONTEXT_HEADER: current.principal.request_context})
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["Cache-Control"] == "no-store"
    assert_cookie(response, AUTH_SESSION_COOKIE_NAME, path="/", max_age=0, deleted=True)
    assert harness.sessions.revoked == [current.principal.auth_session_id]
    assert harness.sessions.resolve(credential=original.credential) == original.principal
    assert harness.verifier.verification_calls and len(harness.verifier.verification_calls) == 2
    assert len(harness.verifier.authorization_calls) == 2
    assert_failure(harness.client.get("/api/auth/me"), 401, UNAUTHENTICATED)


@pytest.mark.parametrize("context", [None, "wrong-context", "stale-generation"])
def test_logout_wrong_context_is_fixed_403_and_cannot_revoke(harness, context):
    _, state = harness.login()
    harness.callback(state)
    headers = {} if context is None else {AUTH_REQUEST_CONTEXT_HEADER: context}
    response = harness.client.post("/api/auth/logout", headers=headers)
    assert_failure(response, 403, INVALID_CONTEXT)
    assert harness.sessions.revoked == []
    assert AUTH_SESSION_COOKIE_NAME in harness.client.cookies


@pytest.mark.parametrize("failure", [AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE), RuntimeError(PRIVATE)])
def test_logout_failed_revocation_is_503_keeps_cookie_and_leaves_other_sessions(harness, failure):
    _, state = harness.login()
    harness.callback(state)
    issued = harness.sessions.issued[0]
    harness.sessions.failure_phase, harness.sessions.error = "revoke", failure
    response = harness.client.post("/api/auth/logout", headers={AUTH_REQUEST_CONTEXT_HEADER: issued.principal.request_context})
    assert_failure(response, 503, UNAVAILABLE)
    assert "set-cookie" not in response.headers
    assert harness.client.cookies.get(AUTH_SESSION_COOKIE_NAME) == issued.credential
    assert harness.sessions.revoked == []
    assert harness.sessions.resolve(credential=issued.credential) == issued.principal


def test_logout_rejects_missing_or_already_revoked_session(harness):
    assert_failure(harness.client.post("/api/auth/logout"), 401, UNAUTHENTICATED)
    _, state = harness.login()
    harness.callback(state)
    issued = harness.sessions.issued[0]
    harness.sessions.revoke(auth_session_id=issued.principal.auth_session_id)
    response = harness.client.post("/api/auth/logout", headers={AUTH_REQUEST_CONTEXT_HEADER: issued.principal.request_context})
    assert_failure(response, 401, UNAUTHENTICATED)


def test_account_switch_replaces_cookie_and_bootstrap_old_tab_context_cannot_follow_new_user(harness):
    _, first = harness.login()
    harness.callback(first, CODE_A)
    original = harness.sessions.issued[0]
    assert harness.client.get("/api/auth/me").json() == {"user_id": str(original.principal.user_id), "request_context": original.principal.request_context}
    _, second = harness.login()
    harness.callback(second, CODE_B)
    current = harness.sessions.issued[1]
    assert current.principal.user_id != original.principal.user_id
    assert current.credential != original.credential
    assert current.principal.request_context != original.principal.request_context
    assert harness.client.get("/api/auth/me", headers={AUTH_REQUEST_CONTEXT_HEADER: original.principal.request_context}).json() == {
        "user_id": str(current.principal.user_id), "request_context": current.principal.request_context,
    }
    stale = harness.client.get("/protected", headers={AUTH_REQUEST_CONTEXT_HEADER: original.principal.request_context})
    assert_failure(stale, 403, INVALID_CONTEXT)
    fresh = harness.client.get("/protected", headers={AUTH_REQUEST_CONTEXT_HEADER: current.principal.request_context})
    assert fresh.status_code == 200
    assert fresh.json() == {"user_id": str(current.principal.user_id)}
    assert harness.sessions.resolve(credential=original.credential) == original.principal


def test_new_login_does_not_adopt_preexisting_cookie_or_query_identity(harness):
    harness.client.cookies.set(AUTH_SESSION_COOKIE_NAME, "attacker-supplied-credential", domain="rehearse.example.test", path="/")
    _, state = harness.login()
    response = harness.callback(state, user_id=str(uuid4()), request_context="attacker-context", return_to="https://evil.example")
    assert response.status_code == 302
    assert response.headers["Location"] == ORIGIN
    issued = harness.sessions.issued[0]
    assert cookie_header(response, AUTH_SESSION_COOKIE_NAME).value == issued.credential
    assert issued.credential != "attacker-supplied-credential"
    assert issued.principal.request_context != "attacker-context"
    assert harness.sessions.provisioned == [VerifiedExternalIdentity(ISSUER, SUBJECT_A)]


@pytest.mark.parametrize("phase", ["verify", "consume", "provision", "create"])
def test_caller_cancellation_is_not_normalized_or_replayed(harness, phase):
    transaction = harness.transactions.create_login_transaction()
    if phase == "verify":
        harness.verifier.error = asyncio.CancelledError()
    elif phase == "consume":
        harness.transactions.error = asyncio.CancelledError()
    else:
        harness.sessions.failure_phase, harness.sessions.error = phase, asyncio.CancelledError()

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.application), base_url=ORIGIN) as client:
            return await client.get("/api/auth/callback", params={"state": transaction.state, "code": CODE_A}, headers={"Cookie": f"{auth_routes.OIDC_STATE_COOKIE_NAME}={transaction.state}"})

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(request())
    assert harness.transactions.consumed == [transaction.state]
    assert len(harness.verifier.verification_calls) <= 1
    assert harness.sessions.issued == []


@pytest.mark.parametrize("phase", ["verify", "consume", "provision", "create"])
def test_system_exit_is_not_swallowed_by_login_composition(harness, phase):
    transaction = harness.transactions.create_login_transaction()
    if phase == "verify":
        harness.verifier.error = SystemExit(0)
    elif phase == "consume":
        harness.transactions.error = SystemExit(0)
    else:
        harness.sessions.failure_phase, harness.sessions.error = phase, SystemExit(0)
    query = urlencode({"state": transaction.state, "code": CODE_A}).encode("ascii")
    request = Request({
        "type": "http", "method": "GET", "path": "/api/auth/callback", "query_string": query,
        "headers": [(b"cookie", f"{auth_routes.OIDC_STATE_COOKIE_NAME}={transaction.state}".encode("ascii"))],
    })
    with pytest.raises(SystemExit) as error:
        asyncio.run(auth_routes.callback(
            request=request, settings=harness.settings, transactions=harness.transactions,
            oidc=harness.verifier, store=harness.sessions,
        ))
    assert error.value.code == 0
    assert len(harness.verifier.verification_calls) <= 1
    assert harness.sessions.issued == []


def test_provider_await_is_between_committed_worker_phases_not_on_the_database_thread(harness):
    _, state = harness.login()
    phases = []
    original_consume = harness.transactions.consume_login_transaction
    def consume(value):
        phases.append(("consume", get_ident()))
        return original_consume(value)
    original_provision = harness.sessions.provision_user
    def provision(**kwargs):
        phases.append(("provision", get_ident()))
        return original_provision(**kwargs)
    original_create = harness.sessions.create
    def create(**kwargs):
        phases.append(("create", get_ident()))
        return original_create(**kwargs)
    harness.transactions.consume_login_transaction = consume
    harness.sessions.provision_user = provision
    harness.sessions.create = create
    harness.verifier.on_verify = lambda kwargs: phases.append(("verify", get_ident()))
    assert harness.callback(state).status_code == 302
    assert [phase for phase, _ in phases] == ["consume", "verify", "provision", "create"]
    provider_thread = phases[1][1]
    assert all(thread != provider_thread for phase, thread in phases if phase != "verify")


def test_real_login_persists_digest_and_callback_creates_only_verified_user_and_digest_session(postgres_harness):
    harness = postgres_harness
    _, state = harness.login()
    with harness.factory() as database:
        transaction = database.scalar(select(OIDCLoginTransaction))
        assert transaction.state_hash == sha256(state.encode("utf-8")).digest()
        assert transaction.expires_at - transaction.created_at == timedelta(minutes=10)
        nonce, verifier = transaction.nonce, transaction.code_verifier
        assert database.scalar(select(func.count()).select_from(User)) == 0
        assert database.scalar(select(func.count()).select_from(AuthSession)) == 0
    response = harness.callback(state)
    assert response.status_code == 302
    credential = cookie_header(response, AUTH_SESSION_COOKIE_NAME).value
    principal = harness.sessions.resolve(credential=credential)
    with harness.factory() as database:
        assert database.scalar(select(func.count()).select_from(OIDCLoginTransaction)) == 0
        assert database.scalar(select(func.count()).select_from(User)) == 1
        assert database.scalar(select(func.count()).select_from(AuthSession)) == 1
        user = database.get(User, principal.user_id)
        assert (user.auth_provider, user.provider_subject) == (ISSUER, SUBJECT_A)
        row = database.get(AuthSession, principal.auth_session_id)
        assert row.token_hash == sha256(credential.encode("utf-8")).digest()
        assert row.request_context == principal.request_context
        assert row.created_at == NOW
        assert row.expires_at == NOW + timedelta(seconds=1234)
        assert set(row.__table__.columns.keys()) == {"id", "user_id", "token_hash", "request_context", "created_at", "expires_at"}
    assert len({state, nonce, verifier, credential, principal.request_context, CODE_A}) == 6
    assert_cookie(response, AUTH_SESSION_COOKIE_NAME, path="/", max_age=1234)


def test_real_callback_verifies_after_consumption_commit_and_outside_any_database_scope(postgres_harness, postgres_engine):
    harness = postgres_harness
    transactions = []
    def on_begin(connection):
        transactions.append(connection)
    def on_end(connection):
        transactions.remove(connection)
    event.listen(postgres_engine, "begin", on_begin)
    event.listen(postgres_engine, "commit", on_end)
    event.listen(postgres_engine, "rollback", on_end)
    try:
        _, state = harness.login()
        def inspect_consumed(kwargs):
            assert transactions == []
            with harness.factory() as database:
                assert database.scalar(select(func.count()).select_from(OIDCLoginTransaction)) == 0
                assert database.scalar(select(func.count()).select_from(AuthSession)) == 0
        harness.verifier.on_verify = inspect_consumed
        assert harness.callback(state).status_code == 302
        assert transactions == []
    finally:
        event.remove(postgres_engine, "begin", on_begin)
        event.remove(postgres_engine, "commit", on_end)
        event.remove(postgres_engine, "rollback", on_end)


@pytest.mark.parametrize("expiry", ["equal", "past"])
def test_real_expired_state_fails_with_no_token_exchange_and_expired_delete_commits(postgres_harness, expiry):
    harness = postgres_harness
    _, state = harness.login()
    harness.clock[0] = NOW + timedelta(minutes=10, seconds=1 if expiry == "past" else 0)
    response = harness.callback(state)
    assert_failure(response, 400, LOGIN_FAILURE, state)
    assert harness.verifier.verification_calls == []
    with harness.factory() as database:
        assert database.scalar(select(func.count()).select_from(OIDCLoginTransaction)) == 0
        assert database.scalar(select(func.count()).select_from(User)) == 0
        assert database.scalar(select(func.count()).select_from(AuthSession)) == 0


def test_real_provider_failure_never_creates_user_or_auth_session_and_cannot_replay(postgres_harness):
    harness = postgres_harness
    _, state = harness.login()
    harness.verifier.error = OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
    response = harness.callback(state)
    assert_failure(response, 400, LOGIN_FAILURE, state)
    harness.client.cookies.set(auth_routes.OIDC_STATE_COOKIE_NAME, state, domain="rehearse.example.test", path=auth_routes.OIDC_STATE_COOKIE_PATH)
    assert_failure(harness.callback(state), 400, LOGIN_FAILURE, state)
    assert len(harness.verifier.verification_calls) == 1
    with harness.factory() as database:
        assert database.scalar(select(func.count()).select_from(OIDCLoginTransaction)) == 0
        assert database.scalar(select(func.count()).select_from(User)) == 0
        assert database.scalar(select(func.count()).select_from(AuthSession)) == 0


def test_real_concurrent_callbacks_atomically_consume_exactly_once(postgres_harness):
    harness = postgres_harness
    _, state = harness.login()
    barrier = Barrier(4)
    def callback():
        with TestClient(harness.application, base_url=ORIGIN, follow_redirects=False) as client:
            barrier.wait(timeout=10)
            return client.get("/api/auth/callback", params={"state": state, "code": CODE_A}, headers={"Cookie": f"{auth_routes.OIDC_STATE_COOKIE_NAME}={state}"})
    with ThreadPoolExecutor(max_workers=4) as workers:
        responses = list(workers.map(lambda _: callback(), range(4)))
    assert sorted(response.status_code for response in responses) == [302, 400, 400, 400]
    assert len(harness.verifier.verification_calls) == 1
    assert sum(cookie_header(response, AUTH_SESSION_COOKIE_NAME) is not None for response in responses) == 1
    with harness.factory() as database:
        assert database.scalar(select(func.count()).select_from(OIDCLoginTransaction)) == 0
        assert database.scalar(select(func.count()).select_from(User)) == 1
        assert database.scalar(select(func.count()).select_from(AuthSession)) == 1


def test_real_identity_resolution_is_issuer_subject_only_and_each_login_issues_fresh_generation(postgres_harness):
    harness = postgres_harness
    logins = []
    for identity in (
        VerifiedExternalIdentity(ISSUER, SUBJECT_A), VerifiedExternalIdentity(ISSUER, SUBJECT_A),
        VerifiedExternalIdentity("https://other-issuer.example.test", SUBJECT_A),
    ):
        harness.verifier.identity = identity
        harness.application.dependency_overrides[auth_routes.get_auth_settings] = lambda identity=identity: settings(issuer=identity.issuer)
        _, state = harness.login()
        response = harness.callback(state)
        credential = cookie_header(response, AUTH_SESSION_COOKIE_NAME).value
        logins.append((credential, harness.sessions.resolve(credential=credential)))
    assert logins[0][1].user_id == logins[1][1].user_id != logins[2][1].user_id
    assert len({credential for credential, _ in logins}) == 3
    assert len({principal.auth_session_id for _, principal in logins}) == 3
    assert len({principal.request_context for _, principal in logins}) == 3
    with harness.factory() as database:
        assert database.scalar(select(func.count()).select_from(User)) == 2
        assert database.scalar(select(func.count()).select_from(AuthSession)) == 3
        assert set(User.__table__.columns.keys()) == {"id", "auth_provider", "provider_subject", "created_at"}


@pytest.mark.parametrize("later_ttl", [60, 86400])
def test_persisted_session_expiry_remains_authoritative_under_changed_issuance_configuration(postgres_harness, later_ttl):
    harness = postgres_harness
    _, state = harness.login()
    response = harness.callback(state)
    credential = cookie_header(response, AUTH_SESSION_COOKIE_NAME).value
    principal = harness.sessions.resolve(credential=credential)
    later = PostgreSQLAuthSessionStore(harness.factory, session_lifetime=timedelta(seconds=later_ttl), clock=lambda: harness.clock[0])
    harness.clock[0] = NOW + timedelta(seconds=1233)
    assert later.resolve(credential=credential) == principal
    harness.clock[0] = NOW + timedelta(seconds=1234)
    with pytest.raises(AuthenticationFailure) as failure:
        later.resolve(credential=credential)
    assert failure.value.kind is AuthenticationFailureKind.UNAUTHENTICATED
    with harness.factory() as database:
        row = database.get(AuthSession, principal.auth_session_id)
        assert row.expires_at == NOW + timedelta(seconds=1234)


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_real_me_rejects_expired_or_revoked_stored_session(postgres_harness, state):
    harness = postgres_harness
    _, login_state = harness.login()
    response = harness.callback(login_state)
    credential = cookie_header(response, AUTH_SESSION_COOKIE_NAME).value
    principal = harness.sessions.resolve(credential=credential)
    if state == "expired":
        harness.clock[0] = NOW + timedelta(seconds=1234)
    else:
        harness.sessions.revoke(auth_session_id=principal.auth_session_id)
    assert_failure(harness.client.get("/api/auth/me"), 401, UNAUTHENTICATED)


def test_real_logout_revokes_exact_generation_leaving_same_user_other_session_valid(postgres_harness):
    harness = postgres_harness
    credentials = []
    for _ in range(2):
        _, state = harness.login()
        response = harness.callback(state)
        credential = cookie_header(response, AUTH_SESSION_COOKIE_NAME).value
        credentials.append((credential, harness.sessions.resolve(credential=credential)))
    current = credentials[1][1]
    response = harness.client.post("/api/auth/logout", headers={AUTH_REQUEST_CONTEXT_HEADER: current.request_context})
    assert response.status_code == 204
    assert_cookie(response, AUTH_SESSION_COOKIE_NAME, path="/", max_age=0, deleted=True)
    assert harness.sessions.resolve(credential=credentials[0][0]) == credentials[0][1]
    with harness.factory() as database:
        assert database.get(AuthSession, current.auth_session_id) is None
        assert database.scalar(select(func.count()).select_from(AuthSession)) == 1
    assert len(harness.verifier.verification_calls) == 2


def test_committed_authentication_rows_are_unchanged_by_read_only_me(postgres_harness):
    harness = postgres_harness
    _, state = harness.login()
    harness.callback(state)
    def rows():
        with harness.factory() as database:
            return (
                tuple(database.execute(select(User.__table__)).all()),
                tuple(database.execute(select(AuthSession.__table__)).all()),
                tuple(database.execute(select(OIDCLoginTransaction.__table__)).all()),
            )
    before = rows()
    for header in ({}, {AUTH_REQUEST_CONTEXT_HEADER: "old-context"}):
        response = harness.client.get("/api/auth/me", headers=header)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
    assert rows() == before


@pytest.mark.parametrize("origin,callback,secure", [
    (ORIGIN, REDIRECT_URI, True),
    ("http://localhost:5173", "http://localhost:8000/api/auth/callback", False),
    ("http://127.0.0.1:5173", "http://127.0.0.1:8000/api/auth/callback", False),
    ("http://[::1]:5173", "http://[::1]:8000/api/auth/callback", False),
])
def test_trusted_cookie_policy_applies_to_login_callback_and_logout_without_other_attribute_changes(origin, callback, secure):
    config = auth_routes.AuthSettings(
        oidc=OIDCConfiguration(issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=callback),
        app_origin=origin, session_ttl_seconds=3600,
    )
    transactions, verifier, sessions = Transactions(), Verifier(), Sessions()

    async def authorization_url(*, transaction):
        return AUTHORIZATION_URI + "?" + urlencode({
            "response_type": "code", "scope": "openid", "client_id": CLIENT_ID,
            "redirect_uri": callback, "state": transaction.state, "nonce": transaction.nonce,
            "code_challenge": transaction.code_challenge, "code_challenge_method": "S256",
        })

    verifier.authorization_url = authorization_url
    application = application_for(config, transactions, verifier, sessions)
    with TestClient(application, base_url=origin, follow_redirects=False) as client:
        started = client.get("/api/auth/login", headers={"X-Forwarded-Proto": "http" if secure else "https"})
        assert started.status_code == 302
        state = parse_qs(urlsplit(started.headers["Location"]).query)["state"][0]
        assert_cookie(
            started, auth_routes.OIDC_STATE_COOKIE_NAME,
            path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=600, secure=secure,
        )
        diagnosed = client.get("/api/auth/callback", params={
            "state": state, "code": CODE_A, "secure": "false" if secure else "true",
        })
        assert diagnosed.status_code == 302
        assert diagnosed.headers["Location"] == origin
        cookie = assert_cookie(diagnosed, AUTH_SESSION_COOKIE_NAME, path="/", max_age=3600, secure=secure)
        assert_cookie(
            diagnosed, auth_routes.OIDC_STATE_COOKIE_NAME,
            path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=0, deleted=True, secure=secure,
        )
        assert cookie.value == sessions.issued[0].credential
        assert cookie.value not in diagnosed.text + diagnosed.headers["Location"]
        bootstrap = client.get("/api/auth/me")
        assert bootstrap.status_code == 200
        assert bootstrap.json() == {
            "user_id": str(sessions.issued[0].principal.user_id),
            "request_context": sessions.issued[0].principal.request_context,
        }
        ended = client.post("/api/auth/logout", headers={
            AUTH_REQUEST_CONTEXT_HEADER: sessions.issued[0].principal.request_context,
            "X-Forwarded-Proto": "http" if secure else "https",
        })
        assert ended.status_code == 204
        assert_cookie(ended, AUTH_SESSION_COOKIE_NAME, path="/", max_age=0, deleted=True, secure=secure)
        assert len(sessions.issued) == 1
        assert sessions.revoked == [sessions.issued[0].principal.auth_session_id]
        assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("phase", ["provider_error", "verification_failure"])
def test_loopback_state_cookie_failure_cleanup_matches_issuance_attributes(phase):
    config = auth_routes.AuthSettings(
        oidc=OIDCConfiguration(
            issuer=ISSUER, client_id=CLIENT_ID, redirect_uri="http://localhost:8000/api/auth/callback",
        ), app_origin="http://localhost:5173", session_ttl_seconds=3600,
    )
    transactions, verifier, sessions = Transactions(), Verifier(), Sessions()
    application = application_for(config, transactions, verifier, sessions)
    with TestClient(application, base_url=config.app_origin, follow_redirects=False) as client:
        started = client.get("/api/auth/login")
        state = parse_qs(urlsplit(started.headers["Location"]).query)["state"][0]
        if phase == "provider_error":
            params = {"state": state, "error": "private-provider-error", "error_description": PRIVATE}
        else:
            verifier.error = OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
            params = {"state": state, "code": CODE_A}
        failed = client.get("/api/auth/callback", params=params)
        assert_failure(failed, 400, LOGIN_FAILURE, state)
        assert_cookie(
            failed, auth_routes.OIDC_STATE_COOKIE_NAME,
            path=auth_routes.OIDC_STATE_COOKIE_PATH, max_age=0, deleted=True, secure=False,
        )
        assert sessions.issued == []


def test_browser_insecure_mode_query_is_rejected_before_login_cookie_policy(harness):
    response = harness.client.get("/api/auth/login?secure=false")
    assert_failure(response, 400, LOGIN_FAILURE)
    assert harness.transactions.created == []
    assert cookie_header(response, auth_routes.OIDC_STATE_COOKIE_NAME) is None
