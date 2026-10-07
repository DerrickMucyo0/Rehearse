"""Trusted OIDC authorization construction with local discovery only."""

import asyncio
import base64
from dataclasses import fields
from hashlib import sha256
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest

from app.oidc_failure import OIDCFailure, OIDCFailureKind
from app.oidc_login import IssuedLoginTransaction
from app.oidc_verifier import AuthlibOIDCVerifier, OIDCConfiguration, OIDC_HTTP_TIMEOUT_SECONDS

ISSUER = "https://identity.example.test/tenant"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
AUTHORIZATION = "https://identity.example.test/authorize"
REDIRECT = "https://rehearse.example.test/api/auth/callback"
CLIENT = "rehearse-client"
STATE = "PRIVATE_SYNTHETIC_STATE_" + "s" * 43
NONCE = "PRIVATE_SYNTHETIC_NONCE_" + "n" * 43
VERIFIER = "PRIVATE_SYNTHETIC_VERIFIER_" + "v" * 43
SECRET = "PRIVATE_SYNTHETIC_CLIENT_SECRET"
PRIVATE = "PRIVATE_DISCOVERY_ERROR_SENTINEL"
CHALLENGE = base64.urlsafe_b64encode(sha256(VERIFIER.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def blocked(*args, **kwargs):
        raise AssertionError("Only in-process OIDC discovery is permitted.")

    async def blocked_async(*args, **kwargs):
        blocked()

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.HTTPTransport, "handle_request", blocked)
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", blocked_async)
    yield
    for value in (STATE, NONCE, VERIFIER, SECRET, PRIVATE):
        assert value not in caplog.text


def transaction():
    return IssuedLoginTransaction(state=STATE, nonce=NONCE, code_challenge=CHALLENGE)


def configuration(*, secret=None):
    return OIDCConfiguration(issuer=ISSUER, client_id=CLIENT,
                             redirect_uri=REDIRECT, client_secret=secret)


def metadata(**changes):
    result = {"issuer": ISSUER, "authorization_endpoint": AUTHORIZATION,
              "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks"}
    result.update(changes)
    return result


class Discovery:
    def __init__(self, *, payload=None, status=200, raw=None, error=None):
        self.payload = metadata() if payload is None else payload
        self.status = status
        self.raw = raw
        self.error = error
        self.requests = []
        self.transport = httpx2.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        assert str(request.url) == DISCOVERY
        assert request.method == "GET"
        assert request.content == b""
        assert "authorization" not in request.headers
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return httpx2.Response(self.status, content=self.raw)
        return httpx2.Response(self.status, json=self.payload,
                               headers={"Location": "https://unexpected.example.test/"})

    def client(self, *, secret=None):
        return AuthlibOIDCVerifier(configuration(secret=secret), transport=self.transport)

    def authorize(self, *, secret=None, issued=None):
        return asyncio.run(self.client(secret=secret).authorization_url(
            transaction=transaction() if issued is None else issued))


def assert_unavailable(provider, **kwargs):
    with pytest.raises(OIDCFailure) as caught:
        provider.authorize(**kwargs)
    error = caught.value
    assert error.kind is OIDCFailureKind.UNAVAILABLE
    assert str(error) == "OIDC login is temporarily unavailable."
    assert error.__context__ is None
    assert error.__cause__ is None
    assert PRIVATE not in repr(error)
    assert len(provider.requests) <= 1


@pytest.mark.parametrize("secret", [None, SECRET])
def test_authorization_uses_exact_trusted_parameters_without_secret_or_verifier(secret):
    provider = Discovery()
    url = provider.authorize(secret=secret)
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "identity.example.test"
    assert parsed.path == "/authorize"
    assert not parsed.fragment
    assert parse_qs(parsed.query) == {
        "client_id": [CLIENT], "redirect_uri": [REDIRECT], "response_type": ["code"],
        "scope": ["openid"], "state": [STATE], "nonce": [NONCE],
        "code_challenge": [CHALLENGE], "code_challenge_method": ["S256"],
    }
    assert VERIFIER not in url and SECRET not in url
    assert len(provider.requests) == 1
    assert {field.name for field in fields(transaction())} == {
        "state", "nonce", "code_challenge", "code_challenge_method"}
    assert STATE not in repr(transaction()) and NONCE not in repr(transaction())


def test_ordinary_authorization_endpoint_options_are_preserved():
    provider = Discovery(payload=metadata(authorization_endpoint=AUTHORIZATION + "?tenant=one&prompt=login"))
    parameters = parse_qs(urlsplit(provider.authorize()).query)
    assert parameters["tenant"] == ["one"]
    assert parameters["prompt"] == ["login"]
    assert parameters["scope"] == ["openid"]
    assert len(provider.requests) == 1


@pytest.mark.parametrize("name", [
    "client_id", "redirect_uri", "response_type", "scope", "state", "nonce",
    "code_challenge", "code_challenge_method", "response_mode",
])
@pytest.mark.parametrize("query_shape", ["{}=attacker", "{}=", "{}", "{}=a&{}=b"])
def test_reserved_authorization_query_parameters_are_rejected(name, query_shape):
    provider = Discovery(payload=metadata(authorization_endpoint=AUTHORIZATION + "?" + query_shape.format(name, name)))
    assert_unavailable(provider)
    assert len(provider.requests) == 1


def test_encoded_reserved_parameter_is_rejected():
    provider = Discovery(payload=metadata(authorization_endpoint=AUTHORIZATION + "?sta%74e=attacker"))
    assert_unavailable(provider)


@pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
@pytest.mark.parametrize("value", [
    "http://identity.example.test/endpoint", "https://user:password@identity.example.test/endpoint",
    "https://identity.example.test/endpoint#fragment", "//identity.example.test/endpoint",
    "", None, 12,
])
def test_all_discovery_endpoints_use_existing_https_security_policy(field, value):
    assert_unavailable(Discovery(payload=metadata(**{field: value})))


@pytest.mark.parametrize("payload", [
    [], "not metadata", 1, {}, metadata(issuer="https://attacker.example.test"),
    metadata(issuer=ISSUER + "/"), metadata(issuer=None),
])
def test_malformed_or_mismatched_discovery_fails_closed(payload):
    assert_unavailable(Discovery(payload=payload))


@pytest.mark.parametrize("raw", [b"null", b"{", PRIVATE.encode()])
def test_invalid_discovery_json_is_sanitized(raw):
    assert_unavailable(Discovery(raw=raw))


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 429, 500, 502, 503])
def test_redirect_and_provider_error_never_follow_or_retry(status):
    provider = Discovery(status=status, payload={"error_description": PRIVATE})
    assert_unavailable(provider)
    assert len(provider.requests) == 1


