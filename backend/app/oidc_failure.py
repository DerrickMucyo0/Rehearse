"""Closed internal OIDC failures without provider details or secret values."""

from enum import Enum


class OIDCFailureKind(str, Enum):
    INVALID_STATE = "invalid_state"
    EXCHANGE_FAILED = "exchange_failed"
    INVALID_TOKEN = "invalid_token"
    UNAVAILABLE = "unavailable"


_MESSAGES = {
    OIDCFailureKind.INVALID_STATE: "Invalid login state.",
    OIDCFailureKind.EXCHANGE_FAILED: "Unable to exchange authorization code.",
    OIDCFailureKind.INVALID_TOKEN: "Invalid identity token.",
    OIDCFailureKind.UNAVAILABLE: "OIDC login is temporarily unavailable.",
}


class OIDCFailure(RuntimeError):
    __slots__ = ("_kind",)

    def __init__(self, kind: OIDCFailureKind) -> None:
        if type(kind) is not OIDCFailureKind:
            raise TypeError("OIDC failure kind must be an approved failure kind.")
        self._kind = kind
        super().__init__(_MESSAGES[kind])

    @property
    def kind(self) -> OIDCFailureKind:
        return self._kind
