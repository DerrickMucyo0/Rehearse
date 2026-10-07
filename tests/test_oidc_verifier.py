"""Actual OIDC/JWS verification with local keys and in-process HTTP fixtures.

Every discovery, code exchange and JWKS request uses MockTransport. The tests
exercise Authlib and joserfc rather than faking verified claims. No PostgreSQL,
OIDC provider, cookie lifecycle or local authentication issuance is involved.
"""

import asyncio
import base64
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import traceback
from urllib.parse import parse_qs

import httpx
import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import ECKey, RSAKey

from app.auth import OIDCVerifier, VerifiedExternalIdentity
from app.oidc_failure import OIDCFailure, OIDCFailureKind
from app.oidc_verifier import AuthlibOIDCVerifier, OIDCConfiguration

ISSUER = "https://identity.example.test/tenant"
CLIENT_ID = "rehearse-test-client"
REDIRECT_URI = "https://rehearse.example.test/api/auth/callback"
DISCOVERY_URI = ISSUER + "/.well-known/openid-configuration"
AUTHORIZATION_URI = "https://identity.example.test/authorize"
TOKEN_URI = "https://identity.example.test/token"
JWKS_URI = "https://identity.example.test/jwks"
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
STATE = "SYNTHETIC_STATE_" + "s" * 48
NONCE = "SYNTHETIC_NONCE_" + "n" * 48
VERIFIER = "SYNTHETIC_PKCE_" + "v" * 48
CODE = "SYNTHETIC_AUTHORIZATION_CODE_" + "a" * 32
CLIENT_SECRET = "SYNTHETIC_CLIENT_SECRET_" + "c" * 32
ACCESS_TOKEN = "SYNTHETIC_ACCESS_TOKEN_" + "a" * 32
REFRESH_TOKEN = "SYNTHETIC_REFRESH_TOKEN_" + "r" * 32
EMAIL = "profile-only@example.test"
SUBJECT = "  Case-sensitive / opaque-咖啡-e\u0301  "
PRIVATE = "PRIVATE_OIDC_PROVIDER_FAILURE_SENTINEL"

FAILURE_MESSAGES = {
    OIDCFailureKind.INVALID_STATE: "Invalid login state.",
    OIDCFailureKind.EXCHANGE_FAILED: "Unable to exchange authorization code.",
    OIDCFailureKind.INVALID_TOKEN: "Invalid identity token.",
    OIDCFailureKind.UNAVAILABLE: "OIDC login is temporarily unavailable.",
}


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    def blocked(*args, **kwargs):
        raise AssertionError("OIDC tests must use in-process MockTransport.")

    async def blocked_async(*args, **kwargs):
        blocked()

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.HTTPTransport, "handle_request", blocked)
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", blocked_async)
    yield
    for private in (PRIVATE, CODE, STATE, NONCE, VERIFIER, CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN):
        assert private not in caplog.text


@pytest.fixture(scope="module")
def signing_keys():
    return {
        "trusted": RSAKey.generate_key(2048, parameters={"kid": "trusted-key", "use": "sig"}),
        "rogue": RSAKey.generate_key(2048, parameters={"kid": "trusted-key", "use": "sig"}),
        "ec": ECKey.generate_key("P-256", parameters={"kid": "ec-key", "use": "sig"}),
    }


def valid_claims(**changes):
    claims = {
        "iss": ISSUER, "sub": SUBJECT, "aud": CLIENT_ID,
        "iat": int(NOW.timestamp()) - 60, "exp": int(NOW.timestamp()) + 300,
        "nonce": NONCE, "email": EMAIL, "email_verified": False,
        "name": "Ignored profile name", "picture": "https://profile.example.test/avatar",
    }
    claims.update(changes)
    return claims


def signed_token(keys, *, claims=None, key="trusted", algorithm="RS256", header=None):
    supplied = {"alg": algorithm, "kid": "ec-key" if key == "ec" else "trusted-key", "typ": "JWT"}
    supplied.update(header or {})
    return jwt.encode(supplied, valid_claims() if claims is None else claims, keys[key])


