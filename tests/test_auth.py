"""Offline contracts for authentication values and injected boundaries.

The local fakes demonstrate injection and failure semantics. They do not perform
OIDC verification, generate secure credentials, or implement session storage.
"""

import ast
from dataclasses import FrozenInstanceError, MISSING, fields, is_dataclass
from enum import Enum
import inspect
from pathlib import Path
import subprocess
import sys
import textwrap
import traceback
from typing import get_type_hints
from uuid import UUID

import pytest

from app import auth
from app.auth import (
    AuthenticatedPrincipal,
    AuthenticationFailure,
    AuthenticationFailureKind,
    AuthSessionStore,
    IssuedAuthSession,
    OIDCVerifier,
    VerifiedExternalIdentity,
)


USER_ID = UUID("00000000-0000-4000-8000-000000000011")
AUTH_SESSION_ID = UUID("00000000-0000-4000-8000-000000000012")
OTHER_ID = UUID("00000000-0000-4000-8000-000000000013")
REQUEST_CONTEXT = "PRIVATE_REQUEST_CONTEXT_SENTINEL"
CREDENTIAL = "PRIVATE_CREDENTIAL_SENTINEL"
PRIVATE_MARKER = "PRIVATE_UNTRUSTED_VALUE_SENTINEL"


def principal():
    return AuthenticatedPrincipal(
        user_id=USER_ID,
        auth_session_id=AUTH_SESSION_ID,
        request_context=REQUEST_CONTEXT,
    )


RECORD_CASES = (
    (
        VerifiedExternalIdentity,
        {"issuer": "https://identity.example.test", "subject": "opaque-subject"},
        {"issuer": str, "subject": str},
    ),
    (
        AuthenticatedPrincipal,
        {"user_id": USER_ID, "auth_session_id": AUTH_SESSION_ID, "request_context": REQUEST_CONTEXT},
        {"user_id": UUID, "auth_session_id": UUID, "request_context": str},
    ),
    (
        IssuedAuthSession,
        {"principal": principal(), "credential": CREDENTIAL},
        {"principal": AuthenticatedPrincipal, "credential": str},
    ),
)
RECORD_IDS = ("external-identity", "authenticated-principal", "issued-session")
TEXT_CASES = (
    (VerifiedExternalIdentity, RECORD_CASES[0][1], "issuer", "Identity issuer"),
    (VerifiedExternalIdentity, RECORD_CASES[0][1], "subject", "Identity subject"),
    (AuthenticatedPrincipal, RECORD_CASES[1][1], "request_context", "Principal request context"),
    (IssuedAuthSession, RECORD_CASES[2][1], "credential", "Authentication credential"),
)
TEXT_IDS = ("issuer", "subject", "request-context", "credential")
UUID_CASES = (
    ("user_id", "Principal user ID must be a UUID."),
    ("auth_session_id", "Principal authentication session ID must be a UUID."),
)


class PrivateValue:
    """Detect accidental formatting of hostile input during validation."""

    def __init__(self):
        self.render_calls = 0

    def __str__(self):
        self.render_calls += 1
        return PRIVATE_MARKER

    def __repr__(self):
        self.render_calls += 1
        return PRIVATE_MARKER


class UntrustedString(str):
    def __str__(self):
        raise AssertionError("String subclasses must not be coerced.")

    def __repr__(self):
        raise AssertionError("String subclasses must not be formatted.")

    def strip(self, *args, **kwargs):
        raise AssertionError("String subclasses must be rejected before validation.")


class UntrustedUUID(UUID):
    def __str__(self):
        raise AssertionError("UUID subclasses must not be coerced.")

    def __repr__(self):
        raise AssertionError("UUID subclasses must not be formatted.")


def assert_safe_error(error, message):
    assert str(error) == message
    assert error.args == (message,)
    assert PRIVATE_MARKER not in repr(error)
    assert PRIVATE_MARKER not in repr(error.args)


@pytest.mark.parametrize("record_type, values, annotations", RECORD_CASES, ids=RECORD_IDS)
def test_records_have_only_the_required_identity_and_session_fields(record_type, values, annotations):
    record = record_type(**values)
    assert is_dataclass(record)
    assert record_type.__dataclass_params__.frozen is True
    assert tuple(field.name for field in fields(record)) == tuple(annotations)
    assert record_type.__slots__ == tuple(annotations)
    assert get_type_hints(record_type) == annotations
    assert not hasattr(record, "__dict__")
    for name in ("email", "profile", "provider_token", "access_token", "interview_session_id"):
        assert not hasattr(record, name)


