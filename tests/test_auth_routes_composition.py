"""Real browser-route composition with local JOSE/HTTP fixtures and PostgreSQL."""

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import httpx2
from joserfc import jwt
from joserfc.jwk import RSAKey
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import auth_http, auth_routes
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.auth_settings import AuthSettings
from app.database_models import AuthSession, OIDCLoginTransaction, User
from app.main import app
from app.oidc_verifier import AuthlibOIDCVerifier, OIDCConfiguration

ORIGIN = "https://rehearse.example.test"
ISSUER = "https://identity.example.test/tenant"
AUTHORIZATION = "https://identity.example.test/authorize"
TOKEN = "https://identity.example.test/token"
JWKS = "https://identity.example.test/jwks"
CODE = "SYNTHETIC_COMPOSITION_AUTHORIZATION_CODE"
SECRET = "SYNTHETIC_COMPOSITION_CLIENT_SECRET"
ACCESS = "SYNTHETIC_COMPOSITION_ACCESS_TOKEN"
REFRESH = "SYNTHETIC_COMPOSITION_REFRESH_TOKEN"
PRIVATE = "PRIVATE_COMPOSITION_FAILURE_SENTINEL"
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def settings(ttl=4567):
    return AuthSettings(
        oidc=OIDCConfiguration(
            issuer=ISSUER, client_id="synthetic-client", client_secret=SECRET,
            redirect_uri=ORIGIN + "/api/auth/callback",
        ),
        app_origin=ORIGIN, session_ttl_seconds=ttl,
    )


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Composition tests require in-process HTTP transports.")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.HTTPTransport, "handle_request", forbidden)
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", forbidden_async)


def assert_unavailable(operation):
    with pytest.raises(HTTPException) as caught:
        operation()
    error = caught.value
    assert error.status_code == 503
    assert error.detail == "Authentication is temporarily unavailable."
    assert error.headers == {"Cache-Control": "no-store"}
    assert error.__cause__ is error.__context__ is None
    assert PRIVATE not in repr(error)


def test_issuance_dependency_uses_shared_factory_with_exact_configured_ttl(monkeypatch):
    factory = sessionmaker()
    observed = []

    def shared_factory():
        observed.append(factory)
        return factory

    monkeypatch.setattr(auth_routes, "get_database_session_factory", shared_factory)
    actual_store = PostgreSQLAuthSessionStore

    def construct(operation_factory, *, session_lifetime):
        assert operation_factory is factory
        assert not isinstance(operation_factory, Session)
        assert session_lifetime == timedelta(seconds=4567)
        return actual_store(operation_factory, session_lifetime=session_lifetime)

    monkeypatch.setattr(auth_routes, "PostgreSQLAuthSessionStore", construct)
    assert type(auth_routes.get_login_auth_session_store(settings())) is actual_store
    assert observed == [factory]


@pytest.mark.parametrize("dependency", ["get_login_transaction_store", "get_login_auth_session_store"])
@pytest.mark.parametrize("source", ["factory", "constructor"])
def test_lifecycle_store_construction_discards_sensitive_failure_chains(monkeypatch, dependency, source):
    def failed(*args, **kwargs):
        raise RuntimeError(PRIVATE + SECRET)

    if source == "factory":
        monkeypatch.setattr(auth_routes, "get_database_session_factory", failed)
    else:
        monkeypatch.setattr(auth_routes, "get_database_session_factory", lambda: sessionmaker())
        constructor = "PostgreSQLOIDCLoginTransactionStore" if dependency == "get_login_transaction_store" else "PostgreSQLAuthSessionStore"
        monkeypatch.setattr(auth_routes, constructor, failed)
    operation = getattr(auth_routes, dependency)
    assert_unavailable(operation if dependency == "get_login_transaction_store" else lambda: operation(settings()))


@pytest.mark.parametrize("source", ["settings", "client", "transactions", "sessions"])
@pytest.mark.parametrize("error", [
    pytest.param(asyncio.CancelledError(), id="cancelled"),
    pytest.param(SystemExit(), id="exit"),
])
def test_composition_construction_propagates_shutdown_and_cancellation(monkeypatch, source, error):
    def failed(*args, **kwargs):
        raise error

    if source == "settings":
        monkeypatch.setattr(auth_routes, "load_auth_settings", failed)
        operation = auth_routes.get_auth_settings
    elif source == "client":
        monkeypatch.setattr(auth_routes, "AuthlibOIDCVerifier", failed)
        operation = lambda: auth_routes.get_oidc_login_client(settings())
    else:
        monkeypatch.setattr(auth_routes, "get_database_session_factory", failed)
        operation = (auth_routes.get_login_transaction_store if source == "transactions"
                     else lambda: auth_routes.get_login_auth_session_store(settings()))
    with pytest.raises(type(error)) as caught:
        operation()
    assert caught.value is error