class LocalProvider:
    def __init__(self, keys):
        self.keys = keys
        self.requests = []
        self.discovery = {
            "issuer": ISSUER, "authorization_endpoint": AUTHORIZATION_URI,
            "token_endpoint": TOKEN_URI, "jwks_uri": JWKS_URI,
            "response_types_supported": ["code"], "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
        }
        self.jwks = {"keys": [keys["trusted"].as_dict(private=False)]}
        self.token_response = {
            "id_token": signed_token(keys), "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN, "token_type": "Bearer", "expires_in": 3600,
        }
        self.statuses = {}
        self.errors = {}
        self.raw_responses = {}
        self.transport = httpx2.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        stage = {
            DISCOVERY_URI: "discovery", TOKEN_URI: "exchange", JWKS_URI: "jwks",
        }.get(str(request.url))
        if stage is None:
            raise AssertionError(f"Unexpected in-process OIDC URL: {request.url}")
        if stage in self.errors:
            raise self.errors[stage]
        status = self.statuses.get(stage, 200)
        if stage in self.raw_responses:
            return httpx2.Response(status, content=self.raw_responses[stage],
                                   headers={"Content-Type": "application/json"})
        payload = {"discovery": self.discovery, "exchange": self.token_response, "jwks": self.jwks}[stage]
        return httpx2.Response(status, json=payload)

    def verifier(self, *, secret=None, algorithms=("RS256",), clock=lambda: NOW):
        return AuthlibOIDCVerifier(OIDCConfiguration(
            issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT_URI,
            client_secret=secret, allowed_algorithms=algorithms,
        ), transport=self.transport, clock=clock)

    def callback(self, verifier=None, **changes):
        supplied = {
            "code": CODE, "state": STATE, "expected_state": STATE,
            "expected_nonce": NONCE, "code_verifier": VERIFIER,
        }
        supplied.update(changes)
        return asyncio.run((verifier or self.verifier()).verify_callback(**supplied))

    def exchange_requests(self):
        return [request for request in self.requests if str(request.url) == TOKEN_URI]


@pytest.fixture
def provider(signing_keys):
    return LocalProvider(signing_keys)


def assert_failure(provider, kind, *, verifier=None, **changes):
    with pytest.raises(OIDCFailure) as caught:
        provider.callback(verifier, **changes)
    failure = caught.value
    assert type(failure) is OIDCFailure
    assert failure.kind is kind
    assert str(failure) == FAILURE_MESSAGES[kind]
    assert failure.args == (FAILURE_MESSAGES[kind],)
    assert failure.__cause__ is None
    assert failure.__context__ is None
    assert failure.__suppress_context__ is True
    public_error = repr(failure) + str(failure) + "".join(traceback.format_exception(failure))
    for private in (PRIVATE, CODE, STATE, NONCE, VERIFIER, CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN, EMAIL):
        assert private not in public_error
    return failure


@pytest.mark.parametrize("secret", [None, CLIENT_SECRET], ids=["public-client", "confidential-client"])
def test_real_library_verifies_identity_and_exact_pkce_code_exchange(provider, secret, caplog):
    verifier: OIDCVerifier = provider.verifier(secret=secret)
    identity = provider.callback(verifier)
    assert type(identity) is VerifiedExternalIdentity
    assert identity == VerifiedExternalIdentity(issuer=ISSUER, subject=SUBJECT)
    assert {field.name for field in fields(identity)} == {"issuer", "subject"}
    assert EMAIL not in repr(identity)
    assert [str(request.url) for request in provider.requests] == [DISCOVERY_URI, TOKEN_URI, JWKS_URI]
    discovery, exchange, jwks = provider.requests
    assert discovery.method == jwks.method == "GET"
    assert exchange.method == "POST"
    body = parse_qs(exchange.content.decode("ascii"), strict_parsing=True)
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == [CODE]
    assert body["redirect_uri"] == [REDIRECT_URI]
    assert body["code_verifier"] == [VERIFIER]
    assert "code_challenge" not in body
    assert "state" not in body and "nonce" not in body
    if secret is None:
        assert body["client_id"] == [CLIENT_ID]
        assert "Authorization" not in exchange.headers
    else:
        assert exchange.headers["Authorization"] == "Basic " + base64.b64encode(
            f"{CLIENT_ID}:{CLIENT_SECRET}".encode("ascii")
        ).decode("ascii")
        assert "client_secret" not in body
    assert ACCESS_TOKEN not in json.dumps(body) and REFRESH_TOKEN not in json.dumps(body)
    assert "Cookie" not in exchange.headers
    assert all("Authorization" not in request.headers for request in (discovery, jwks))
    assert provider.token_response["id_token"] not in caplog.text
    for private in (CODE, STATE, NONCE, VERIFIER, CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN):
        assert private not in repr(verifier)
        assert private not in caplog.text