@pytest.mark.parametrize("record_type, values, annotations", RECORD_CASES, ids=RECORD_IDS)
def test_records_cannot_be_changed_or_extended(record_type, values, annotations):
    record = record_type(**values)
    field_name = next(iter(annotations))
    with pytest.raises(FrozenInstanceError):
        setattr(record, field_name, PRIVATE_MARKER)
    with pytest.raises(FrozenInstanceError):
        delattr(record, field_name)
    with pytest.raises((AttributeError, TypeError)):
        setattr(record, "email", PRIVATE_MARKER)
    assert getattr(record, field_name) == values[field_name]


@pytest.mark.parametrize("record_type, values, annotations", RECORD_CASES, ids=RECORD_IDS)
def test_every_record_field_is_required(record_type, values, annotations):
    for field in fields(record_type):
        assert field.default is MISSING
        assert field.default_factory is MISSING
        incomplete = dict(values)
        del incomplete[field.name]
        with pytest.raises(TypeError):
            record_type(**incomplete)


@pytest.mark.parametrize("record_type, values, name, label", TEXT_CASES, ids=TEXT_IDS)
def test_valid_text_is_preserved_without_stripping_or_normalization(record_type, values, name, label):
    supplied = " \tMiXeD / Case?e\u0301 中文 😀\n "
    record = record_type(**{**values, name: supplied})
    assert getattr(record, name) is supplied
    assert getattr(record, name).encode("utf-8") == supplied.encode("utf-8")


@pytest.mark.parametrize("record_type, values, name, label", TEXT_CASES, ids=TEXT_IDS)
def test_text_requires_an_exact_string_without_rendering_wrong_types(record_type, values, name, label):
    hostile = PrivateValue()
    for supplied in (None, True, 7, b"PRIVATE_UNTRUSTED_VALUE_SENTINEL", [], {}, hostile):
        with pytest.raises(TypeError) as caught:
            record_type(**{**values, name: supplied})
        assert_safe_error(caught.value, f"{label} must be a string.")
    assert hostile.render_calls == 0


@pytest.mark.parametrize("record_type, values, name, label", TEXT_CASES, ids=TEXT_IDS)
def test_string_subclasses_are_rejected_before_their_methods_run(record_type, values, name, label):
    with pytest.raises(TypeError) as caught:
        record_type(**{**values, name: UntrustedString(PRIVATE_MARKER)})
    assert_safe_error(caught.value, f"{label} must be a string.")


@pytest.mark.parametrize("record_type, values, name, label", TEXT_CASES, ids=TEXT_IDS)
def test_empty_and_whitespace_only_text_is_rejected_with_fixed_messages(record_type, values, name, label):
    for supplied in ("", " ", "\t\r\n", "\u2003"):
        with pytest.raises(ValueError) as caught:
            record_type(**{**values, name: supplied})
        assert_safe_error(caught.value, f"{label} must not be blank.")


@pytest.mark.parametrize("name, message", UUID_CASES, ids=("user-id", "auth-session-id"))
def test_principal_ids_require_uuid_objects_without_coercion(name, message):
    hostile = PrivateValue()
    for supplied in (None, True, 7, str(USER_ID), USER_ID.bytes, hostile):
        with pytest.raises(TypeError) as caught:
            AuthenticatedPrincipal(**{**RECORD_CASES[1][1], name: supplied})
        assert_safe_error(caught.value, message)
    assert hostile.render_calls == 0


@pytest.mark.parametrize("name, message", UUID_CASES, ids=("user-id", "auth-session-id"))
def test_principal_ids_reject_uuid_subclasses(name, message):
    supplied = UntrustedUUID(int=USER_ID.int)
    with pytest.raises(TypeError) as caught:
        AuthenticatedPrincipal(**{**RECORD_CASES[1][1], name: supplied})
    assert_safe_error(caught.value, message)


def test_issued_session_requires_a_principal_without_rendering_other_values():
    hostile = PrivateValue()
    for supplied in (None, USER_ID, REQUEST_CONTEXT, RECORD_CASES[0][0](**RECORD_CASES[0][1]), hostile):
        with pytest.raises(TypeError) as caught:
            IssuedAuthSession(principal=supplied, credential=CREDENTIAL)
        assert_safe_error(caught.value, "Issued authentication session must contain an authenticated principal.")
    assert hostile.render_calls == 0


