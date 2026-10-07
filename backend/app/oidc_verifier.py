"""Provider-neutral OIDC verification using Authlib and its JOSE implementation.

Use verify_login_callback from oidc_login at the login boundary: it consumes the
server transaction before invoking this stateless verifier. Trusted configuration
and consumed nonce/PKCE proof are required; decoded claims never attest identity.
Each invocation owns its HTTP client. Discovery/JWKS are fetched per invocation,
without a global metadata, token, identity or Rehearse authorization-state cache.
No HTTP lifecycle, user provisioning, local sessions or cookies are implemented.
"""

import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
import httpx2
from joserfc import jwt
from joserfc.jwk import KeySet

from app.auth import OIDCVerifier, VerifiedExternalIdentity
from app.oidc_failure import OIDCFailure, OIDCFailureKind
from app.oidc_login import IssuedLoginTransaction

_ASYMMETRIC_ALGORITHMS = frozenset({
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512", "EdDSA",
})
OIDC_HTTP_TIMEOUT_SECONDS = 10
_AUTHORIZATION_PARAMETERS = frozenset({
    "client_id", "redirect_uri", "response_type", "scope", "state", "nonce",
    "code_challenge", "code_challenge_method", "response_mode",
})


def _valid_text(value: object) -> bool:
    if type(value) is not str or not value.strip() or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _https_endpoint(value: object) -> bool:
    if not _valid_text(value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname) and
                parsed.username is None and parsed.password is None and not parsed.fragment)
    except ValueError:
        return False