@pytest.mark.parametrize("state", ["different", STATE.lower(), " " + STATE, STATE + " ", "", "   ", None, 1, True, b"state", "bad\x00state", "\ud800"])
def test_invalid_state_fails_exactly_before_any_discovery_or_exchange(provider, state):
    assert_failure(provider, OIDCFailureKind.INVALID_STATE, state=state)
    assert provider.requests == []


def test_unicode_state_comparison_remains_exact_and_is_not_normalized(provider):
    state = "咖啡 / e\u0301 login state"
    assert provider.callback(state=state, expected_state=state) == VerifiedExternalIdentity(issuer=ISSUER, subject=SUBJECT)
    provider.requests.clear()
    assert_failure(provider, OIDCFailureKind.INVALID_STATE, state=state, expected_state="咖啡 / é login state")
    assert provider.requests == []


@pytest.mark.parametrize("field,value", [
    ("expected_state", None), ("expected_state", True), ("expected_state", ""),
])
def test_malformed_expected_state_is_not_a_trusted_login_proof(provider, field, value):
    assert_failure(provider, OIDCFailureKind.INVALID_STATE, **{field: value})
    assert provider.requests == []


@pytest.mark.parametrize("field,value", [
    ("code", ""), ("code", None), ("code", True),
    ("expected_nonce", ""), ("expected_nonce", None), ("expected_nonce", True),
    ("code_verifier", "short"), ("code_verifier", "v" * 129),
    ("code_verifier", "v" * 42 + " "), ("code_verifier", None), ("code_verifier", True),
])
def test_malformed_callback_proofs_fail_without_remote_code_exchange(provider, field, value):
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN, **{field: value})
    assert provider.requests == []


@pytest.mark.parametrize("claim,value", [
    ("iss", "https://another-issuer.example.test"), ("iss", ISSUER + "/"),
    ("aud", "another-client"), ("aud", ["another-client"]),
    ("nonce", "another-nonce"), ("nonce", NONCE.lower()),
    ("sub", ""), ("sub", "   "), ("sub", None), ("sub", 5), ("sub", True), ("sub", ["subject"]),
    ("sub", "bad\x00subject"), ("azp", "another-client"),
    ("iat", int(NOW.timestamp()) + 1), ("nbf", int(NOW.timestamp()) + 1),
])
def test_verified_jwt_with_invalid_identity_or_binding_claims_is_rejected(provider, claim, value):
    provider.token_response["id_token"] = signed_token(provider.keys, claims=valid_claims(**{claim: value}))
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)
    assert len(provider.exchange_requests()) == 1


@pytest.mark.parametrize("missing", ["iss", "aud", "exp", "iat", "nonce", "sub"])
def test_mandatory_oidc_claims_cannot_be_replaced_by_profile_fields(provider, missing):
    claims = valid_claims(email=EMAIL, email_verified=True)
    claims.pop(missing)
    provider.token_response["id_token"] = signed_token(provider.keys, claims=claims)
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)


@pytest.mark.parametrize("claim", ["exp", "iat", "nbf"])
@pytest.mark.parametrize("value", [True, False, math.nan, math.inf, -math.inf, "1780000000", None])
def test_numeric_dates_require_finite_nonboolean_numbers(provider, claim, value):
    provider.token_response["id_token"] = signed_token(provider.keys, claims=valid_claims(**{claim: value}))
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)


@pytest.mark.parametrize("offset", [-timedelta(microseconds=1), timedelta(0), timedelta(microseconds=1)])
def test_expiry_uses_exact_injected_clock_boundary_without_sleep(provider, offset):
    expiry = NOW.timestamp()
    provider.token_response["id_token"] = signed_token(provider.keys, claims=valid_claims(exp=expiry))
    verifier = provider.verifier(clock=lambda: NOW + offset)
    if offset < timedelta(0):
        assert provider.callback(verifier) == VerifiedExternalIdentity(issuer=ISSUER, subject=SUBJECT)
    else:
        assert_failure(provider, OIDCFailureKind.INVALID_TOKEN, verifier=verifier)


@pytest.mark.parametrize("claims", [
    {"aud": [CLIENT_ID, "additional-audience"], "azp": CLIENT_ID},
    {"aud": [CLIENT_ID], "azp": CLIENT_ID},
])
def test_valid_audience_shapes_and_authorized_party_are_verified(provider, claims):
    provider.token_response["id_token"] = signed_token(provider.keys, claims=valid_claims(**claims))
    assert provider.callback() == VerifiedExternalIdentity(issuer=ISSUER, subject=SUBJECT)


