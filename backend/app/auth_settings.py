"""Explicit trusted settings for the backend browser authentication lifecycle.

Loading is lazy and uncached. This boundary owns no request identity, database,
HTTP client or principal. HTTPS deployment is required in this slice; local
insecure-cookie exceptions are deliberately not part of this contract.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
import os
from urllib.parse import urlsplit

from app.auth import AuthenticationFailure, AuthenticationFailureKind
from app.oidc_verifier import OIDCConfiguration

AUTH_SESSION_MIN_TTL_SECONDS = 60
AUTH_SESSION_MAX_TTL_SECONDS = 86400


def _trusted_https_url(value: object) -> bool:
    if (type(value) is not str or not value or
            any(character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in value) or "\\" in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname) and
                parsed.username is None and parsed.password is None and
                (parsed.port is None or 1 <= parsed.port <= 65535) and
                "?" not in value and "#" not in value)
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """Validated server configuration, never caller-selected callback values."""

    oidc: OIDCConfiguration
    app_origin: str
    session_ttl_seconds: int

    def __post_init__(self) -> None:
        if type(self.oidc) is not OIDCConfiguration:
            raise TypeError("Authentication requires trusted OIDC configuration.")
        if (not _trusted_https_url(self.oidc.issuer) or
                not _trusted_https_url(self.oidc.redirect_uri) or
                urlsplit(self.oidc.redirect_uri).path != "/api/auth/callback"):
            raise ValueError("Authentication OIDC URLs must use the trusted HTTPS callback policy.")
        if (not _trusted_https_url(self.app_origin) or
                urlsplit(self.app_origin).path not in ("", "/")):
            raise ValueError("Authentication application origin must be a trusted HTTPS root origin.")
        if type(self.session_ttl_seconds) is not int:
            raise TypeError("Authentication session lifetime must be an integer.")
        if not AUTH_SESSION_MIN_TTL_SECONDS <= self.session_ttl_seconds <= AUTH_SESSION_MAX_TTL_SECONDS:
            raise ValueError("Authentication session lifetime is outside the supported bounds.")

    @property
    def session_lifetime(self) -> timedelta:
        return timedelta(seconds=self.session_ttl_seconds)


def load_auth_settings(environment: Mapping[str, str] | None = None) -> AuthSettings:
    """Read explicit settings only when invoked; missing/invalid settings fail closed."""
    try:
        source = os.environ if environment is None else environment
        ttl = source["AUTH_SESSION_TTL_SECONDS"]
        if type(ttl) is not str or not ttl or not ttl.isascii() or not ttl.isdecimal():
            raise ValueError("Authentication session lifetime must use decimal seconds.")
        settings = AuthSettings(
            oidc=OIDCConfiguration(
                issuer=source["AUTH_OIDC_ISSUER"],
                client_id=source["AUTH_OIDC_CLIENT_ID"],
                client_secret=source.get("AUTH_OIDC_CLIENT_SECRET"),
                redirect_uri=source["AUTH_OIDC_REDIRECT_URI"],
            ),
            app_origin=source["AUTH_APP_ORIGIN"],
            session_ttl_seconds=int(ttl),
        )
    except Exception:
        pass
    else:
        return settings
    # The configuration exception may contain secret values; discard its chain.
    raise AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE) from None
