"""One-use PostgreSQL login state and offline callback coordination only."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass, fields
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import inspect
from threading import Barrier, current_thread, get_ident
import traceback

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError

from app import oidc_login
from app.auth import VerifiedExternalIdentity
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database_models import AuthSession, OIDCLoginTransaction, StoredInterviewSession, User
from app.oidc_failure import OIDCFailure, OIDCFailureKind
from app.oidc_login import (
    ConsumedLoginTransaction, IssuedLoginTransaction, OIDC_LOGIN_ENTROPY_BYTES,
    OIDC_LOGIN_TRANSACTION_LIFETIME, OIDC_STATE_HASH_SCHEME,
    PostgreSQLOIDCLoginTransactionStore, verify_login_callback,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
LIFETIME = timedelta(minutes=7)
PRIVATE = "PRIVATE_LOGIN_DATABASE_PROVIDER_SECRET_SENTINEL"
CODE = "PRIVATE_AUTHORIZATION_CODE_SENTINEL"
MESSAGES = {
    OIDCFailureKind.INVALID_STATE: "Invalid login state.",
    OIDCFailureKind.EXCHANGE_FAILED: "Unable to exchange authorization code.",
    OIDCFailureKind.INVALID_TOKEN: "Invalid identity token.",
    OIDCFailureKind.UNAVAILABLE: "OIDC login is temporarily unavailable.",
}
UNSET = object()


@pytest.fixture(autouse=True)
def no_network_or_local_auth_issuance(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Login foundation tests cannot call providers or issue local authentication sessions.")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(PostgreSQLAuthSessionStore, "create", blocked)


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
        return self.output if self.output is not UNSET else f"{self.prefix}_{self.calls}_" + "x" * 48


@dataclass
class Harness:
    store: PostgreSQLOIDCLoginTransactionStore
    factory: object
    clock: Clock
    states: Values
    nonces: Values
    verifiers: Values


@pytest.fixture
def harness(postgres_session_factory):
    clock, states, nonces, verifiers = Clock(), Values("STATE"), Values("NONCE"), Values("VERIFIER")
    store = PostgreSQLOIDCLoginTransactionStore(
        postgres_session_factory, transaction_lifetime=LIFETIME, clock=clock,
        state_generator=states, nonce_generator=nonces, verifier_generator=verifiers,
    )
    return Harness(store, postgres_session_factory, clock, states, nonces, verifiers)


def records(factory, model=OIDCLoginTransaction):
    with factory() as database:
        return {row["id"]: dict(row) for row in database.execute(select(model.__table__)).mappings()}


def assert_failure(operation, kind, *private_values):
    with pytest.raises(OIDCFailure) as caught:
        operation()
    error = caught.value
    assert type(error) is OIDCFailure and error.kind is kind
    assert error.args == (MESSAGES[kind],) and str(error) == MESSAGES[kind]
    assert error.__cause__ is None and error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for value in (PRIVATE, CODE, *private_values):
        assert value not in rendered + repr(error) + repr(error.args)
    return error


def test_creation_persists_only_exact_state_digest_and_separate_server_side_values(harness, caplog, capsys):
    issued = harness.store.create_login_transaction()
    stored = records(harness.factory)
    assert type(issued) is IssuedLoginTransaction and len(stored) == 1
    row = next(iter(stored.values()))
    assert OIDC_STATE_HASH_SCHEME == "sha256-v1" and OIDC_LOGIN_ENTROPY_BYTES == 32
    assert row["state_hash"] == sha256(issued.state.encode("utf-8")).digest()
    assert type(row["state_hash"]) is bytes and len(row["state_hash"]) == 32
    assert row["nonce"] == issued.nonce and row["code_verifier"] == "VERIFIER_1_" + "x" * 48
    assert row["created_at"] == NOW and row["expires_at"] == NOW + LIFETIME
    assert set(row) == {"id", "state_hash", "nonce", "code_verifier", "created_at", "expires_at"}
    assert issued.state not in row.values()
    assert len({issued.state, issued.nonce, row["code_verifier"]}) == 3
    assert harness.states.calls == harness.nonces.calls == harness.verifiers.calls == 1
    assert {item.name for item in fields(issued)} == {"state", "nonce", "code_challenge", "code_challenge_method"}
    assert not hasattr(issued, "code_verifier") and not hasattr(issued, "id")
    assert row["code_verifier"] not in str(issued) + repr(issued)
    assert issued.state not in repr(issued) and issued.nonce not in repr(issued)
    for model in (User, AuthSession, StoredInterviewSession):
        assert records(harness.factory, model) == {}
    assert caplog.records == [] and capsys.readouterr() == ("", "")


def test_pkce_s256_matches_the_rfc_7636_vector_without_padding(harness):
    harness.verifiers.output = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    issued = harness.store.create_login_transaction()
    assert issued.code_challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert issued.code_challenge_method == "S256" and "=" not in issued.code_challenge
    assert harness.store.consume_login_transaction(issued.state).code_verifier == harness.verifiers.output


@pytest.mark.parametrize("length", [43, 128])
def test_pkce_accepts_the_exact_unreserved_length_boundaries(harness, length):
    harness.verifiers.output = ("Aa0._~-" * 19)[:length]
    issued = harness.store.create_login_transaction()
    expected = base64.urlsafe_b64encode(sha256(harness.verifiers.output.encode("ascii")).digest()).decode().rstrip("=")
    assert issued.code_challenge == expected
    assert harness.store.consume_login_transaction(issued.state).code_verifier == harness.verifiers.output


def test_secure_defaults_draw_three_independent_32_byte_values_per_transaction(postgres_session_factory, monkeypatch):
    calls = []

    def opaque(size):
        calls.append(size)
        return chr(ord("A") + len(calls) - 1) * 43

    monkeypatch.setattr(oidc_login.secrets, "token_urlsafe", opaque)
    store = PostgreSQLOIDCLoginTransactionStore(postgres_session_factory, clock=lambda: NOW)
    first, second = store.create_login_transaction(), store.create_login_transaction()
    assert calls == [32] * 6
    assert (first.state, first.nonce, second.state, second.nonce) == ("A" * 43, "B" * 43, "D" * 43, "E" * 43)
    stored = records(postgres_session_factory)
    assert {row["code_verifier"] for row in stored.values()} == {"C" * 43, "F" * 43}
    assert all(row["expires_at"] == NOW + timedelta(minutes=10) for row in stored.values())
    assert OIDC_LOGIN_TRANSACTION_LIFETIME == timedelta(minutes=10)


def test_actual_secure_defaults_are_nonconstant_urlsafe_and_independent(postgres_session_factory):
    store = PostgreSQLOIDCLoginTransactionStore(postgres_session_factory, clock=lambda: NOW)
    issued = [store.create_login_transaction() for _ in range(2)]
    values = [value for item in issued for value in (item.state, item.nonce)]
    values += [row["code_verifier"] for row in records(postgres_session_factory).values()]
    assert len(set(values)) == 6
    assert all(len(value) == 43 and set(value) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
               for value in values)


def test_constructor_performs_no_database_clock_or_entropy_activity():
    def forbidden(*args, **kwargs):
        raise AssertionError("Constructing a login store must be inert.")

    store = PostgreSQLOIDCLoginTransactionStore(
        forbidden, clock=forbidden, state_generator=forbidden,
        nonce_generator=forbidden, verifier_generator=forbidden,
    )
    assert type(store) is PostgreSQLOIDCLoginTransactionStore
    assert inspect.signature(PostgreSQLOIDCLoginTransactionStore).parameters["transaction_lifetime"].default == timedelta(minutes=10)


@pytest.mark.parametrize("lifetime", [None, 1, True, timedelta(0), timedelta(seconds=-1)])
def test_constructor_rejects_invalid_lifetime_without_formatting_values(lifetime):
    with pytest.raises(ValueError, match="positive timedelta"):
        PostgreSQLOIDCLoginTransactionStore(object(), transaction_lifetime=lifetime)


@pytest.mark.parametrize("dependency", ["clock", "state_generator", "nonce_generator", "verifier_generator"])
def test_constructor_rejects_noncallable_dependencies(dependency):
    with pytest.raises(TypeError, match="must be callable"):
        PostgreSQLOIDCLoginTransactionStore(object(), **{dependency: object()})


def test_valid_state_consumes_committed_record_once_and_returns_only_private_verification_values(harness):
    issued = harness.store.create_login_transaction()
    verifier = next(iter(records(harness.factory).values()))["code_verifier"]
    consumed = harness.store.consume_login_transaction(issued.state)
    assert type(consumed) is ConsumedLoginTransaction
    assert consumed.nonce == issued.nonce and consumed.code_verifier == verifier
    assert {item.name for item in fields(consumed)} == {"nonce", "code_verifier"}
    for value in (issued.state, issued.nonce, verifier):
        assert value not in repr(consumed) + repr(harness.store)
    with pytest.raises(FrozenInstanceError):
        issued.state = "replacement-state"
    with pytest.raises(FrozenInstanceError):
        consumed.code_verifier = "replacement-verifier"
    assert records(harness.factory) == {}
    assert_failure(lambda: harness.store.consume_login_transaction(issued.state), OIDCFailureKind.INVALID_STATE, issued.state, verifier)


@pytest.mark.parametrize("difference,valid", [(-1, True), (0, False), (1, False)])
def test_expiry_boundary_is_exact_and_expired_deletion_commits(harness, difference, valid):
    issued = harness.store.create_login_transaction()
    harness.clock.value = NOW + LIFETIME + timedelta(microseconds=difference)
    if valid:
        assert harness.store.consume_login_transaction(issued.state).nonce == issued.nonce
    else:
        assert_failure(lambda: harness.store.consume_login_transaction(issued.state), OIDCFailureKind.INVALID_STATE, issued.state, issued.nonce)
    assert records(harness.factory) == {}
    assert_failure(lambda: harness.store.consume_login_transaction(issued.state), OIDCFailureKind.INVALID_STATE)


@pytest.mark.parametrize("original,altered", [
    ("Opaque-State-" + "x" * 43, "opaque-state-" + "x" * 43),
    ("Exact-State-" + "x" * 43, " Exact-State-" + "x" * 43),
    ("Exact-State-" + "x" * 43, "Exact-State-" + "x" * 43 + " "),
    ("State-é-" + "x" * 43, "State-e\u0301-" + "x" * 43),
])
def test_state_matching_never_normalizes_case_whitespace_or_unicode(harness, original, altered):
    harness.states.output = original
    issued = harness.store.create_login_transaction()
    before = records(harness.factory)
    assert_failure(lambda: harness.store.consume_login_transaction(altered), OIDCFailureKind.INVALID_STATE, original, altered)
    assert records(harness.factory) == before
    assert harness.store.consume_login_transaction(issued.state).nonce == issued.nonce


class HostileText(str):
    def strip(self, *args):
        raise AssertionError("String subclasses must not be invoked.")

    def encode(self, *args):
        raise AssertionError("String subclasses must not be invoked.")


@pytest.mark.parametrize("state", [
    None, True, 123, b"state", object(), "", " \t\n", "bad\x00state", "bad\ud800state", HostileText(PRIVATE),
], ids=["none", "bool", "int", "bytes", "object", "empty", "blank", "nul", "surrogate", "str-subclass"])
def test_malformed_state_is_the_same_private_invalid_state_failure_and_cannot_consume_rows(harness, state):
    harness.store.create_login_transaction()
    before = records(harness.factory)
    assert_failure(lambda: harness.store.consume_login_transaction(state), OIDCFailureKind.INVALID_STATE)
    assert records(harness.factory) == before


@pytest.mark.parametrize("generator,value", [
    ("states", None), ("states", ""), ("states", "bad\x00state"), ("states", "bad\ud800state"),
    ("nonces", None), ("nonces", " \n"), ("nonces", HostileText(PRIVATE)),
    ("verifiers", None), ("verifiers", "x" * 42), ("verifiers", "x" * 129),
    ("verifiers", "x" * 42 + "+"), ("verifiers", "x" * 42 + "/"),
    ("verifiers", "x" * 42 + "="), ("verifiers", "x" * 42 + "é"),
    ("verifiers", HostileText("x" * 43)),
], ids=[
    "state-none", "state-empty", "state-nul", "state-surrogate",
    "nonce-none", "nonce-blank", "nonce-str-subclass",
    "verifier-none", "verifier-short", "verifier-long", "verifier-plus", "verifier-slash",
    "verifier-padding", "verifier-nonascii", "verifier-str-subclass",
])
def test_invalid_generator_outputs_fail_closed_without_persisting_a_partial_transaction(harness, generator, value):
    getattr(harness, generator).output = value
    assert_failure(harness.store.create_login_transaction, OIDCFailureKind.UNAVAILABLE)
    assert records(harness.factory) == {}


@pytest.mark.parametrize("same_pair", [("states", "nonces"), ("states", "verifiers"), ("nonces", "verifiers")])
def test_generated_state_nonce_and_verifier_cannot_reuse_the_same_value(harness, same_pair):
    for generator in same_pair:
        getattr(harness, generator).output = "same-independent-value-" + "x" * 43
    assert_failure(harness.store.create_login_transaction, OIDCFailureKind.UNAVAILABLE)
    assert records(harness.factory) == {}


@pytest.mark.parametrize("dependency", ["clock", "states", "nonces", "verifiers"])
def test_clock_and_entropy_failures_discard_private_details_before_database_work(harness, dependency, caplog, capsys):
    getattr(harness, dependency).error = RuntimeError(PRIVATE)
    assert_failure(harness.store.create_login_transaction, OIDCFailureKind.UNAVAILABLE)
    assert records(harness.factory) == {}
    assert caplog.records == [] and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("clock_value", [None, NOW.replace(tzinfo=None), "clock", True])
def test_clock_values_require_an_actual_aware_datetime(harness, clock_value):
    harness.clock.value = clock_value
    assert_failure(harness.store.create_login_transaction, OIDCFailureKind.UNAVAILABLE)
    assert records(harness.factory) == {}


def test_clock_failure_rolls_back_the_delete_instead_of_releasing_private_values(harness):
    issued = harness.store.create_login_transaction()
    before = records(harness.factory)
    harness.clock.error = RuntimeError(PRIVATE)
    assert_failure(lambda: harness.store.consume_login_transaction(issued.state), OIDCFailureKind.UNAVAILABLE, issued.state, issued.nonce)
    assert records(harness.factory) == before
    harness.clock.error = None
    assert harness.store.consume_login_transaction(issued.state).nonce == issued.nonce


@pytest.mark.parametrize("operation,prefix", [("create", "INSERT INTO OIDC_LOGIN_TRANSACTIONS"), ("consume", "DELETE FROM OIDC_LOGIN_TRANSACTIONS")])
def test_database_error_after_mutation_rolls_back_and_has_no_secret_exception_chain(harness, postgres_engine, operation, prefix):
    issued = harness.store.create_login_transaction()
    before = records(harness.factory)
    private_values = (issued.state, issued.nonce, next(iter(before.values()))["code_verifier"])
    calls = []

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(prefix):
            calls.append(True)
            raise SQLAlchemyError(PRIVATE + "".join(private_values))

    event.listen(postgres_engine, "after_cursor_execute", fail)
    try:
        assert_failure(harness.store.create_login_transaction if operation == "create" else
                       lambda: harness.store.consume_login_transaction(issued.state), OIDCFailureKind.UNAVAILABLE, *private_values)
    finally:
        event.remove(postgres_engine, "after_cursor_execute", fail)
    assert calls == [True] and records(harness.factory) == before
    assert harness.store.consume_login_transaction(issued.state).nonce == issued.nonce


def test_duplicate_generated_state_fails_without_regeneration_or_overwriting_existing_transaction(harness):
    first = harness.store.create_login_transaction()
    before = records(harness.factory)
    harness.states.output = first.state
    assert_failure(harness.store.create_login_transaction, OIDCFailureKind.UNAVAILABLE, first.state, first.nonce)
    assert harness.states.calls == harness.nonces.calls == harness.verifiers.calls == 2
    assert records(harness.factory) == before
    assert harness.store.consume_login_transaction(first.state).nonce == first.nonce


def test_simultaneous_consumers_have_exactly_one_winner_without_application_locks(harness, postgres_engine):
    issued = harness.store.create_login_transaction()
    participants, observed = 4, []
    barrier = Barrier(participants)

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("login-consume") and statement.lstrip().upper().startswith("DELETE FROM OIDC_LOGIN_TRANSACTIONS"):
            observed.append(statement)
            barrier.wait(timeout=10)

    def consume():
        try:
            return harness.store.consume_login_transaction(issued.state)
        except OIDCFailure as error:
            assert error.__cause__ is None and error.__context__ is None
            return error.kind

    event.listen(postgres_engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=participants, thread_name_prefix="login-consume") as pool:
            futures = [pool.submit(consume) for _ in range(participants)]
            results = [future.result(timeout=15) for future in futures]
    finally:
        event.remove(postgres_engine, "before_cursor_execute", synchronize)
    winners = [result for result in results if type(result) is ConsumedLoginTransaction]
    assert len(observed) == participants and len(winners) == 1
    assert winners[0].nonce == issued.nonce
    assert results.count(OIDCFailureKind.INVALID_STATE) == participants - 1
    assert records(harness.factory) == {}


class FakeVerifier:
    def __init__(self):
        self.calls = []
        self.error = None
        self.result = VerifiedExternalIdentity(issuer="https://identity.example.test", subject="opaque-subject")
        self.observe = lambda: None

    async def verify_callback(self, **kwargs):
        self.calls.append(kwargs)
        self.observe()
        await asyncio.sleep(0)
        self.observe()
        if self.error is not None:
            raise self.error
        return self.result


def callback(harness, verifier, state):
    return asyncio.run(verify_login_callback(transactions=harness.store, verifier=verifier, code=CODE, state=state))


def test_callback_consumes_in_worker_then_verifies_after_commit_without_local_issuance(harness, postgres_engine):
    issued = harness.store.create_login_transaction()
    verifier_value = next(iter(records(harness.factory).values()))["code_verifier"]
    verifier, events, worker_threads, provider_threads = FakeVerifier(), [], [], []
    active = set()

    def begin(connection):
        active.add(id(connection))
        events.append("begin")

    def end(connection):
        active.discard(id(connection))
        events.append("commit")

    def statement(connection, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith("DELETE FROM OIDC_LOGIN_TRANSACTIONS"):
            worker_threads.append(get_ident())

    def outside_transaction():
        assert active == set() and postgres_engine.pool.checkedout() == 0
        assert asyncio.get_running_loop().is_running()
        provider_threads.append(get_ident())
        events.append("verify")

    verifier.observe = outside_transaction
    for name, listener in (("begin", begin), ("commit", end), ("before_cursor_execute", statement)):
        event.listen(postgres_engine, name, listener)
    try:
        identity = callback(harness, verifier, issued.state)
    finally:
        for name, listener in (("begin", begin), ("commit", end), ("before_cursor_execute", statement)):
            event.remove(postgres_engine, name, listener)
    assert identity is verifier.result and type(identity) is VerifiedExternalIdentity
    assert verifier.calls == [{
        "code": CODE, "state": issued.state, "expected_state": issued.state,
        "expected_nonce": issued.nonce, "code_verifier": verifier_value,
    }]
    assert events == ["begin", "commit", "verify", "verify"]
    assert len(worker_threads) == 1 and len(provider_threads) == 2
    assert all(worker_threads[0] != thread for thread in provider_threads)
    assert records(harness.factory) == {}
    for model in (User, AuthSession, StoredInterviewSession):
        assert records(harness.factory, model) == {}
    assert_failure(lambda: callback(harness, verifier, issued.state), OIDCFailureKind.INVALID_STATE, issued.state)
    assert len(verifier.calls) == 1


@pytest.mark.parametrize("kind", list(OIDCFailureKind))
def test_verifier_failure_is_sanitized_after_consumption_and_cannot_replay(harness, kind):
    issued, verifier = harness.store.create_login_transaction(), FakeVerifier()
    verifier.error = OIDCFailure(kind)
    verifier.error.args = (PRIVATE + CODE + issued.state + issued.nonce,)
    assert_failure(lambda: callback(harness, verifier, issued.state), kind, issued.state, issued.nonce)
    assert len(verifier.calls) == 1 and records(harness.factory) == {}
    assert_failure(lambda: callback(harness, verifier, issued.state), OIDCFailureKind.INVALID_STATE)
    assert len(verifier.calls) == 1


def test_unexpected_provider_failure_discards_secret_chain_and_does_not_restore_login_state(harness, caplog, capsys):
    issued, verifier = harness.store.create_login_transaction(), FakeVerifier()
    verifier.error = RuntimeError(PRIVATE + CODE + issued.state + issued.nonce)
    assert_failure(lambda: callback(harness, verifier, issued.state), OIDCFailureKind.UNAVAILABLE, issued.state, issued.nonce)
    assert len(verifier.calls) == 1 and records(harness.factory) == {}
    assert caplog.records == [] and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("metadata", ["missing-kind", "string-kind", "object-kind", "subclass"])
def test_malformed_failure_metadata_cannot_escape_the_closed_unavailable_mapping(harness, metadata):
    class DerivedFailure(OIDCFailure):
        @property
        def kind(self):
            raise RuntimeError(PRIVATE)

    issued, verifier = harness.store.create_login_transaction(), FakeVerifier()
    error = OIDCFailure(OIDCFailureKind.INVALID_TOKEN)
    if metadata == "missing-kind":
        del error._kind
    elif metadata == "string-kind":
        error._kind = "invalid_token"
    elif metadata == "object-kind":
        error._kind = object()
    else:
        error = DerivedFailure(OIDCFailureKind.INVALID_TOKEN)
    error.args = (PRIVATE + CODE + issued.state,)
    verifier.error = error
    assert_failure(lambda: callback(harness, verifier, issued.state), OIDCFailureKind.UNAVAILABLE, issued.state)
    assert len(verifier.calls) == 1 and records(harness.factory) == {}


@pytest.mark.parametrize("result", [None, {"issuer": "spoofed", "subject": "spoofed"}, object()])
def test_callback_requires_only_a_verified_identity_and_never_accepts_arbitrary_claims(harness, result):
    issued, verifier = harness.store.create_login_transaction(), FakeVerifier()
    verifier.result = result
    assert_failure(lambda: callback(harness, verifier, issued.state), OIDCFailureKind.INVALID_TOKEN)
    assert records(harness.factory) == {}


@pytest.mark.parametrize("phase", ["clock", "state-generator", "verifier"])
@pytest.mark.parametrize("error", [asyncio.CancelledError(PRIVATE), KeyboardInterrupt(), SystemExit()])
def test_cancellation_and_shutdown_propagate_without_normalization_or_retry(harness, phase, error):
    if phase == "verifier":
        issued, verifier = harness.store.create_login_transaction(), FakeVerifier()
        verifier.error = error
        operation = lambda: callback(harness, verifier, issued.state)
    else:
        (harness.clock if phase == "clock" else harness.states).error = error
        operation = harness.store.create_login_transaction
    with pytest.raises(type(error)) as caught:
        operation()
    assert caught.value is error
    assert records(harness.factory) == {}
    if phase == "verifier":
        assert len(verifier.calls) == 1


@pytest.mark.parametrize("value", ["invalid_state", None, object(), True])
def test_closed_failure_constructor_rejects_arbitrary_metadata(value):
    with pytest.raises(TypeError, match="approved failure kind"):
        OIDCFailure(value)
