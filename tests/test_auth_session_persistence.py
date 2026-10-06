"""Local auth persistence only, using isolated PostgreSQL and injected inputs.

No provider work, HTTP authentication, cookies or interview ownership is exercised.
Clocks and generators are deterministic; concurrent provisioning uses independent
transactions and a barrier, never sleeps or a process-wide application lock.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier, current_thread
import traceback
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app import auth_persistence
from app.auth import (
    AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind,
    IssuedAuthSession, VerifiedExternalIdentity,
)
from app.auth_persistence import (
    AUTH_SESSION_TOKEN_ENTROPY_BYTES, AUTH_SESSION_TOKEN_HASH_SCHEME,
    PostgreSQLAuthSessionStore,
)
from app.database_models import AuthSession, User

NOW = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
LIFETIME = timedelta(minutes=45)
ISSUER = "https://identity.example.test/realm"
PRIVATE_MARKER = "PRIVATE_AUTH_INFRASTRUCTURE_DETAIL_SENTINEL"
MESSAGES = {
    AuthenticationFailureKind.UNAUTHENTICATED: "Authentication required.",
    AuthenticationFailureKind.INVALID_REQUEST_CONTEXT: "Invalid authentication request context.",
    AuthenticationFailureKind.UNAVAILABLE: "Authentication is temporarily unavailable.",
}
UNSET = object()


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Provider requests are forbidden in local authentication persistence tests")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


class Clock:
    def __init__(self):
        self.value = NOW
        self.error = None
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


class Values:
    def __init__(self, prefix):
        self.prefix = prefix
        self.calls = 0
        self.output = UNSET
        self.error = None

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.output is not UNSET:
            return self.output
        return f"{self.prefix}_{self.calls:04d}_" + "x" * 48


@dataclass
class Harness:
    store: PostgreSQLAuthSessionStore
    factory: object
    clock: Clock
    credentials: Values
    contexts: Values

    def user(self, subject="subject-1", issuer=ISSUER):
        return self.store.provision_user(identity=VerifiedExternalIdentity(issuer=issuer, subject=subject))


@pytest.fixture
def harness(postgres_session_factory):
    clock, credentials, contexts = Clock(), Values("PRIVATE_CREDENTIAL"), Values("PRIVATE_CONTEXT")
    store = PostgreSQLAuthSessionStore(
        postgres_session_factory, session_lifetime=LIFETIME, clock=clock,
        credential_generator=credentials, request_context_generator=contexts,
    )
    return Harness(store, postgres_session_factory, clock, credentials, contexts)


def records(factory, model):
    with factory() as database:
        return {row["id"]: dict(row) for row in database.execute(select(model.__table__)).mappings()}


def assert_failure(operation, kind, *private_values):
    with pytest.raises(AuthenticationFailure) as caught:
        operation()
    error = caught.value
    assert error.kind is kind
    assert str(error) == MESSAGES[kind]
    assert error.args == (MESSAGES[kind],)
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for value in (PRIVATE_MARKER, *private_values):
        assert value not in str(error)
        assert value not in repr(error)
        assert value not in repr(error.args)
        assert value not in rendered
    return error


def test_first_and_repeated_verified_identity_provision_one_exact_internal_user(harness):
    identity = VerifiedExternalIdentity(issuer=ISSUER, subject="opaque-provider-subject")
    first = harness.store.provision_user(identity=identity)
    before = records(harness.factory, User)
    assert isinstance(first, UUID) and first.version == 4
    assert harness.store.provision_user(identity=identity) == first
    assert records(harness.factory, User) == before
    assert set(before) == {first}
    assert before[first]["auth_provider"] == identity.issuer
    assert before[first]["provider_subject"] == identity.subject
    assert before[first]["created_at"].tzinfo is not None
    assert set(before[first]) == {"id", "auth_provider", "provider_subject", "created_at"}
    assert records(harness.factory, AuthSession) == {}
    assert harness.credentials.calls == harness.contexts.calls == 0


@pytest.mark.parametrize("first,second", [
    ((ISSUER, "shared"), ("https://other.example.test/realm", "shared")),
    ((ISSUER, "first-subject"), (ISSUER, "second-subject")),
    ((ISSUER, "Subject"), (ISSUER, "subject")),
    ((ISSUER, "subject"), ("https://Identity.example.test/realm", "subject")),
    ((ISSUER, "subject"), (ISSUER + "/", "subject")),
    ((ISSUER, "subject"), (" " + ISSUER + " ", "subject")),
    ((ISSUER, "subject"), (ISSUER, " subject ")),
    ((ISSUER, "subject-\u00e9"), (ISSUER, "subject-e\u0301")),
])
def test_provisioning_never_merges_distinct_exact_issuer_subject_pairs(harness, first, second):
    identities = [VerifiedExternalIdentity(issuer=issuer, subject=subject) for issuer, subject in (first, second)]
    identifiers = [harness.store.provision_user(identity=identity) for identity in identities]
    assert identifiers[0] != identifiers[1]
    stored = records(harness.factory, User)
    for identifier, identity in zip(identifiers, identities):
        assert (stored[identifier]["auth_provider"], stored[identifier]["provider_subject"]) == (identity.issuer, identity.subject)
        assert harness.store.provision_user(identity=identity) == identifier


def test_simultaneous_first_login_insert_races_converge_through_postgresql_uniqueness(harness, postgres_engine):
    participants = 4
    barrier = Barrier(participants)
    observed = []
    identity = VerifiedExternalIdentity(issuer=ISSUER, subject="simultaneous-first-login")

    def before_insert(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("auth-provision") and statement.lstrip().upper().startswith("INSERT INTO USERS"):
            observed.append(statement)
            barrier.wait(timeout=10)

    event.listen(postgres_engine, "before_cursor_execute", before_insert)
    try:
        with ThreadPoolExecutor(max_workers=participants, thread_name_prefix="auth-provision") as pool:
            futures = [pool.submit(harness.store.provision_user, identity=identity) for _ in range(participants)]
            identifiers = [future.result(timeout=15) for future in futures]
    finally:
        event.remove(postgres_engine, "before_cursor_execute", before_insert)
    assert len(observed) == participants
    assert len(set(identifiers)) == 1
    with harness.factory() as database:
        assert database.scalar(select(func.count()).select_from(User).where(
            User.auth_provider == identity.issuer, User.provider_subject == identity.subject,
        )) == 1
    assert set(records(harness.factory, User)) == {identifiers[0]}


def test_issuance_persists_only_defined_digest_and_separate_context_after_commit(harness, caplog, capsys):
    user_id = harness.user()
    issued = harness.store.create(user_id=user_id)
    assert type(issued) is IssuedAuthSession
    assert type(issued.principal) is AuthenticatedPrincipal
    assert AUTH_SESSION_TOKEN_HASH_SCHEME == "sha256-v1"
    assert AUTH_SESSION_TOKEN_ENTROPY_BYTES == 32
    expected_digest = sha256(issued.credential.encode("utf-8")).digest()
    stored = records(harness.factory, AuthSession)
    assert set(stored) == {issued.principal.auth_session_id}
    row = stored[issued.principal.auth_session_id]
    assert row["token_hash"] == expected_digest
    assert type(row["token_hash"]) is bytes and len(row["token_hash"]) == 32
    assert row["user_id"] == user_id == issued.principal.user_id
    assert row["request_context"] == issued.principal.request_context
    assert row["created_at"] == NOW
    assert row["expires_at"] == NOW + LIFETIME
    assert row["created_at"].tzinfo is not None and row["expires_at"].tzinfo is not None
    assert issued.credential not in row.values()
    assert set(row) == {"id", "token_hash", "user_id", "created_at", "expires_at", "request_context"}
    assert issued.principal.request_context not in {
        issued.credential, str(user_id), str(issued.principal.auth_session_id), expected_digest.hex(),
    }
    assert harness.credentials.calls == harness.contexts.calls == 1
    with harness.factory() as database:
        model = database.get(AuthSession, issued.principal.auth_session_id)
        rendered = " ".join(repr(value) for value in (harness.store, issued, issued.principal, model))
    output = capsys.readouterr()
    rendered += caplog.text + output.out + output.err
    for private in (issued.credential, issued.principal.request_context, expected_digest.hex(), repr(expected_digest)):
        assert private not in rendered


def test_secure_defaults_make_independent_32_byte_entropy_requests(harness, monkeypatch):
    credential, context = "secure-generated-token-" + "a" * 43, "secure-generated-context-" + "b" * 43
    generated = iter((credential, context))
    requests = []

    def secure_value(entropy_bytes):
        requests.append(entropy_bytes)
        return next(generated)

    monkeypatch.setattr(auth_persistence.secrets, "token_urlsafe", secure_value)
    store = PostgreSQLAuthSessionStore(harness.factory, session_lifetime=LIFETIME, clock=harness.clock)
    issued = store.create(user_id=harness.user())
    assert requests == [32, 32]
    assert issued.credential == credential
    assert issued.principal.request_context == context
    assert store.resolve(credential=credential) == issued.principal


def test_resolve_returns_frozen_snapshot_and_revalidation_never_refreshes_or_rotates(harness):
    issued = harness.store.create(user_id=harness.user())
    before = records(harness.factory, AuthSession)
    harness.clock.value += timedelta(minutes=5)
    principal = harness.store.resolve(credential=issued.credential)
    assert type(principal) is AuthenticatedPrincipal
    assert principal == issued.principal
    with pytest.raises(FrozenInstanceError):
        principal.user_id = uuid4()
    with pytest.raises(FrozenInstanceError):
        principal.request_context = "changed-context"
    assert harness.store.revalidate(principal=principal) is None
    assert records(harness.factory, AuthSession) == before
    assert harness.credentials.calls == harness.contexts.calls == 1


@pytest.mark.parametrize("changed_field", ["request_context", "user_id"])
def test_database_mutation_cannot_mutate_an_already_returned_principal(harness, changed_field):
    issued = harness.store.create(user_id=harness.user())
    principal = harness.store.resolve(credential=issued.credential)
    replacement = "new-independent-context" if changed_field == "request_context" else harness.user("another-user")
    with harness.factory.begin() as database:
        database.execute(update(AuthSession).where(AuthSession.id == principal.auth_session_id).values(**{changed_field: replacement}))
    assert getattr(principal, changed_field) != replacement
    resolved = harness.store.resolve(credential=issued.credential)
    assert getattr(resolved, changed_field) == replacement
    expected = (AuthenticationFailureKind.INVALID_REQUEST_CONTEXT if changed_field == "request_context"
                else AuthenticationFailureKind.UNAUTHENTICATED)
    assert_failure(lambda: harness.store.revalidate(principal=principal), expected)


@pytest.mark.parametrize("operation", ["resolve", "revalidate"])
@pytest.mark.parametrize("offset,live", [(-1, True), (0, False), (1, False)])
def test_exact_expiry_boundary_and_adjacent_instants(harness, operation, offset, live):
    issued = harness.store.create(user_id=harness.user())
    harness.clock.value = NOW + LIFETIME + timedelta(microseconds=offset)
    invoke = (lambda: harness.store.resolve(credential=issued.credential)) if operation == "resolve" else (
        lambda: harness.store.revalidate(principal=issued.principal))
    if live:
        expected = issued.principal if operation == "resolve" else None
        assert invoke() == expected
    else:
        assert_failure(invoke, AuthenticationFailureKind.UNAUTHENTICATED, issued.credential, issued.principal.request_context)


def test_aware_non_utc_clock_represents_the_same_creation_and_expiry_instants(harness):
    harness.clock.value = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    issued = harness.store.create(user_id=harness.user())
    row = records(harness.factory, AuthSession)[issued.principal.auth_session_id]
    assert row["created_at"] == NOW
    assert row["expires_at"] == NOW + LIFETIME
    assert harness.store.resolve(credential=issued.credential) == issued.principal
    harness.clock.value = row["expires_at"].astimezone(timezone(timedelta(hours=-4)))
    assert_failure(lambda: harness.store.resolve(credential=issued.credential), AuthenticationFailureKind.UNAUTHENTICATED)


def test_credential_lookup_never_treats_context_or_internal_ids_as_credentials(harness):
    issued = harness.store.create(user_id=harness.user())
    for value in (issued.principal.request_context, str(issued.principal.user_id), str(issued.principal.auth_session_id), "unknown-provider-token"):
        assert_failure(lambda: harness.store.resolve(credential=value), AuthenticationFailureKind.UNAUTHENTICATED, value)
    assert harness.store.resolve(credential=issued.credential) == issued.principal


@pytest.mark.parametrize("changed_field", ["user_id", "auth_session_id", "request_context"])
def test_revalidation_checks_exact_user_session_and_request_context(harness, changed_field):
    issued = harness.store.create(user_id=harness.user())
    replacement = "wrong-request-context" if changed_field == "request_context" else uuid4()
    forged = replace(issued.principal, **{changed_field: replacement})
    expected = (AuthenticationFailureKind.INVALID_REQUEST_CONTEXT if changed_field == "request_context"
                else AuthenticationFailureKind.UNAUTHENTICATED)
    assert_failure(lambda: harness.store.revalidate(principal=forged), expected, issued.credential, issued.principal.request_context)
    assert harness.store.resolve(credential=issued.credential) == issued.principal


def test_expired_or_wrong_user_context_mismatches_fail_as_unauthenticated(harness):
    issued = harness.store.create(user_id=harness.user())
    context_mismatch = replace(issued.principal, request_context="wrong-context")
    wrong_user = replace(context_mismatch, user_id=harness.user("different-user"))
    assert_failure(lambda: harness.store.revalidate(principal=wrong_user), AuthenticationFailureKind.UNAUTHENTICATED)
    harness.clock.value = NOW + LIFETIME
    assert_failure(lambda: harness.store.revalidate(principal=context_mismatch), AuthenticationFailureKind.UNAUTHENTICATED)


def test_multiple_user_sessions_are_independent_and_revoke_only_removes_its_target(harness):
    user_id, other_user = harness.user(), harness.user("other-user")
    first = harness.store.create(user_id=user_id)
    second = harness.store.create(user_id=user_id)
    other = harness.store.create(user_id=other_user)
    assert len({value.credential for value in (first, second, other)}) == 3
    assert len({value.principal.request_context for value in (first, second, other)}) == 3
    assert len({value.principal.auth_session_id for value in (first, second, other)}) == 3
    before = records(harness.factory, AuthSession)
    users_before = records(harness.factory, User)
    assert harness.store.revoke(auth_session_id=first.principal.auth_session_id) is None
    assert harness.store.revoke(auth_session_id=first.principal.auth_session_id) is None
    assert harness.store.revoke(auth_session_id=uuid4()) is None
    assert records(harness.factory, AuthSession) == {
        identifier: row for identifier, row in before.items() if identifier != first.principal.auth_session_id
    }
    assert records(harness.factory, User) == users_before
    assert_failure(lambda: harness.store.resolve(credential=first.credential), AuthenticationFailureKind.UNAUTHENTICATED)
    assert_failure(lambda: harness.store.revalidate(principal=first.principal), AuthenticationFailureKind.UNAUTHENTICATED)
    for value in (second, other):
        assert harness.store.resolve(credential=value.credential) == value.principal
        assert harness.store.revalidate(principal=value.principal) is None


def test_unknown_user_cannot_receive_an_auth_session(harness):
    assert_failure(lambda: harness.store.create(user_id=uuid4()), AuthenticationFailureKind.UNAUTHENTICATED)
    assert records(harness.factory, User) == records(harness.factory, AuthSession) == {}


class NeverFactory:
    def begin(self):
        raise AssertionError("Invalid authentication inputs must not access the database")


def test_constructor_requires_explicit_lifetime_and_performs_no_dependency_work(monkeypatch):
    clock, credentials, contexts = Clock(), Values("PRIVATE_CREDENTIAL"), Values("PRIVATE_CONTEXT")
    entropy_requests = []

    def forbidden_entropy(*args, **kwargs):
        entropy_requests.append(True)
        raise AssertionError("Construction must not generate credentials")

    monkeypatch.setattr(auth_persistence.secrets, "token_urlsafe", forbidden_entropy)
    with pytest.raises(TypeError):
        PostgreSQLAuthSessionStore(NeverFactory())
    store = PostgreSQLAuthSessionStore(
        NeverFactory(), session_lifetime=LIFETIME, clock=clock,
        credential_generator=credentials, request_context_generator=contexts,
    )
    defaults = PostgreSQLAuthSessionStore(NeverFactory(), session_lifetime=LIFETIME, clock=clock)
    assert type(store) is type(defaults) is PostgreSQLAuthSessionStore
    assert clock.calls == credentials.calls == contexts.calls == 0
    assert entropy_requests == []


class PrivateValue:
    def __str__(self):
        raise AssertionError("Untrusted values must not be formatted")

    __repr__ = __str__


class PrivateText(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("String subclass methods must not be called")

    def encode(self, *args, **kwargs):
        raise AssertionError("String subclass methods must not be called")


class DerivedUUID(UUID):
    pass


class DerivedPrincipal(AuthenticatedPrincipal):
    pass


class DerivedIdentity(VerifiedExternalIdentity):
    pass


INVALID_INPUTS = [
    *[(method, argument, value) for method, argument in (("create", "user_id"), ("revoke", "auth_session_id"))
      for value in (None, str(uuid4()), 1, True, PrivateValue(), DerivedUUID(int=1))],
    *[("resolve", "credential", value) for value in (
        None, "", " \t\n ", b"private-credential", PrivateText("private-credential"), PrivateValue(),
        "private\x00credential", "private\ud800credential",
    )],
    *[("revalidate", "principal", value) for value in (
        None, uuid4(), "claimed-principal", PrivateValue(),
        DerivedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context="private-context"),
    )],
    *[("provision_user", "identity", value) for value in (
        None, {"issuer": ISSUER, "subject": "claimed"}, "claimed-identity", PrivateValue(),
        DerivedIdentity(issuer=ISSUER, subject="derived"),
        VerifiedExternalIdentity(issuer=ISSUER, subject="private\x00subject"),
        VerifiedExternalIdentity(issuer=ISSUER, subject="private\ud800subject"),
    )],
]


@pytest.mark.parametrize("method,argument,value", INVALID_INPUTS,
                         ids=[f"{method}-{index}" for index, (method, _, _) in enumerate(INVALID_INPUTS)])
def test_invalid_inputs_fail_closed_without_database_access_or_rendering(method, argument, value):
    store = PostgreSQLAuthSessionStore(NeverFactory(), session_lifetime=LIFETIME)
    assert_failure(lambda: getattr(store, method)(**{argument: value}), AuthenticationFailureKind.UNAUTHENTICATED)


@pytest.mark.parametrize("generator", ["credentials", "contexts"])
@pytest.mark.parametrize("value", [
    pytest.param(None, id="null"), pytest.param("", id="empty"),
    pytest.param(" \t\n ", id="blank"), pytest.param(b"private", id="bytes"),
    pytest.param(PrivateText("private"), id="subclass"),
    pytest.param("private\x00value", id="nul"), pytest.param("private\ud800value", id="unencodable"),
])
def test_invalid_generator_outputs_are_unavailable_without_partial_sessions(harness, generator, value):
    user_id = harness.user()
    getattr(harness, generator).output = value
    assert_failure(lambda: harness.store.create(user_id=user_id), AuthenticationFailureKind.UNAVAILABLE)
    assert records(harness.factory, AuthSession) == {}
    assert set(records(harness.factory, User)) == {user_id}


@pytest.mark.parametrize("dependency", ["credentials", "contexts", "clock"])
def test_dependency_exceptions_are_private_and_issuance_is_atomic(harness, dependency, caplog, capsys):
    user_id = harness.user()
    getattr(harness, dependency).error = RuntimeError(PRIVATE_MARKER)
    assert_failure(lambda: harness.store.create(user_id=user_id), AuthenticationFailureKind.UNAVAILABLE)
    assert records(harness.factory, AuthSession) == {}
    captured = capsys.readouterr()
    assert PRIVATE_MARKER not in caplog.text + captured.out + captured.err


@pytest.mark.parametrize("operation", ["create", "resolve", "revalidate"])
@pytest.mark.parametrize("clock_value", [None, datetime(2026, 1, 2), NOW.date()])
def test_clock_must_supply_an_aware_datetime_without_mutating_live_rows(harness, operation, clock_value):
    user_id = harness.user()
    issued = harness.store.create(user_id=user_id)
    before = records(harness.factory, AuthSession)
    harness.clock.value = clock_value
    arguments = {"create": {"user_id": user_id}, "resolve": {"credential": issued.credential},
                 "revalidate": {"principal": issued.principal}}
    assert_failure(lambda: getattr(harness.store, operation)(**arguments[operation]), AuthenticationFailureKind.UNAVAILABLE)
    assert records(harness.factory, AuthSession) == before


class UnavailableFactory:
    def begin(self):
        raise SQLAlchemyError(PRIVATE_MARKER)


@pytest.mark.parametrize("method,arguments", [
    ("provision_user", {"identity": VerifiedExternalIdentity(issuer=ISSUER, subject="subject")}),
    ("create", {"user_id": uuid4()}),
    ("resolve", {"credential": "private-opaque-credential"}),
    ("revalidate", {"principal": AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context="private-context")}),
    ("revoke", {"auth_session_id": uuid4()}),
])
def test_database_unavailability_never_exposes_exception_details_or_chains(method, arguments, caplog, capsys):
    store = PostgreSQLAuthSessionStore(
        UnavailableFactory(), session_lifetime=LIFETIME, clock=Clock(),
        credential_generator=Values("PRIVATE_CREDENTIAL"), request_context_generator=Values("PRIVATE_CONTEXT"),
    )
    assert_failure(lambda: getattr(store, method)(**arguments), AuthenticationFailureKind.UNAVAILABLE)
    captured = capsys.readouterr()
    assert PRIVATE_MARKER not in caplog.text + captured.out + captured.err


@pytest.mark.parametrize("operation,statement_prefix", [
    ("provision_user", "INSERT INTO USERS"), ("create", "INSERT INTO AUTH_SESSIONS"),
    ("revoke", "DELETE FROM AUTH_SESSIONS"),
])
def test_database_failure_after_mutation_rolls_back_provision_issue_and_revoke(harness, postgres_engine, operation, statement_prefix):
    user_id = harness.user()
    issued = harness.store.create(user_id=user_id)
    other = harness.store.create(user_id=harness.user("other-user"))
    before_users, before_sessions = records(harness.factory, User), records(harness.factory, AuthSession)
    invoked = []

    def after_mutation(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(statement_prefix):
            invoked.append(True)
            raise SQLAlchemyError(PRIVATE_MARKER)

    arguments = {
        "provision_user": {"identity": VerifiedExternalIdentity(issuer=ISSUER, subject="rollback-new-user")},
        "create": {"user_id": user_id}, "revoke": {"auth_session_id": issued.principal.auth_session_id},
    }
    event.listen(postgres_engine, "after_cursor_execute", after_mutation)
    try:
        assert_failure(lambda: getattr(harness.store, operation)(**arguments[operation]), AuthenticationFailureKind.UNAVAILABLE,
                       issued.credential, issued.principal.request_context)
    finally:
        event.remove(postgres_engine, "after_cursor_execute", after_mutation)
    assert invoked == [True]
    assert records(harness.factory, User) == before_users
    assert records(harness.factory, AuthSession) == before_sessions
    for value in (issued, other):
        assert harness.store.resolve(credential=value.credential) == value.principal


def test_duplicate_generated_credential_fails_privately_and_preserves_existing_sessions(harness, caplog, capsys):
    first = harness.store.create(user_id=harness.user())
    before = records(harness.factory, AuthSession)
    harness.credentials.output = first.credential
    other_user = harness.user("collision-user")
    assert_failure(lambda: harness.store.create(user_id=other_user), AuthenticationFailureKind.UNAVAILABLE,
                   first.credential, first.principal.request_context, sha256(first.credential.encode()).hexdigest())
    assert records(harness.factory, AuthSession) == before
    assert harness.store.resolve(credential=first.credential) == first.principal
    captured = capsys.readouterr()
    for private in (first.credential, first.principal.request_context):
        assert private not in caplog.text + captured.out + captured.err