def test_issued_session_rejects_a_principal_subclass():
    class DerivedPrincipal(AuthenticatedPrincipal):
        pass

    supplied = DerivedPrincipal(**RECORD_CASES[1][1])
    with pytest.raises(TypeError) as caught:
        IssuedAuthSession(principal=supplied, credential=CREDENTIAL)
    assert_safe_error(caught.value, "Issued authentication session must contain an authenticated principal.")


def test_principal_and_issued_session_reprs_hide_context_and_credential():
    supplied = principal()
    issued = IssuedAuthSession(principal=supplied, credential=CREDENTIAL)
    assert supplied.request_context == REQUEST_CONTEXT
    assert issued.credential == CREDENTIAL
    assert issued.principal is supplied
    for record in (supplied, issued):
        assert REQUEST_CONTEXT not in repr(record)
        assert CREDENTIAL not in repr(record)
        assert str(USER_ID) in repr(record)
        assert str(AUTH_SESSION_ID) in repr(record)
    assert next(field for field in fields(supplied) if field.name == "request_context").repr is False
    assert next(field for field in fields(issued) if field.name == "credential").repr is False


def test_request_context_is_separate_from_user_and_authentication_session_identity():
    first = principal()
    second = AuthenticatedPrincipal(
        user_id=USER_ID, auth_session_id=AUTH_SESSION_ID, request_context="another-bound-context",
    )
    assert first.user_id == second.user_id == USER_ID
    assert first.auth_session_id == second.auth_session_id == AUTH_SESSION_ID
    assert first.request_context != second.request_context
    assert first != second


FAILURE_CASES = (
    (AuthenticationFailureKind.UNAUTHENTICATED, "Authentication required."),
    (AuthenticationFailureKind.INVALID_REQUEST_CONTEXT, "Invalid authentication request context."),
    (AuthenticationFailureKind.UNAVAILABLE, "Authentication is temporarily unavailable."),
)


@pytest.mark.parametrize("kind, message", FAILURE_CASES, ids=("unauthenticated", "invalid-context", "unavailable"))
def test_authentication_failures_expose_only_fixed_allowlisted_metadata(kind, message):
    error = AuthenticationFailure(kind)
    assert error.kind is kind
    assert_safe_error(error, message)
    for marker in (REQUEST_CONTEXT, CREDENTIAL, "raw callback code", "raw provider response"):
        assert marker not in str(error)
        assert marker not in repr(error)
        assert marker not in repr(error.args)


def test_failure_kind_is_a_closed_string_enum():
    assert issubclass(AuthenticationFailureKind, str)
    assert issubclass(AuthenticationFailureKind, Enum)
    assert {member.name: member.value for member in AuthenticationFailureKind} == {
        "UNAUTHENTICATED": "unauthenticated",
        "INVALID_REQUEST_CONTEXT": "invalid_request_context",
        "UNAVAILABLE": "unavailable",
    }


def test_failure_rejects_strings_other_enums_and_objects_without_rendering_them():
    class OtherKind(str, Enum):
        UNAUTHENTICATED = "unauthenticated"

    hostile = PrivateValue()
    for supplied in (None, True, "unauthenticated", PRIVATE_MARKER, OtherKind.UNAUTHENTICATED, hostile):
        with pytest.raises(TypeError) as caught:
            AuthenticationFailure(supplied)
        assert_safe_error(caught.value, "Authentication failure kind must be an allowlisted value.")
    assert hostile.render_calls == 0


def test_failure_kind_property_cannot_be_assigned_or_deleted():
    error = AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
    with pytest.raises(AttributeError):
        error.kind = AuthenticationFailureKind.UNAVAILABLE
    with pytest.raises(AttributeError):
        del error.kind
    assert error.kind is AuthenticationFailureKind.UNAUTHENTICATED


@pytest.mark.parametrize("name", ("detail", "cause", "message"))
def test_failure_constructor_rejects_arbitrary_detail_keywords(name):
    hostile = PrivateValue()
    with pytest.raises(TypeError) as caught:
        AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED, **{name: hostile})
    assert PRIVATE_MARKER not in str(caught.value)
    assert PRIVATE_MARKER not in repr(caught.value)
    assert PRIVATE_MARKER not in repr(caught.value.args)
    assert hostile.render_calls == 0