@pytest.mark.parametrize("azp", [None, "another-client"])
def test_multiple_audiences_need_the_configured_authorized_party(provider, azp):
    claims = valid_claims(aud=[CLIENT_ID, "additional-audience"])
    if azp is not None:
        claims["azp"] = azp
    provider.token_response["id_token"] = signed_token(provider.keys, claims=claims)
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)


@pytest.mark.parametrize("nonce_claim", [None, "another-nonce"])
def test_provider_nonce_supported_false_cannot_disable_required_nonce(provider, nonce_claim):
    claims = valid_claims(nonce_supported=False)
    if nonce_claim is None:
        claims.pop("nonce")
    else:
        claims["nonce"] = nonce_claim
    provider.token_response["id_token"] = signed_token(provider.keys, claims=claims)
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)


def test_signature_with_matching_kid_but_another_local_key_is_rejected(provider):
    provider.token_response["id_token"] = signed_token(provider.keys, key="rogue")
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)
    assert len(provider.exchange_requests()) == 1


def test_provider_advertised_algorithm_cannot_expand_configured_allowlist(provider):
    provider.jwks["keys"].append(provider.keys["ec"].as_dict(private=False))
    provider.token_response["id_token"] = signed_token(provider.keys, key="ec", algorithm="ES256")
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)


def test_explicitly_configured_asymmetric_algorithm_uses_the_actual_verifier(provider):
    provider.jwks = {"keys": [provider.keys["ec"].as_dict(private=False)]}
    provider.token_response["id_token"] = signed_token(provider.keys, key="ec", algorithm="ES256")
    assert provider.callback(provider.verifier(algorithms=("ES256",))) == VerifiedExternalIdentity(
        issuer=ISSUER, subject=SUBJECT,
    )


def test_unsigned_none_token_cannot_be_an_identity(provider):
    def encoded(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    provider.token_response["id_token"] = encoded({"alg": "none"}) + "." + encoded(valid_claims()) + "."
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)


@pytest.mark.parametrize("id_token", [None, 5, True, {}, [], "", "not-a-jwt", "a.b.c", "PRIVATE.ID.TOKEN"])
def test_malformed_or_missing_id_token_response_is_sanitized(provider, id_token):
    if id_token is None:
        provider.token_response.pop("id_token")
    else:
        provider.token_response["id_token"] = id_token
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)
    assert len(provider.exchange_requests()) == 1


def test_null_id_token_is_not_a_successful_identity(provider):
    provider.token_response["id_token"] = None
    assert_failure(provider, OIDCFailureKind.INVALID_TOKEN)


def test_email_and_profile_changes_do_not_change_exact_issuer_subject_identity(provider):
    first = provider.callback()
    provider.token_response["id_token"] = signed_token(provider.keys, claims=valid_claims(
        email="different@example.test", email_verified=True, name=PRIVATE, picture=PRIVATE,
    ))
    second = provider.callback()
    assert first == second == VerifiedExternalIdentity(issuer=ISSUER, subject=SUBJECT)
    assert not hasattr(first, "__dict__")
    assert not hasattr(first, "email") and not hasattr(first, "access_token") and not hasattr(first, "refresh_token")
    assert len(provider.exchange_requests()) == 2


@pytest.mark.parametrize("stage", ["discovery", "exchange", "jwks"])
def test_transport_timeouts_are_fixed_and_never_retried(provider, stage):
    provider.errors[stage] = httpx2.ReadTimeout(PRIVATE)
    assert_failure(provider, OIDCFailureKind.UNAVAILABLE)
    assert sum(str(request.url) == {"discovery": DISCOVERY_URI, "exchange": TOKEN_URI, "jwks": JWKS_URI}[stage]
               for request in provider.requests) == 1


@pytest.mark.parametrize("stage", ["discovery", "exchange", "jwks"])
@pytest.mark.parametrize("status", [429, 503])
def test_http_server_outages_are_fixed_and_never_retried(provider, stage, status):
    provider.statuses[stage] = status
    provider.raw_responses[stage] = PRIVATE.encode()
    assert_failure(provider, OIDCFailureKind.UNAVAILABLE)
    assert sum(str(request.url) == {"discovery": DISCOVERY_URI, "exchange": TOKEN_URI, "jwks": JWKS_URI}[stage]
               for request in provider.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 302])
