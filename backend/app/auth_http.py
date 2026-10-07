"""Cookie authentication, exact context binding and post-provider revalidation.

This module registers no routes and performs no interview ownership lookup.
Interview routes enforce this boundary; History remains a later slice.
Cookie issuance, OIDC, logout and frontend context bootstrap are not implemented.

The synchronous dependencies run in FastAPI's worker pool. Every invocation
resolves its own live credential; only the database factory is shared. FastAPI
controls request-validation ordering; this boundary makes no precedence claim.
"""

import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response

from app.auth import (
    AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind,
    AuthSessionStore,
)
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database import get_database_session_factory

AUTH_SESSION_COOKIE_NAME = "rehearse_auth_session"
AUTH_REQUEST_CONTEXT_HEADER = "X-Rehearse-Auth-Context"

# Explicit composition value required by the existing store constructor. This
# HTTP boundary never issues sessions; stored expires_at governs all resolution.
# The future login/cookie lifecycle must review its issuance lifetime policy.
AUTH_SESSION_LIFETIME = timedelta(hours=8)

_HTTP_FAILURES = {
    AuthenticationFailureKind.UNAUTHENTICATED: (401, "Authentication required."),
    AuthenticationFailureKind.INVALID_REQUEST_CONTEXT: (403, "Invalid authentication request context."),
    AuthenticationFailureKind.UNAVAILABLE: (503, "Authentication is temporarily unavailable."),
}


def _http_failure(kind: AuthenticationFailureKind) -> HTTPException:
    status, detail = _HTTP_FAILURES[kind]
    return HTTPException(status_code=status, detail=detail, headers={"Cache-Control": "no-store"})


def _failure_kind(error: AuthenticationFailure) -> AuthenticationFailureKind:
    # Unexpected subclasses or malformed internal metadata cannot select a body.
    if type(error) is AuthenticationFailure:
        kind = getattr(error, "kind", None)
        if type(kind) is AuthenticationFailureKind:
            return kind
    return AuthenticationFailureKind.UNAVAILABLE


def _cookie_credential(request: Request) -> str:
    credential = request.cookies.get(AUTH_SESSION_COOKIE_NAME)
    if type(credential) is not str or not credential.strip():
        raise _http_failure(AuthenticationFailureKind.UNAUTHENTICATED) from None
    return credential


def get_auth_session_store(request: Request) -> AuthSessionStore:
    """Injectable store using the same production factory as interview storage."""
    # Fail missing/blank cookies without requiring database configuration. Repeat
    # this shared guard in the principal boundary for overridden store providers.
    _cookie_credential(request)
    try:
        store = PostgreSQLAuthSessionStore(
            get_database_session_factory(), session_lifetime=AUTH_SESSION_LIFETIME,
        )
    except Exception:
        pass
    else:
        return store
    # No exception text or underlying cause survives the public failure.
    raise _http_failure(AuthenticationFailureKind.UNAVAILABLE) from None


def _context_matches(received: str | None, expected: str) -> bool:
    if type(received) is not str or not received.strip():
        return False
    try:
        received_bytes = received.encode("utf-8")
    except UnicodeEncodeError:
        return False
    # Bytes support exact non-ASCII contexts too; no stripping/case folding.
    return secrets.compare_digest(received_bytes, expected.encode("utf-8"))


def require_authenticated_principal(
    request: Request,
    response: Response,
    store: Annotated[AuthSessionStore, Depends(get_auth_session_store)],
) -> AuthenticatedPrincipal:
    """Resolve cookie, then bind context; return only the immutable principal.

    Auth failures always carry no-store. On success, the injected Response sets
    no-store for ordinary FastAPI response serialization. Future routes returning
    an explicit Response must preserve that header themselves; validation/other
    failures are not covered by this dependency's cache or ordering guarantees.
    """
    credential = _cookie_credential(request)
    failure = None
    try:
        principal = store.resolve(credential=credential)
        if type(principal) is not AuthenticatedPrincipal:
            failure = AuthenticationFailureKind.UNAVAILABLE
        elif not _context_matches(request.headers.get(AUTH_REQUEST_CONTEXT_HEADER), principal.request_context):
            failure = AuthenticationFailureKind.INVALID_REQUEST_CONTEXT
    except AuthenticationFailure as error:
        failure = _failure_kind(error)
    except Exception:
        failure = AuthenticationFailureKind.UNAVAILABLE
    if failure is not None:
        raise _http_failure(failure) from None
    response.headers["Cache-Control"] = "no-store"
    return principal


AuthenticatedPrincipalDependency = Annotated[
    AuthenticatedPrincipal, Depends(require_authenticated_principal),
]

AuthSessionStoreDependency = Annotated[AuthSessionStore, Depends(get_auth_session_store)]


def revalidate_authenticated_principal(
    principal: AuthenticatedPrincipal, store: AuthSessionStore,
) -> None:
    """Check the initiating login generation, never resolve another credential.

    The store owns a fresh short transaction. Async routes run this synchronous
    boundary in the worker pool after provider work and before protected writes
    or result release. Use the same closed HTTP failure mapping as authentication;
    cancellation and shutdown exceptions propagate without normalization.
    """
    failure = None
    try:
        if type(principal) is not AuthenticatedPrincipal:
            failure = AuthenticationFailureKind.UNAVAILABLE
        else:
            store.revalidate(principal=principal)
    except AuthenticationFailure as error:
        failure = _failure_kind(error)
    except Exception:
        failure = AuthenticationFailureKind.UNAVAILABLE
    if failure is not None:
        raise _http_failure(failure) from None


def revalidate_authenticated_principal_in_transaction(
    principal: AuthenticatedPrincipal, store: AuthSessionStore, database,
) -> None:
    """Map the adaptive transaction guard through the existing auth contract."""
    failure = None
    try:
        guard = getattr(store, "revalidate_in_transaction", None)
        if type(principal) is not AuthenticatedPrincipal or not callable(guard):
            failure = AuthenticationFailureKind.UNAVAILABLE
        elif guard(database=database, principal=principal) is not None:
            failure = AuthenticationFailureKind.UNAVAILABLE
    except AuthenticationFailure as error:
        failure = _failure_kind(error)
    except Exception:
        failure = AuthenticationFailureKind.UNAVAILABLE
    if failure is not None:
        raise _http_failure(failure) from None