def test_failure_constructor_rejects_additional_positional_details():
    hostile = PrivateValue()
    with pytest.raises(TypeError) as caught:
        AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED, hostile)
    assert PRIVATE_MARKER not in str(caught.value)
    assert PRIVATE_MARKER not in repr(caught.value)
    assert PRIVATE_MARKER not in repr(caught.value.args)
    assert hostile.render_calls == 0


PROTOCOL_CASES = (
    (
        OIDCVerifier.verify_callback,
        {"code": str, "state": str, "expected_state": str, "expected_nonce": str, "code_verifier": str},
        VerifiedExternalIdentity,
        True,
    ),
    (AuthSessionStore.create, {"user_id": UUID}, IssuedAuthSession, False),
    (AuthSessionStore.resolve, {"credential": str}, AuthenticatedPrincipal, False),
    (AuthSessionStore.revoke, {"auth_session_id": UUID}, type(None), False),
    (AuthSessionStore.revalidate, {"principal": AuthenticatedPrincipal}, type(None), False),
)


@pytest.mark.parametrize("method, inputs, result, is_async", PROTOCOL_CASES,
                         ids=("verify-callback", "create", "resolve", "revoke", "revalidate"))
def test_protocol_methods_have_required_keyword_only_positive_contracts(method, inputs, result, is_async):
    parameters = inspect.signature(method).parameters
    assert tuple(parameters) == ("self", *inputs)
    assert parameters["self"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in inputs:
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty
    assert get_type_hints(method) == {**inputs, "return": result}
    assert inspect.iscoroutinefunction(method) is is_async
    assert method.__isabstractmethod__ is True


class IncompleteVerifier(OIDCVerifier):
    pass


class IncompleteStore(AuthSessionStore):
    def create(self, *, user_id: UUID) -> IssuedAuthSession:
        return IssuedAuthSession(principal=principal(), credential=CREDENTIAL)


@pytest.mark.parametrize("boundary", (OIDCVerifier, AuthSessionStore, IncompleteVerifier, IncompleteStore),
                         ids=("verifier-protocol", "store-protocol", "incomplete-verifier", "incomplete-store"))
def test_unimplemented_boundaries_cannot_be_instantiated(boundary):
    with pytest.raises(TypeError):
        boundary()


class SyntheticStore(AuthSessionStore):
    """One fixed synthetic login; expiration is explicit and never uses a clock."""

    def __init__(self):
        self.issued = None
        self.expired = False
        self.revoked = False

    def create(self, *, user_id: UUID) -> IssuedAuthSession:
        self.issued = IssuedAuthSession(
            principal=AuthenticatedPrincipal(
                user_id=user_id, auth_session_id=AUTH_SESSION_ID, request_context=REQUEST_CONTEXT,
            ),
            credential=CREDENTIAL,
        )
        self.expired = False
        self.revoked = False
        return self.issued

    def resolve(self, *, credential: str) -> AuthenticatedPrincipal:
        if self.issued is None or credential != self.issued.credential or self.expired or self.revoked:
            raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
        return self.issued.principal

    def revoke(self, *, auth_session_id: UUID) -> None:
        if self.issued is not None and auth_session_id == self.issued.principal.auth_session_id:
            self.revoked = True

    def revalidate(self, *, principal: AuthenticatedPrincipal) -> None:
        if self.issued is None or self.expired or self.revoked or principal != self.issued.principal:
            raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)


def test_injected_store_can_issue_resolve_revalidate_and_revoke_a_principal():
    store: AuthSessionStore = SyntheticStore()
    issued = store.create(user_id=USER_ID)
    assert isinstance(issued, IssuedAuthSession)
    resolved = store.resolve(credential=issued.credential)
    assert resolved is issued.principal
    assert resolved.user_id == USER_ID
    assert resolved.auth_session_id == AUTH_SESSION_ID
    assert store.revalidate(principal=resolved) is None
    assert store.revoke(auth_session_id=resolved.auth_session_id) is None
    with pytest.raises(AuthenticationFailure) as caught:
        store.revalidate(principal=resolved)
    assert caught.value.kind is AuthenticationFailureKind.UNAUTHENTICATED


