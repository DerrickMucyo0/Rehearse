"""Browser OIDC lifecycle, without changing interview/history authorization.

Binding cookies are not authentication. Consume a matching one-time transaction
before provider work; provisioning and local issuance then own short worker-thread
transactions. Issued credentials reach only HttpOnly cookies. No query, cookie,
provider or identity values are logged here. Deployment access logs must also
exclude sensitive callback query strings.

A committed local session whose response is never delivered can remain until its
fixed expiry. There is no network-spanning transaction or compensating replay.
HTTPS is required; this slice adds no insecure development-cookie exception.
"""

import re
import secrets
from typing import Annotated, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.auth import (
    AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind,
    IssuedAuthSession, OIDCVerifier, VerifiedExternalIdentity,
)
from app.auth_http import (
    AUTH_SESSION_COOKIE_NAME, AuthenticatedPrincipalDependency,
    AuthSessionStoreDependency, _cookie_credential, _failure_kind, _http_failure,
)
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.auth_settings import AuthSettings, load_auth_settings
from app.database import get_database_session_factory
from app.oidc_failure import OIDCFailure, OIDCFailureKind
from app.oidc_login import (
    ConsumedLoginTransaction, IssuedLoginTransaction,
    OIDC_LOGIN_TRANSACTION_LIFETIME, PostgreSQLOIDCLoginTransactionStore,
)
from app.oidc_verifier import AuthlibOIDCVerifier

router = APIRouter(prefix="/api/auth")
OIDC_STATE_COOKIE_NAME = "rehearse_oidc_state"
OIDC_STATE_COOKIE_PATH = "/api/auth/callback"
OIDC_STATE_COOKIE_MAX_AGE = int(OIDC_LOGIN_TRANSACTION_LIFETIME.total_seconds())
_NO_STORE = {"Cache-Control": "no-store"}
_LOGIN_FAILURE = "Unable to complete login."
_UNAVAILABLE = "Authentication is temporarily unavailable."


class OIDCLoginClient(OIDCVerifier, Protocol):
    async def authorization_url(self, *, transaction: IssuedLoginTransaction) -> str:
        ...


def get_auth_settings() -> AuthSettings:
    try:
        return load_auth_settings()
    except Exception:
        pass
    raise _http_failure(AuthenticationFailureKind.UNAVAILABLE) from None


SettingsDependency = Annotated[AuthSettings, Depends(get_auth_settings)]


def get_oidc_login_client(settings: SettingsDependency) -> OIDCLoginClient:
    try:
        return AuthlibOIDCVerifier(settings.oidc)
    except Exception:
        pass
    raise _http_failure(AuthenticationFailureKind.UNAVAILABLE) from None


def get_login_transaction_store() -> PostgreSQLOIDCLoginTransactionStore:
    try:
        return PostgreSQLOIDCLoginTransactionStore(get_database_session_factory())
    except Exception:
        pass
    raise _http_failure(AuthenticationFailureKind.UNAVAILABLE) from None


def get_login_auth_session_store(settings: SettingsDependency) -> PostgreSQLAuthSessionStore:
    try:
        return PostgreSQLAuthSessionStore(
            get_database_session_factory(), session_lifetime=settings.session_lifetime,
        )
    except Exception:
        pass
    raise _http_failure(AuthenticationFailureKind.UNAVAILABLE) from None


LoginClientDependency = Annotated[OIDCLoginClient, Depends(get_oidc_login_client)]
TransactionStoreDependency = Annotated[
    PostgreSQLOIDCLoginTransactionStore, Depends(get_login_transaction_store),
]
IssuanceStoreDependency = Annotated[
    PostgreSQLAuthSessionStore, Depends(get_login_auth_session_store),
]


def _text(value: object) -> bool:
    if type(value) is not str or not value.strip() or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _cookie_value(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None


def _failure(status: int = 400, *, clear_state: bool = False) -> JSONResponse:
    response = JSONResponse(
        {"detail": _UNAVAILABLE if status == 503 else _LOGIN_FAILURE},
        status_code=status, headers=_NO_STORE,
    )
    if clear_state:
        _clear_state_cookie(response)
    return response


def _clear_state_cookie(response: Response) -> None:
    response.delete_cookie(
        OIDC_STATE_COOKIE_NAME, path=OIDC_STATE_COOKIE_PATH,
        secure=True, httponly=True, samesite="lax",
    )


def _oidc_kind(error: OIDCFailure) -> OIDCFailureKind:
    kind = getattr(error, "kind", None) if type(error) is OIDCFailure else None
    return kind if type(kind) is OIDCFailureKind else OIDCFailureKind.UNAVAILABLE


@router.get("/login")
async def login(
    request: Request, settings: SettingsDependency,
    transactions: TransactionStoreDependency, oidc: LoginClientDependency,
) -> Response:
    # V1 has no caller-selectable configuration or post-login destination.
    if request.query_params:
        return _failure()
    try:
        issued = await run_in_threadpool(transactions.create_login_transaction)
        if type(issued) is not IssuedLoginTransaction or not _cookie_value(issued.state):
            return _failure(503)
        location = await oidc.authorization_url(transaction=issued)
        if not _text(location) or any(character.isspace() for character in location):
            return _failure(503)
        endpoint = urlsplit(location)
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
                or endpoint.password is not None or endpoint.fragment):
            return _failure(503)
        _ = endpoint.port
        response = RedirectResponse(location, status_code=302, headers=_NO_STORE)
        response.set_cookie(
            OIDC_STATE_COOKIE_NAME, issued.state, max_age=OIDC_STATE_COOKIE_MAX_AGE,
            path=OIDC_STATE_COOKIE_PATH, secure=True, httponly=True, samesite="lax",
        )
        return response
    except Exception:
        pass
    return _failure(503)