def test_unsuccessful_exchange_status_cannot_be_accepted_as_a_verified_identity(provider, status):
    # Keep the otherwise valid, locally signed token body: HTTP must succeed too.
    provider.statuses["exchange"] = status
    assert_failure(provider, OIDCFailureKind.EXCHANGE_FAILED)
    assert len(provider.exchange_requests()) == 1
    assert [str(request.url) for request in provider.requests] == [DISCOVERY_URI, TOKEN_URI]


def test_oauth_exchange_failure_discards_provider_error_description_and_chain(provider):
    provider.statuses["exchange"] = 400
    provider.token_response = {"error": "invalid_grant", "error_description": PRIVATE + CODE + VERIFIER}
    assert_failure(provider, OIDCFailureKind.EXCHANGE_FAILED)
    assert len(provider.exchange_requests()) == 1
    assert all(str(request.url) != JWKS_URI for request in provider.requests)


@pytest.mark.parametrize("stage", ["discovery", "exchange", "jwks"])
def test_cancellation_propagates_without_failure_conversion_or_retry(provider, stage):
    cancellation = asyncio.CancelledError(PRIVATE)
    provider.errors[stage] = cancellation
    with pytest.raises(asyncio.CancelledError) as caught:
        provider.callback()
    assert caught.value is cancellation
    assert sum(str(request.url) == {"discovery": DISCOVERY_URI, "exchange": TOKEN_URI, "jwks": JWKS_URI}[stage]
               for request in provider.requests) == 1


def test_configuration_hides_client_secret_and_is_immutable():
    configuration = OIDCConfiguration(issuer=ISSUER, client_id=CLIENT_ID,
                                      redirect_uri=REDIRECT_URI, client_secret=CLIENT_SECRET)
    assert CLIENT_SECRET not in repr(configuration)
    with pytest.raises(AttributeError):
        configuration.client_id = "another-client"


@pytest.mark.parametrize("algorithm", ["none", "HS256", "HS384", "HS512", "unknown"])
def test_configuration_rejects_symmetric_unsigned_and_unknown_algorithms(algorithm):
    with pytest.raises((TypeError, ValueError)):
        OIDCConfiguration(issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT_URI,
                          allowed_algorithms=(algorithm,))


@pytest.mark.parametrize("issuer", ["http://identity.example.test", "https://user:secret@identity.example.test",
                                    "https://identity.example.test/?query=1", "https://identity.example.test/#fragment"])
def test_configuration_rejects_untrusted_issuer_urls(issuer):
    with pytest.raises((TypeError, ValueError)):
        OIDCConfiguration(issuer=issuer, client_id=CLIENT_ID, redirect_uri=REDIRECT_URI)


@pytest.mark.parametrize("uri", [
    "https://app.example/callback", "http://localhost:8000/api/auth/callback",
    "http://127.0.0.1:8000/api/auth/callback", "http://[::1]:8000/api/auth/callback",
])
def test_callback_uri_uses_https_or_only_explicit_loopback_http(uri):
    config = OIDCConfiguration(issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=uri)
    assert config.redirect_uri == uri


@pytest.mark.parametrize("uri", [
    "http://0.0.0.0/callback", "http://192.168.1.2/callback", "http://10.1.2.3/callback",
    "http://172.16.0.2/callback", "http://8.8.8.8/callback", "http://example.com/callback",
    "http://localhost.example.com/callback", "http://example.localhost/callback", "http://*/callback",
    "http://127.1/callback", "http://localhost./callback", "http://[::]/callback",
    "http://localhost:wrong/callback", "http://localhost:65536/callback", "http://localhost:0/callback",
    "http://localhost:/callback", "http://user:password@localhost/callback", "http://localhost/callback?",
    "http://localhost/callback#", "http://local host/callback", "http://localhost\\other/callback",
    " http://localhost/callback", "http://localhost/call\nback", "http://localhost/call\x00back",
    "http://[invalid]/callback",
    "http://[::1]evil:8000/callback", "http://[::1].example:8000/callback", "http://[::1]suffix/callback",
])
def test_http_callback_exception_rejects_non_loopback_or_malformed_uris(uri):
    with pytest.raises(ValueError):
        OIDCConfiguration(issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=uri)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_callback_does_not_relax_https_issuer_policy(host):
    with pytest.raises(ValueError):
        OIDCConfiguration(
            issuer=f"http://{host}:9000", client_id=CLIENT_ID,
            redirect_uri=f"http://{host}:8000/api/auth/callback",
        )


