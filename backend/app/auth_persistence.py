"""Local authentication persistence; no HTTP authentication or provider work.

Only trusted composition may supply an already-verified external identity or an
internal user ID. This store does not verify external claims, authorize interview
access, issue cookies or configure its own database. Each operation owns a short
transaction; no ORM session or lock spans future identity-provider work.

The fixed digest scheme is SHA-256 over the UTF-8 bytes of a high-entropy opaque
credential, stored as 32 bytes. It is not a password-hashing scheme. The caller
supplies the lifetime policy explicitly; this slice does not choose that policy.
"""

import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import TypeVar
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.auth import (
    AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind,
    AuthSessionStore, IssuedAuthSession, VerifiedExternalIdentity,
)
from app.database_models import AuthSession, User

AUTH_SESSION_TOKEN_HASH_SCHEME = "sha256-v1"
AUTH_SESSION_TOKEN_ENTROPY_BYTES = 32

T = TypeVar("T")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_opaque_value() -> str:
    return secrets.token_urlsafe(AUTH_SESSION_TOKEN_ENTROPY_BYTES)


def _valid_text(value: object) -> bool:
    # Opaque values remain exact; never coerce or invoke a str subclass's methods.
    if type(value) is not str or not value.strip() or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _guarded(operation: Callable[[], T]) -> T:
    """Discard private error chains, retaining only the approved failure kind."""
    try:
        return operation()
    except AuthenticationFailure as error:
        kind = error.kind
    except Exception:
        kind = AuthenticationFailureKind.UNAVAILABLE
    # Outside the handler: no database exception, parameters or chain escapes.
    raise AuthenticationFailure(kind) from None