@pytest.mark.parametrize("scenario", ("missing", "invalid", "expired", "revoked"))
def test_store_fake_uses_the_same_failure_for_missing_invalid_expired_and_revoked_sessions(scenario):
    store = SyntheticStore()
    issued = store.create(user_id=USER_ID)
    credential = issued.credential
    if scenario == "missing":
        store.issued = None
    elif scenario == "invalid":
        credential = PRIVATE_MARKER
    elif scenario == "expired":
        store.expired = True
    else:
        store.revoke(auth_session_id=issued.principal.auth_session_id)
    with pytest.raises(AuthenticationFailure) as caught:
        store.resolve(credential=credential)
    assert caught.value.kind is AuthenticationFailureKind.UNAUTHENTICATED
    assert_safe_error(caught.value, "Authentication required.")
    if scenario == "expired":
        with pytest.raises(AuthenticationFailure) as caught:
            store.revalidate(principal=issued.principal)
        assert caught.value.kind is AuthenticationFailureKind.UNAUTHENTICATED


@pytest.mark.parametrize("changed_field", ("user_id", "auth_session_id", "request_context"))
def test_store_fake_revalidation_checks_the_exact_user_session_and_context(changed_field):
    store = SyntheticStore()
    issued = store.create(user_id=USER_ID)
    replacement = "another-context" if changed_field == "request_context" else OTHER_ID
    forged = AuthenticatedPrincipal(**{**RECORD_CASES[1][1], changed_field: replacement})
    with pytest.raises(AuthenticationFailure) as caught:
        store.revalidate(principal=forged)
    assert caught.value.kind is AuthenticationFailureKind.UNAUTHENTICATED
    assert store.resolve(credential=issued.credential) is issued.principal


def test_request_context_user_id_and_auth_session_id_cannot_authenticate_in_the_store_fake():
    store: AuthSessionStore = SyntheticStore()
    issued = store.create(user_id=USER_ID)
    for supplied in (issued.principal.request_context, str(USER_ID), str(AUTH_SESSION_ID)):
        with pytest.raises(AuthenticationFailure) as caught:
            store.resolve(credential=supplied)
        assert caught.value.kind is AuthenticationFailureKind.UNAUTHENTICATED
    assert store.resolve(credential=issued.credential) is issued.principal


class SyntheticVerifier(OIDCVerifier):
    """Injection fake only; returning a fixture does not verify an OIDC token."""

    def __init__(self, *, observed_nonce="pending-nonce"):
        self.observed_nonce = observed_nonce
        self.identity = VerifiedExternalIdentity(issuer="https://identity.example.test", subject="fixture-subject")
        self.calls = []

    async def verify_callback(
        self, *, code: str, state: str, expected_state: str, expected_nonce: str, code_verifier: str,
    ) -> VerifiedExternalIdentity:
        self.calls.append({
            "code": code, "state": state, "expected_state": expected_state,
            "expected_nonce": expected_nonce, "code_verifier": code_verifier,
        })
        if state != expected_state or self.observed_nonce != expected_nonce:
            raise AuthenticationFailure(AuthenticationFailureKind.INVALID_REQUEST_CONTEXT)
        return self.identity


CALLBACK_VALUES = {
    "code": "PRIVATE_CALLBACK_CODE_SENTINEL",
    "state": "pending-state",
    "expected_state": "pending-state",
    "expected_nonce": "pending-nonce",
    "code_verifier": "PRIVATE_PKCE_VERIFIER_SENTINEL",
}