@router.get("/callback")
async def callback(
    request: Request, settings: SettingsDependency,
    transactions: TransactionStoreDependency, oidc: LoginClientDependency,
    store: IssuanceStoreDependency,
) -> Response:
    # Manual parsing avoids validation responses echoing sensitive query values.
    states = request.query_params.getlist("state")
    browser_state = request.cookies.get(OIDC_STATE_COOKIE_NAME)
    if (len(states) != 1 or not _text(states[0]) or not _text(browser_state)
            or not secrets.compare_digest(states[0].encode("utf-8"), browser_state.encode("utf-8"))):
        return _failure()
    state = states[0]
    consumed = False
    phase = "consume"
    status = 503
    try:
        proof = await run_in_threadpool(transactions.consume_login_transaction, state)
        consumed = True
        if type(proof) is not ConsumedLoginTransaction:
            return _failure(503, clear_state=True)
        codes = request.query_params.getlist("code")
        # Provider-declared rejection burns the matching state, without exchange.
        if "error" in request.query_params or len(codes) != 1 or not _text(codes[0]):
            return _failure(clear_state=True)
        phase = "verify"
        identity = await oidc.verify_callback(
            code=codes[0], state=state, expected_state=state,
            expected_nonce=proof.nonce, code_verifier=proof.code_verifier,
        )
        if (type(identity) is not VerifiedExternalIdentity or not _text(identity.subject)
                or identity.issuer != settings.oidc.issuer):
            return _failure(clear_state=True)
        phase = "persist"
        user_id = await run_in_threadpool(store.provision_user, identity=identity)
        if type(user_id) is not UUID:
            return _failure(503, clear_state=True)
        issued = await run_in_threadpool(store.create, user_id=user_id)
        if (type(issued) is not IssuedAuthSession or issued.principal.user_id != user_id
                or not _cookie_value(issued.credential)):
            return _failure(503, clear_state=True)
        response = RedirectResponse(settings.app_origin, status_code=302, headers=_NO_STORE)
        response.set_cookie(
            AUTH_SESSION_COOKIE_NAME, issued.credential, max_age=settings.session_ttl_seconds,
            path="/", secure=True, httponly=True, samesite="lax",
        )
        _clear_state_cookie(response)
        return response
    except OIDCFailure as error:
        kind = _oidc_kind(error)
        if phase == "consume" and kind is OIDCFailureKind.INVALID_STATE:
            # Absent/replayed/expired matching state has no legitimate cookie use.
            consumed = True
        if phase != "persist" and kind is not OIDCFailureKind.UNAVAILABLE:
            status = 400
    except Exception:
        pass
    return _failure(status, clear_state=consumed)


def get_bootstrap_principal(request: Request, store: AuthSessionStoreDependency) -> AuthenticatedPrincipal:
    credential = _cookie_credential(request)
    failure = None
    try:
        principal = store.resolve(credential=credential)
        if type(principal) is not AuthenticatedPrincipal or not _text(principal.request_context):
            failure = AuthenticationFailureKind.UNAVAILABLE
    except AuthenticationFailure as error:
        failure = _failure_kind(error)
        if failure is AuthenticationFailureKind.INVALID_REQUEST_CONTEXT:
            failure = AuthenticationFailureKind.UNAUTHENTICATED
    except Exception:
        failure = AuthenticationFailureKind.UNAVAILABLE
    if failure is not None:
        raise _http_failure(failure) from None
    return principal


@router.get("/me")
def me(principal: Annotated[AuthenticatedPrincipal, Depends(get_bootstrap_principal)]) -> Response:
    return JSONResponse(
        {"user_id": str(principal.user_id), "request_context": principal.request_context}, headers=_NO_STORE,
    )


@router.post("/logout", status_code=204)
def logout(principal: AuthenticatedPrincipalDependency, store: AuthSessionStoreDependency) -> Response:
    try:
        store.revoke(auth_session_id=principal.auth_session_id)
    except Exception:
        pass
    else:
        response = Response(status_code=204, headers=_NO_STORE)
        response.delete_cookie(
            AUTH_SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax",
        )
        return response
    raise _http_failure(AuthenticationFailureKind.UNAVAILABLE) from None