class PostgreSQLAuthSessionStore(AuthSessionStore):
    """Digest-only sessions with exact identity lookup and explicit dependencies.

    Generators are called independently, without identity/token arguments. Trusted
    test injection may make them deterministic; production defaults use separate
    secure random draws. Normal object repr contains none of their values.

    Ordinary revalidation is a point-in-time liveness check. Adaptive Continue
    additionally guards its own short persistence transaction with the existing
    login row. Revocation of an absent session is an idempotent success.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        session_lifetime: timedelta,
        clock: Callable[[], datetime] = _utc_now,
        credential_generator: Callable[[], str] = _generate_opaque_value,
        request_context_generator: Callable[[], str] = _generate_opaque_value,
    ) -> None:
        if type(session_lifetime) is not timedelta or session_lifetime <= timedelta(0):
            raise ValueError("Authentication session lifetime must be a positive timedelta.")
        if not all(callable(value) for value in (clock, credential_generator, request_context_generator)):
            raise TypeError("Authentication clock and generators must be callable.")
        self._session_factory = session_factory
        self._session_lifetime = session_lifetime
        self._clock = clock
        self._credential_generator = credential_generator
        self._request_context_generator = request_context_generator

    def _now(self) -> datetime:
        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE)
        return now.astimezone(timezone.utc)

    def provision_user(self, *, identity: VerifiedExternalIdentity) -> UUID:
        """Provision exactly one user, with database uniqueness resolving races.

        ON CONFLICT waits for a competing insert without retrying the operation.
        The subsequent SELECT uses the factory's normal PostgreSQL READ COMMITTED
        isolation to see the committed winner. Other database failures fail closed.
        """
        def operation() -> UUID:
            if (type(identity) is not VerifiedExternalIdentity
                    or not _valid_text(identity.issuer) or not _valid_text(identity.subject)):
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            with self._session_factory.begin() as database:
                user_id = database.scalar(
                    insert(User).values(
                        auth_provider=identity.issuer, provider_subject=identity.subject,
                    ).on_conflict_do_nothing(constraint="uq_users_auth_identity").returning(User.id)
                )
                if user_id is None:
                    user_id = database.scalar(select(User.id).where(
                        User.auth_provider == identity.issuer,
                        User.provider_subject == identity.subject,
                    ))
                if user_id is None:
                    raise AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE)
            return user_id

        return _guarded(operation)

    def create(self, *, user_id: UUID) -> IssuedAuthSession:
        """Issue once; return the raw credential only after a successful commit."""
        def operation() -> IssuedAuthSession:
            if type(user_id) is not UUID:
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            created_at = self._now()
            credential = self._credential_generator()
            request_context = self._request_context_generator()
            if not _valid_text(credential) or not _valid_text(request_context):
                raise AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE)
            digest = sha256(credential.encode("utf-8")).digest()
            with self._session_factory.begin() as database:
                if database.scalar(select(User.id).where(User.id == user_id)) is None:
                    raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
                record = AuthSession(
                    user_id=user_id, token_hash=digest, request_context=request_context,
                    created_at=created_at, expires_at=created_at + self._session_lifetime,
                )
                database.add(record)
                database.flush()
                principal = AuthenticatedPrincipal(
                    user_id=user_id, auth_session_id=record.id, request_context=request_context,
                )
            return IssuedAuthSession(principal=principal, credential=credential)

        return _guarded(operation)

    def resolve(self, *, credential: str) -> AuthenticatedPrincipal:
        """Missing, invalid, expired and revoked credentials share one failure."""
        def operation() -> AuthenticatedPrincipal:
            if not _valid_text(credential):
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            digest = sha256(credential.encode("utf-8")).digest()
            with self._session_factory.begin() as database:
                row = database.execute(select(
                    AuthSession.id, AuthSession.user_id, AuthSession.request_context, AuthSession.expires_at,
                ).join(User, User.id == AuthSession.user_id).where(AuthSession.token_hash == digest)).one_or_none()
                if row is None or self._now() >= row.expires_at:
                    raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
                principal = AuthenticatedPrincipal(
                    user_id=row.user_id, auth_session_id=row.id, request_context=row.request_context,
                )
            return principal

        return _guarded(operation)

    def revalidate(self, *, principal: AuthenticatedPrincipal) -> None:
        """Check exact live session/user/context without rotation or refresh."""
        def operation() -> None:
            if type(principal) is not AuthenticatedPrincipal:
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            with self._session_factory.begin() as database:
                row = database.execute(select(
                    AuthSession.request_context, AuthSession.expires_at,
                ).join(User, User.id == AuthSession.user_id).where(
                    AuthSession.id == principal.auth_session_id,
                    AuthSession.user_id == principal.user_id,
                )).one_or_none()
                if row is None or self._now() >= row.expires_at:
                    raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
                if row.request_context != principal.request_context:
                    raise AuthenticationFailure(AuthenticationFailureKind.INVALID_REQUEST_CONTEXT)

        _guarded(operation)

    def revalidate_in_transaction(
        self, *, database: Session, principal: AuthenticatedPrincipal,
    ) -> None:
        """Guard an adaptive write using its transaction, without opening another.

        The caller first locks the owned interview root. A shared lock on the
        exact local login then serializes this commit with revocation; concurrent
        writes for different interviews can share that lock. Expiry is evaluated
        after both lock waits. No credential or identity-provider data is needed.
        """
        def operation() -> None:
            if type(principal) is not AuthenticatedPrincipal:
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            if not isinstance(database, Session) or not database.in_transaction():
                raise AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE)
            row = database.execute(select(
                AuthSession.request_context, AuthSession.expires_at,
            ).join(User, User.id == AuthSession.user_id).where(
                AuthSession.id == principal.auth_session_id,
                AuthSession.user_id == principal.user_id,
            ).with_for_update(read=True, of=AuthSession)).one_or_none()
            if row is None or self._now() >= row.expires_at:
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            if not secrets.compare_digest(
                row.request_context.encode("utf-8"), principal.request_context.encode("utf-8"),
            ):
                raise AuthenticationFailure(AuthenticationFailureKind.INVALID_REQUEST_CONTEXT)

        _guarded(operation)

    def revoke(self, *, auth_session_id: UUID) -> None:
        """Delete only the named local session; absence is an idempotent success."""
        def operation() -> None:
            if type(auth_session_id) is not UUID:
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            with self._session_factory.begin() as database:
                database.execute(delete(AuthSession).where(AuthSession.id == auth_session_id))

        _guarded(operation)