@pytest.mark.parametrize("error", [
    httpx2.ConnectError(PRIVATE), httpx2.ReadTimeout(PRIVATE), RuntimeError(PRIVATE),
])
def test_transport_and_unexpected_exceptions_discard_private_messages(error):
    provider = Discovery(error=error)
    assert_unavailable(provider)
    assert len(provider.requests) == 1


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_cancellation_and_system_exceptions_propagate(error_type):
    provider = Discovery(error=error_type())
    with pytest.raises(error_type):
        provider.authorize()
    assert len(provider.requests) == 1


def test_constructor_is_inert_and_call_uses_existing_http_policy(monkeypatch):
    import app.oidc_verifier as module

    original = module.AsyncOAuth2Client
    client_options = []

    class ObservedClient(original):
        def __init__(self, *args, **kwargs):
            client_options.append(kwargs.copy())
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "AsyncOAuth2Client", ObservedClient)
    provider = Discovery()
    client = provider.client()
    assert provider.requests == [] and client_options == []
    asyncio.run(client.authorization_url(transaction=transaction()))
    assert len(client_options) == 1
    options = client_options[0]
    assert options["timeout"] == OIDC_HTTP_TIMEOUT_SECONDS == 10
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False
    assert options["transport"] is provider.transport
    assert len(options["event_hooks"]["response"]) == 1
    assert len(provider.requests) == 1


@pytest.mark.parametrize("issued", [object(), {}, "raw state"])
def test_invalid_transaction_type_fails_before_http(issued):
    provider = Discovery()
    assert_unavailable(provider, issued=issued)
    assert provider.requests == []


def test_browser_return_destination_is_not_an_authorization_input():
    provider = Discovery()
    with pytest.raises(TypeError):
        asyncio.run(provider.client().authorization_url(transaction=transaction(), return_to="https://attacker.example.test"))
    assert provider.requests == []
