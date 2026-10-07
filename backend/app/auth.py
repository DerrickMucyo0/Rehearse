"""Provider-neutral authentication contracts, below HTTP and persistence.

Only a verifier may attest to an external identity, and only trusted backend
composition may establish a principal from a live local authentication session.
Constructing these values checks their shape; it does not authenticate supplied
claims. Browser user IDs, email, interview UUIDs and unverified claims confer no
authority. Protected services will require an AuthenticatedPrincipal explicitly
and perform owner-filtered operations; this module implements neither step.

There is no configuration, cryptographic generation, I/O or storage here.
"""

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol
from uuid import UUID


def _require_text(value: str, label: str) -> None:
    # Never coerce or format an untrusted value, including a str subclass.
    if type(value) is not str:
        raise TypeError(f"{label} must be a string.")
    if not value.strip():
        raise ValueError(f"{label} must not be blank.")


@dataclass(frozen=True, slots=True)
class VerifiedExternalIdentity:
    """Exact issuer/subject attested by the OIDC verifier, without normalization.

    The verifier must match issuer to its trusted configured issuer exactly.
    Subject is opaque and case-sensitive; email/profile data cannot link users.
    """

    issuer: str
    subject: str

    def __post_init__(self) -> None:
        _require_text(self.issuer, "Identity issuer")
        _require_text(self.subject, "Identity subject")


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Server-established identity for a single live local login session.

    auth_session_id identifies the internal authentication session, never an
    interview session or a raw browser credential. request_context must be
    generated unpredictably by the future store and bound to that login session.
    It is not a user/owner ID and cannot authenticate independently. After cookie
    authentication, the HTTP boundary must check its request-context binding to
    protect CSRF, login generation and stale-tab account switching.

    Structural validation cannot prove entropy, identity verification or session
    liveness. Those guarantees belong to the injected verifier/store boundaries.
    """

    user_id: UUID
    auth_session_id: UUID
    request_context: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.user_id) is not UUID:
            raise TypeError("Principal user ID must be a UUID.")
        if type(self.auth_session_id) is not UUID:
            raise TypeError("Principal authentication session ID must be a UUID.")
        _require_text(self.request_context, "Principal request context")


@dataclass(frozen=True, slots=True)
class IssuedAuthSession:
    """Store-to-HTTP result; only principal may reach protected services.

    credential is the opaque browser credential, not a provider token. The future
    HTTP boundary delivers it securely; it must never be logged or used directly
    as a service principal. Omitting it from repr is not a serialization policy:
    callers must not serialize this internal object generically.
    """

    principal: AuthenticatedPrincipal
    credential: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.principal) is not AuthenticatedPrincipal:
            raise TypeError("Issued authentication session must contain an authenticated principal.")
        _require_text(self.credential, "Authentication credential")


class AuthenticationFailureKind(str, Enum):
    UNAUTHENTICATED = "unauthenticated"
    INVALID_REQUEST_CONTEXT = "invalid_request_context"
    UNAVAILABLE = "unavailable"


_FAILURE_MESSAGES = {
    AuthenticationFailureKind.UNAUTHENTICATED: "Authentication required.",
    AuthenticationFailureKind.INVALID_REQUEST_CONTEXT: "Invalid authentication request context.",
    AuthenticationFailureKind.UNAVAILABLE: "Authentication is temporarily unavailable.",
}


class AuthenticationFailure(Exception):
    """Closed failure metadata with fixed messages and no arbitrary details.

    Future adapters must report only the closed kind, without copying bodies,
    credentials or exception text into metadata. Raise outside the external
    exception handler and from None; do not attach or log external error chains.
    Resource ownership failures belong to owned resource lookup, not this type.
    """

    def __init__(self, kind: AuthenticationFailureKind) -> None:
        if type(kind) is not AuthenticationFailureKind:
            raise TypeError("Authentication failure kind must be an allowlisted value.")
        self._kind = kind
        super().__init__(_FAILURE_MESSAGES[kind])

    @property
    def kind(self) -> AuthenticationFailureKind:
        return self._kind


class OIDCVerifier(Protocol):
    """Injectable async verification boundary; no verification is implemented.

    code/state are untrusted callback inputs. expected_state, expected_nonce and
    code_verifier come from a trusted, pending server login transaction. The
    adapter owns trusted issuer, audience/client ID, redirect and key/algorithm
    configuration. It must compare state before code exchange, then complete
    exchange with PKCE and verify nonce, issuer, audience, signature,
    algorithm, expiry and subject. Login proof consumption/replay prevention
    also belongs to that implementation. Decoding claims alone is insufficient.
    """

    @abstractmethod
    async def verify_callback(
        self,
        *,
        code: str,
        state: str,
        expected_state: str,
        expected_nonce: str,
        code_verifier: str,
    ) -> VerifiedExternalIdentity:
        ...


class AuthSessionStore(Protocol):
    """Injectable synchronous local-session boundary; no database is implemented.

    create accepts an already-resolved internal user, not a frontend claim.
    resolve requires an opaque credential; request_context alone is never enough.
    Missing/invalid/expired/revoked sessions share UNAUTHENTICATED. Binding
    failures use INVALID_REQUEST_CONTEXT; infrastructure failures use UNAVAILABLE.

    revalidate must check the supplied principal's exact live session/user/context
    without changing identity. Later transaction-aware implementations coordinate
    revalidation with revocation for protected writes and recheck after inference,
    holding no database locks across provider work. Services receive principals,
    never raw credentials. None is not a principal or a successful resolution.
    """

    @abstractmethod
    def create(self, *, user_id: UUID) -> IssuedAuthSession:
        ...

    @abstractmethod
    def resolve(self, *, credential: str) -> AuthenticatedPrincipal:
        ...

    @abstractmethod
    def revoke(self, *, auth_session_id: UUID) -> None:
        ...

    @abstractmethod
    def revalidate(self, *, principal: AuthenticatedPrincipal) -> None:
        ...