def test_oidc_dependency_uses_trusted_config_without_constructor_network(monkeypatch):
    seen = []
    sentinel = object()
    trusted = settings()

    def construct(configuration):
        seen.append(configuration)
        return sentinel

    monkeypatch.setattr(auth_routes, "AuthlibOIDCVerifier", construct)
    assert auth_routes.get_oidc_login_client(trusted) is sentinel
    assert seen == [trusted.oidc]


def test_real_routes_stores_and_oidc_library_compose_without_external_http(
    postgres_engine, postgres_session_factory, monkeypatch,
):
    trusted = settings()
    key = RSAKey.generate_key(2048, parameters={"kid": "local-key", "use": "sig"})
    requests = []
    proof = {}

    def handle(request):
        # Login consumption and issuance never hold connections during HTTP.
        assert postgres_engine.pool.checkedout() == 0
        requests.append(request)
        if str(request.url) == ISSUER + "/.well-known/openid-configuration":
            return httpx2.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": AUTHORIZATION,
                "token_endpoint": TOKEN, "jwks_uri": JWKS,
            })
        if str(request.url) == TOKEN:
            body = parse_qs(request.content.decode("ascii"))
            assert body == {
                "grant_type": ["authorization_code"], "code": [CODE],
                "redirect_uri": [trusted.oidc.redirect_uri],
                "code_verifier": [proof["verifier"]],
            }
            signed = jwt.encode({"alg": "RS256", "kid": "local-key"}, {
                "iss": ISSUER, "sub": "opaque-subject", "aud": trusted.oidc.client_id,
                "nonce": proof["nonce"], "iat": int(NOW.timestamp()) - 10,
                "exp": int(NOW.timestamp()) + 600, "email": "ignored@example.test",
            }, key)
            return httpx2.Response(200, json={
                "id_token": signed, "access_token": ACCESS,
                "refresh_token": REFRESH, "token_type": "Bearer",
            })
        assert str(request.url) == JWKS
        assert "Authorization" not in request.headers
        return httpx2.Response(200, json={"keys": [key.as_dict(private=False)]})

    def oidc_client(configuration):
        return AuthlibOIDCVerifier(configuration, transport=httpx2.MockTransport(handle), clock=lambda: NOW)

    monkeypatch.setitem(app.dependency_overrides, auth_routes.get_auth_settings, lambda: trusted)
    monkeypatch.setattr(auth_routes, "AuthlibOIDCVerifier", oidc_client)
    monkeypatch.setattr(auth_routes, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.setattr(auth_http, "get_database_session_factory", lambda: postgres_session_factory)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        initiation = client.get("/api/auth/login")
        assert initiation.status_code == 302
        query = parse_qs(urlsplit(initiation.headers["Location"]).query)
        state = query["state"][0]
        with postgres_session_factory() as database:
            transaction = database.scalars(select(OIDCLoginTransaction)).one()
            assert transaction.state_hash == sha256(state.encode()).digest()
            proof.update(nonce=transaction.nonce, verifier=transaction.code_verifier)
        result = client.get("/api/auth/callback", params={"state": state, "code": CODE})
        assert result.status_code == 302 and result.headers["Location"] == ORIGIN
        credential = client.cookies.get(auth_http.AUTH_SESSION_COOKIE_NAME)
        assert credential not in result.text + result.headers["Location"]
        bootstrap = client.get("/api/auth/me")
        assert bootstrap.status_code == 200
        assert set(bootstrap.json()) == {"user_id", "request_context"}
        with postgres_session_factory() as database:
            user = database.scalars(select(User)).one()
            session = database.scalars(select(AuthSession)).one()
            assert (user.auth_provider, user.provider_subject) == (ISSUER, "opaque-subject")
            assert session.user_id == user.id
            assert session.token_hash == sha256(credential.encode()).digest()
            assert session.expires_at - session.created_at == trusted.session_lifetime
            assert database.scalars(select(OIDCLoginTransaction)).all() == []
        logged_out = client.post("/api/auth/logout", headers={
            auth_http.AUTH_REQUEST_CONTEXT_HEADER: bootstrap.json()["request_context"],
        })
        assert logged_out.status_code == 204
        assert client.get("/api/auth/me").status_code == 401
    assert [str(request.url) for request in requests] == [
        ISSUER + "/.well-known/openid-configuration",
        ISSUER + "/.well-known/openid-configuration", TOKEN, JWKS,
    ]
    with postgres_session_factory() as database:
        assert database.scalars(select(AuthSession)).all() == []