def _exact(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class OIDCConfiguration:
    issuer: str
    client_id: str
    redirect_uri: str
    client_secret: str | None = field(default=None, repr=False)
    allowed_algorithms: tuple[str, ...] = ("RS256",)

    def __post_init__(self) -> None:
        if not _https_endpoint(self.issuer) or urlsplit(self.issuer).query:
            raise ValueError("OIDC issuer must be a trusted HTTPS URL without query or fragment.")
        if not _valid_text(self.client_id):
            raise ValueError("OIDC client ID must be nonblank text.")
        if not _https_endpoint(self.redirect_uri):
            raise ValueError("OIDC redirect URI must be a trusted HTTPS URL.")
        if self.client_secret is not None and not _valid_text(self.client_secret):
            raise ValueError("OIDC client secret must be nonblank text when configured.")
        if (type(self.allowed_algorithms) is not tuple or not self.allowed_algorithms or
                any(type(value) is not str or value not in _ASYMMETRIC_ALGORITHMS
                    for value in self.allowed_algorithms) or
                len(set(self.allowed_algorithms)) != len(self.allowed_algorithms)):
            raise ValueError("OIDC signing algorithms must be an explicit asymmetric allowlist.")


async def _check_provider_status(response: httpx2.Response) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        raise OIDCFailure(OIDCFailureKind.UNAVAILABLE) from None
    # Authlib's token parser alone does not reject every unsuccessful status.
    # Never accept an error/redirect response merely because its body has tokens.
    response.raise_for_status()


class AuthlibOIDCVerifier(OIDCVerifier):
    """Trusted authorization URLs and one code exchange; no retry or issuance."""

    def __init__(
        self, configuration: OIDCConfiguration, *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if type(configuration) is not OIDCConfiguration or not callable(clock):
            raise TypeError("OIDC verification requires trusted configuration and a clock.")
        self._configuration = configuration
        self._transport = transport
        self._clock = clock

    def _client(self) -> AsyncOAuth2Client:
        config = self._configuration
        return AsyncOAuth2Client(
            client_id=config.client_id, client_secret=config.client_secret,
            redirect_uri=config.redirect_uri, code_challenge_method="S256",
            token_endpoint_auth_method="client_secret_basic" if config.client_secret is not None else "none",
            transport=self._transport, timeout=OIDC_HTTP_TIMEOUT_SECONDS,
            trust_env=False, follow_redirects=False,
            event_hooks={"response": [_check_provider_status]},
        )

    async def _provider_metadata(self, client: AsyncOAuth2Client) -> dict:
        config = self._configuration
        response = await client.request(
            "GET", config.issuer.rstrip("/") + "/.well-known/openid-configuration", withhold_token=True,
        )
        response.raise_for_status()
        metadata = response.json()
        if (type(metadata) is not dict or metadata.get("issuer") != config.issuer or
                not _https_endpoint(metadata.get("authorization_endpoint")) or
                not _https_endpoint(metadata.get("token_endpoint")) or
                not _https_endpoint(metadata.get("jwks_uri"))):
            raise OIDCFailure(OIDCFailureKind.UNAVAILABLE)
        return metadata

    async def authorization_url(self, *, transaction: IssuedLoginTransaction) -> str:
        """One trusted discovery request; no token exchange or browser return URL."""
        try:
            if type(transaction) is not IssuedLoginTransaction:
                raise OIDCFailure(OIDCFailureKind.UNAVAILABLE)
            async with self._client() as client:
                metadata = await self._provider_metadata(client)
                endpoint = metadata["authorization_endpoint"]
                # Authlib preserves endpoint query parameters. Reject any that
                # would make the security parameters ambiguous or duplicated.
                if any(name in _AUTHORIZATION_PARAMETERS for name, _ in
                       parse_qsl(urlsplit(endpoint).query, keep_blank_values=True)):
                    raise OIDCFailure(OIDCFailureKind.UNAVAILABLE)
                url, _ = client.create_authorization_url(
                    endpoint, response_type="code", scope="openid",
                    state=transaction.state, nonce=transaction.nonce,
                    code_challenge=transaction.code_challenge,
                    code_challenge_method="S256",
                )
                return url
        except Exception:
            pass
        # Discard provider/transport exceptions and secret-bearing chains.
        raise OIDCFailure(OIDCFailureKind.UNAVAILABLE) from None

    def _now(self) -> float:
        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise OIDCFailure(OIDCFailureKind.UNAVAILABLE)
        return now.timestamp()

    def _validate_claims(self, claims: dict, header: dict, nonce: str, access_token: str) -> None:
        config = self._configuration
        if any(not _valid_text(claims.get(name)) for name in ("iss", "sub", "nonce")):
            raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
        if not _exact(claims["iss"], config.issuer) or not _exact(claims["nonce"], nonce):
            raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
        audience = claims.get("aud")
        audiences = [audience] if type(audience) is str else audience
        if (type(audiences) is not list or not audiences or
                any(not _valid_text(value) for value in audiences) or config.client_id not in audiences):
            raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
        if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != config.client_id:
            raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
        # Tighten NumericDate boundary/type checks after signature verification.
        # No clock leeway, booleans, NaN or Infinity may extend token validity.
        now = self._now()
        for name in ("exp", "iat", *(["nbf"] if "nbf" in claims else [])):
            value = claims.get(name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
        if now >= claims["exp"] or now < claims["iat"] or now < claims.get("nbf", now):
            raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
        CodeIDToken(
            claims, header,
            options={"iss": {"essential": True, "value": config.issuer},
                     "aud": {"essential": True, "value": config.client_id}},
            params={"nonce": nonce, "client_id": config.client_id, "access_token": access_token},
        ).validate(now=now, leeway=0)

    async def verify_callback(
        self, *, code: str, state: str, expected_state: str,
        expected_nonce: str, code_verifier: str,
    ) -> VerifiedExternalIdentity:
        phase = OIDCFailureKind.UNAVAILABLE
        try:
            if (not _valid_text(state) or not _valid_text(expected_state) or
                    not _exact(state, expected_state)):
                raise OIDCFailure(OIDCFailureKind.INVALID_STATE)
            if not _valid_text(code) or not _valid_text(expected_nonce):
                raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
            # The trusted transaction store enforces the RFC 7636 verifier shape.
            from app.oidc_login import _valid_verifier
            if not _valid_verifier(code_verifier):
                raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
            self._now()
            config = self._configuration
            async with self._client() as client:
                metadata = await self._provider_metadata(client)
                phase = OIDCFailureKind.EXCHANGE_FAILED
                token = await client.fetch_token(
                    metadata["token_endpoint"], grant_type="authorization_code",
                    code=code, code_verifier=code_verifier,
                )
                phase = OIDCFailureKind.INVALID_TOKEN
                if (not isinstance(token, dict) or not _valid_text(token.get("id_token")) or
                        not _valid_text(token.get("access_token")) or
                        type(token.get("token_type")) is not str or token["token_type"].lower() != "bearer"):
                    raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
                phase = OIDCFailureKind.UNAVAILABLE
                # Never send the exchanged access token to the JWKS endpoint.
                jwks_response = await client.request("GET", metadata["jwks_uri"], withhold_token=True)
                jwks_response.raise_for_status()
                jwks = jwks_response.json()
                if type(jwks) is not dict or type(jwks.get("keys")) is not list or not jwks["keys"]:
                    raise OIDCFailure(OIDCFailureKind.UNAVAILABLE)
                keys = KeySet.import_key_set(jwks)
                phase = OIDCFailureKind.INVALID_TOKEN
                verified = jwt.decode(token["id_token"], keys, algorithms=list(config.allowed_algorithms))
                self._validate_claims(verified.claims, verified.header, expected_nonce, token["access_token"])
                return VerifiedExternalIdentity(issuer=verified.claims["iss"], subject=verified.claims["sub"])
        except OIDCFailure as error:
            kind = getattr(error, "kind", None) if type(error) is OIDCFailure else None
            if type(kind) is not OIDCFailureKind:
                kind = OIDCFailureKind.UNAVAILABLE
        except (httpx2.TransportError, httpx2.TimeoutException):
            kind = OIDCFailureKind.UNAVAILABLE
        except Exception:
            kind = phase
        # Outside the handler: no token/provider exception chain survives.
        raise OIDCFailure(kind) from None
