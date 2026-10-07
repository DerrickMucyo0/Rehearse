"""One-use OIDC login transactions before any HTTP login or local issuance.

State is stored only as SHA-256 over its exact UTF-8 bytes. Nonce and PKCE
verifier remain server-side until atomic consumption. Each database operation
owns a short transaction; callback verification begins after consumption closes.
No user provisioning, local authentication sessions, cookies or routes exist here.
"""

import asyncio
import base64
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from typing import Literal, TypeVar

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.auth import OIDCVerifier, VerifiedExternalIdentity
from app.database_models import OIDCLoginTransaction
from app.oidc_failure import OIDCFailure, OIDCFailureKind

OIDC_STATE_HASH_SCHEME = "sha256-v1"
OIDC_LOGIN_ENTROPY_BYTES = 32
OIDC_LOGIN_TRANSACTION_LIFETIME = timedelta(minutes=10)

T = TypeVar("T")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z", re.ASCII)
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_opaque_value() -> str:
    return secrets.token_urlsafe(OIDC_LOGIN_ENTROPY_BYTES)


def _valid_text(value: object) -> bool:
    if type(value) is not str or not value.strip() or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_verifier(value: object) -> bool:
    return type(value) is str and _VERIFIER.fullmatch(value) is not None


def _failure_kind(error: OIDCFailure) -> OIDCFailureKind:
    if type(error) is OIDCFailure:
        kind = getattr(error, "kind", None)
        if type(kind) is OIDCFailureKind:
            return kind
    return OIDCFailureKind.UNAVAILABLE


def _guarded(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except OIDCFailure as error:
        kind = _failure_kind(error)
    except Exception:
        kind = OIDCFailureKind.UNAVAILABLE
    # Raise after the handler so SQL parameters and arbitrary secret chains die.
    raise OIDCFailure(kind) from None


@dataclass(frozen=True, slots=True)
class IssuedLoginTransaction:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_challenge: str
    code_challenge_method: Literal["S256"] = "S256"

    def __post_init__(self) -> None:
        if not _valid_text(self.state) or not _valid_text(self.nonce):
            raise ValueError("Login state and nonce must be nonblank text.")
        if type(self.code_challenge) is not str or _CHALLENGE.fullmatch(self.code_challenge) is None:
            raise ValueError("Login code challenge must be a SHA-256 base64url value.")
        if type(self.code_challenge_method) is not str or self.code_challenge_method != "S256":
            raise ValueError("Login code challenge method must be S256.")


@dataclass(frozen=True, slots=True)
class ConsumedLoginTransaction:
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _valid_text(self.nonce) or not _valid_verifier(self.code_verifier):
            raise ValueError("Consumed login values must contain a valid nonce and PKCE verifier.")


class PostgreSQLOIDCLoginTransactionStore:
    """Independent secure draws and PostgreSQL atomic DELETE RETURNING.

    A digest collision fails without regenerating state. Expired matching rows
    are deleted and committed before the same failure used for absent/replayed
    state. Infrastructure errors roll back and expose only UNAVAILABLE.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        transaction_lifetime: timedelta = OIDC_LOGIN_TRANSACTION_LIFETIME,
        clock: Callable[[], datetime] = _utc_now,
        state_generator: Callable[[], str] = _generate_opaque_value,
        nonce_generator: Callable[[], str] = _generate_opaque_value,
        verifier_generator: Callable[[], str] = _generate_opaque_value,
    ) -> None:
        if type(transaction_lifetime) is not timedelta or transaction_lifetime <= timedelta(0):
            raise ValueError("Login transaction lifetime must be a positive timedelta.")
        if not all(callable(value) for value in (clock, state_generator, nonce_generator, verifier_generator)):
            raise TypeError("Login clock and generators must be callable.")
        self._session_factory = session_factory
        self._transaction_lifetime = transaction_lifetime
        self._clock = clock
        self._state_generator = state_generator
        self._nonce_generator = nonce_generator
        self._verifier_generator = verifier_generator

    def _now(self) -> datetime:
        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise OIDCFailure(OIDCFailureKind.UNAVAILABLE)
        return now.astimezone(timezone.utc)

    def create_login_transaction(self) -> IssuedLoginTransaction:
        def operation() -> IssuedLoginTransaction:
            created_at = self._now()
            state = self._state_generator()
            nonce = self._nonce_generator()
            verifier = self._verifier_generator()
            if (not _valid_text(state) or not _valid_text(nonce) or not _valid_verifier(verifier)
                    or len({state, nonce, verifier}) != 3):
                raise OIDCFailure(OIDCFailureKind.UNAVAILABLE)
            issued = IssuedLoginTransaction(
                state=state, nonce=nonce,
                code_challenge=base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("="),
            )
            with self._session_factory.begin() as database:
                database.add(OIDCLoginTransaction(
                    state_hash=sha256(state.encode("utf-8")).digest(), nonce=nonce, code_verifier=verifier,
                    created_at=created_at, expires_at=created_at + self._transaction_lifetime,
                ))
                database.flush()
            return issued

        return _guarded(operation)

    def consume_login_transaction(self, state: str) -> ConsumedLoginTransaction:
        def operation() -> ConsumedLoginTransaction:
            if not _valid_text(state):
                raise OIDCFailure(OIDCFailureKind.INVALID_STATE)
            digest = sha256(state.encode("utf-8")).digest()
            with self._session_factory.begin() as database:
                row = database.execute(delete(OIDCLoginTransaction).where(
                    OIDCLoginTransaction.state_hash == digest,
                ).returning(
                    OIDCLoginTransaction.nonce, OIDCLoginTransaction.code_verifier,
                    OIDCLoginTransaction.expires_at,
                )).one_or_none()
                invalid = row is None or self._now() >= row.expires_at
                consumed = None if invalid else ConsumedLoginTransaction(
                    nonce=row.nonce, code_verifier=row.code_verifier,
                )
            # A committed expired delete cannot become a reusable login state.
            if invalid:
                raise OIDCFailure(OIDCFailureKind.INVALID_STATE)
            return consumed

        return _guarded(operation)


async def verify_login_callback(
    *, transactions: PostgreSQLOIDCLoginTransactionStore, verifier: OIDCVerifier,
    code: str, state: str,
) -> VerifiedExternalIdentity:
    """Consume once in a worker, then verify without an open database scope.

    expected_state is trusted only after digest-backed atomic consumption. The
    exact state is then supplied to the stateless existing verifier contract;
    the consumed nonce and verifier never enter any browser response here.
    """
    try:
        consumed = await asyncio.to_thread(transactions.consume_login_transaction, state)
        identity = await verifier.verify_callback(
            code=code, state=state, expected_state=state,
            expected_nonce=consumed.nonce, code_verifier=consumed.code_verifier,
        )
        if (type(identity) is not VerifiedExternalIdentity or
                not _valid_text(identity.issuer) or not _valid_text(identity.subject)):
            raise OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
        return identity
    except OIDCFailure as error:
        kind = _failure_kind(error)
    except Exception:
        kind = OIDCFailureKind.UNAVAILABLE
    raise OIDCFailure(kind) from None