def complete_without_suspension(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    else:
        raise AssertionError("The synthetic verifier must finish without external work.")
    finally:
        coroutine.close()


def test_async_verifier_fake_is_injected_and_returns_only_the_exact_identity():
    implementation = SyntheticVerifier()
    verifier: OIDCVerifier = implementation
    result = complete_without_suspension(verifier.verify_callback(**CALLBACK_VALUES))
    assert result is implementation.identity
    assert type(result) is VerifiedExternalIdentity
    assert implementation.calls == [CALLBACK_VALUES]
    assert tuple(field.name for field in fields(result)) == ("issuer", "subject")


@pytest.mark.parametrize("mismatch", ("state", "nonce"))
def test_verifier_fake_rejects_invalid_callback_bindings_without_private_details(mismatch):
    verifier = SyntheticVerifier(observed_nonce="wrong-nonce" if mismatch == "nonce" else "pending-nonce")
    supplied = {**CALLBACK_VALUES, **({"state": PRIVATE_MARKER} if mismatch == "state" else {})}
    with pytest.raises(AuthenticationFailure) as caught:
        complete_without_suspension(verifier.verify_callback(**supplied))
    assert caught.value.kind is AuthenticationFailureKind.INVALID_REQUEST_CONTEXT
    assert_safe_error(caught.value, "Invalid authentication request context.")
    for value in (supplied["code"], supplied["code_verifier"]):
        assert value not in str(caught.value)
        assert value not in repr(caught.value)
        assert value not in repr(caught.value.args)


def test_verifier_fake_maps_private_adapter_failure_without_cause_or_context():
    class UnavailableVerifier(OIDCVerifier):
        async def verify_callback(
            self, *, code: str, state: str, expected_state: str, expected_nonce: str, code_verifier: str,
        ) -> VerifiedExternalIdentity:
            # Illustrate adapter translation outside the handler; no real call.
            try:
                raise RuntimeError(PRIVATE_MARKER + code + code_verifier)
            except RuntimeError:
                pass
            raise AuthenticationFailure(AuthenticationFailureKind.UNAVAILABLE) from None

    verifier: OIDCVerifier = UnavailableVerifier()
    with pytest.raises(AuthenticationFailure) as caught:
        complete_without_suspension(verifier.verify_callback(**CALLBACK_VALUES))
    error = caught.value
    assert error.kind is AuthenticationFailureKind.UNAVAILABLE
    assert_safe_error(error, "Authentication is temporarily unavailable.")
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for marker in (PRIVATE_MARKER, CALLBACK_VALUES["code"], CALLBACK_VALUES["code_verifier"]):
        assert marker not in str(error)
        assert marker not in repr(error)
        assert marker not in repr(error.args)
        assert marker not in rendered


def test_auth_module_imports_only_pure_standard_library_contract_dependencies():
    tree = ast.parse(Path(auth.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module)
    assert imported <= {"abc", "dataclasses", "enum", "typing", "uuid", "__future__"}


def test_fresh_auth_import_performs_no_network_file_environment_or_clock_access():
    backend = Path(__file__).resolve().parents[1] / "backend"
    script = textwrap.dedent("""
        import abc
        import builtins
        import dataclasses
        import datetime
        import enum
        import io
        import os
        from pathlib import Path
        import socket
        import sys
        import time
        import typing
        import urllib.request
        import uuid

        sys.path.insert(0, sys.argv[1])
        import app
        assert 'app.auth' not in sys.modules

        def forbidden(*args, **kwargs):
            raise AssertionError('Authentication contracts must import without external access.')

        class ForbiddenEnvironment(dict):
            __getitem__ = forbidden
            __setitem__ = forbidden
            __delitem__ = forbidden
            __iter__ = forbidden
            __len__ = forbidden
            __contains__ = forbidden
            get = forbidden
            keys = forbidden
            values = forbidden
            items = forbidden
            copy = forbidden

        class ForbiddenDateTime(datetime.datetime):
            now = classmethod(forbidden)
            utcnow = classmethod(forbidden)
            today = classmethod(forbidden)

        class ForbiddenDate(datetime.date):
            today = classmethod(forbidden)

        for owner, names in (
            (builtins, ('open',)),
            (io, ('open',)),
            (os, ('open', 'getenv', 'putenv', 'unsetenv', 'urandom', 'listdir', 'scandir', 'stat', 'access')),
            (Path, ('open', 'read_text', 'read_bytes', 'write_text', 'write_bytes', 'iterdir', 'stat')),
            (socket, ('socket', 'create_connection', 'getaddrinfo')),
            (urllib.request, ('urlopen',)),
            (time, ('time', 'time_ns', 'monotonic', 'monotonic_ns', 'perf_counter', 'perf_counter_ns', 'process_time')),
            (uuid, ('uuid1', 'uuid3', 'uuid4', 'uuid5')),
        ):
            for name in names:
                setattr(owner, name, forbidden)
        os.environ = ForbiddenEnvironment()
        datetime.datetime = ForbiddenDateTime
        datetime.date = ForbiddenDate

        import app.auth
        assert app.auth.VerifiedExternalIdentity.__module__ == 'app.auth'
        print('auth import is pure')
    """)
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(backend)],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "auth import is pure\n"