def test_loopback_callback_preserves_real_library_exchange_and_https_provider_transport(provider):
    callback = "http://localhost:8000/api/auth/callback"
    verifier = AuthlibOIDCVerifier(
        OIDCConfiguration(issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=callback),
        transport=provider.transport, clock=lambda: NOW,
    )
    assert provider.callback(verifier) == VerifiedExternalIdentity(issuer=ISSUER, subject=SUBJECT)
    assert [str(request.url) for request in provider.requests] == [DISCOVERY_URI, TOKEN_URI, JWKS_URI]
    assert all(request.url.scheme == "https" for request in provider.requests)
    body = parse_qs(provider.exchange_requests()[0].content.decode("ascii"))
    assert body["redirect_uri"] == [callback]
    assert body["code_verifier"] == [VERIFIER]


@pytest.mark.parametrize("endpoint", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
def test_loopback_redirect_exception_does_not_allow_http_provider_endpoints(provider, endpoint):
    provider.discovery[endpoint] = "http://localhost:9000/provider"
    verifier = AuthlibOIDCVerifier(
        OIDCConfiguration(
            issuer=ISSUER, client_id=CLIENT_ID, redirect_uri="http://localhost:8000/api/auth/callback",
        ), transport=provider.transport, clock=lambda: NOW,
    )
    assert_failure(provider, OIDCFailureKind.UNAVAILABLE, verifier=verifier)
    assert [str(request.url) for request in provider.requests] == [DISCOVERY_URI]


def test_discovery_issuer_mismatch_fails_before_sending_code_or_verifier(provider):
    provider.discovery["issuer"] = "https://another-issuer.example.test"
    assert_failure(provider, OIDCFailureKind.UNAVAILABLE)
    assert [str(request.url) for request in provider.requests] == [DISCOVERY_URI]


@pytest.mark.parametrize("endpoint", ["token_endpoint", "jwks_uri"])
@pytest.mark.parametrize("unsafe", ["http://attacker.example.test/path", "https://secret@attacker.example.test/path", "https://attacker.example.test/path#fragment"])
def test_untrusted_discovery_endpoint_urls_fail_before_code_exchange(provider, endpoint, unsafe):
    provider.discovery[endpoint] = unsafe
    assert_failure(provider, OIDCFailureKind.UNAVAILABLE)
    assert [str(request.url) for request in provider.requests] == [DISCOVERY_URI]


@pytest.mark.parametrize("discovery", [None, [], {}, {"issuer": ISSUER}, {"issuer": ISSUER, "token_endpoint": TOKEN_URI}])
def test_malformed_discovery_is_a_fixed_infrastructure_failure(provider, discovery):
    provider.discovery = discovery
    assert_failure(provider, OIDCFailureKind.UNAVAILABLE)
    assert [str(request.url) for request in provider.requests] == [DISCOVERY_URI]


@pytest.mark.parametrize("jwks", [None, [], {}, {"keys": []}, {"keys": "not-a-key-set"}, {"keys": [{"kty": "unknown"}]}])
def test_malformed_jwks_is_a_fixed_infrastructure_failure(provider, jwks):
    provider.jwks = jwks
    assert_failure(provider, OIDCFailureKind.UNAVAILABLE)
    assert len(provider.exchange_requests()) == 1


@pytest.mark.parametrize("stage,kind", [
    ("discovery", OIDCFailureKind.UNAVAILABLE),
    ("exchange", OIDCFailureKind.EXCHANGE_FAILED),
    ("jwks", OIDCFailureKind.UNAVAILABLE),
])
def test_malformed_protocol_json_drops_the_entire_raw_provider_body(provider, stage, kind):
    provider.raw_responses[stage] = (PRIVATE + CODE + STATE + NONCE + VERIFIER + CLIENT_SECRET).encode()
    assert_failure(provider, kind)


def test_metadata_jwks_and_provider_tokens_are_not_cached_as_authorization_state(provider):
    verifier = provider.verifier()
    first = provider.callback(verifier)
    provider.token_response["id_token"] = signed_token(provider.keys, claims=valid_claims(sub="another-exact-subject"))
    second = provider.callback(verifier)
    assert first.subject == SUBJECT and second.subject == "another-exact-subject"
    assert [str(request.url) for request in provider.requests] == [DISCOVERY_URI, TOKEN_URI, JWKS_URI] * 2
    assert not hasattr(verifier, "token") and not hasattr(verifier, "identity") and not hasattr(verifier, "principal")
