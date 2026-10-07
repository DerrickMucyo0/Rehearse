"""Adaptive-only transaction guard uses the existing local auth session store."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.auth import AuthenticationFailure, AuthenticationFailureKind, VerifiedExternalIdentity
from app.auth_http import revalidate_authenticated_principal_in_transaction
from app.auth_persistence import PostgreSQLAuthSessionStore


@pytest.fixture
def login(postgres_session_factory):
    clock = [datetime.now(timezone.utc)]
    store = PostgreSQLAuthSessionStore(
        postgres_session_factory, session_lifetime=timedelta(hours=1), clock=lambda: clock[0],
    )
    issued = store.create(user_id=store.provision_user(identity=VerifiedExternalIdentity(
        issuer="https://adaptive-auth.example.test", subject=str(uuid4()),
    )))
    return store, issued.principal, clock


def test_guard_reuses_supplied_active_transaction_and_returns_no_cached_principal(login, postgres_session_factory):
    store, principal, _ = login
    statements = []
    with postgres_session_factory.begin() as database:
        connection = database.connection()

        def observe(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(connection, "before_cursor_execute", observe)
        assert store.revalidate_in_transaction(database=database, principal=principal) is None
        assert database.in_transaction()
        assert len(statements) == 1
        assert "FOR SHARE OF auth_sessions" in statements[0]
        assert database.connection() is connection
    assert not database.in_transaction()


@pytest.mark.parametrize("changes,kind", [
    ({"user_id": uuid4()}, AuthenticationFailureKind.UNAUTHENTICATED),
    ({"auth_session_id": uuid4()}, AuthenticationFailureKind.UNAUTHENTICATED),
    ({"request_context": "different-context"}, AuthenticationFailureKind.INVALID_REQUEST_CONTEXT),
])
def test_transaction_guard_checks_exact_user_session_and_context(login, postgres_session_factory, changes, kind):
    store, principal, _ = login
    with postgres_session_factory.begin() as database:
        with pytest.raises(AuthenticationFailure) as caught:
            store.revalidate_in_transaction(database=database, principal=replace(principal, **changes))
        assert caught.value.kind is kind


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_transaction_guard_rechecks_liveness_each_time(login, postgres_session_factory, state):
    store, principal, clock = login
    with postgres_session_factory.begin() as database:
        store.revalidate_in_transaction(database=database, principal=principal)
    if state == "revoked":
        store.revoke(auth_session_id=principal.auth_session_id)
    else:
        clock[0] += timedelta(hours=1)
    with postgres_session_factory.begin() as database:
        with pytest.raises(AuthenticationFailure) as caught:
            store.revalidate_in_transaction(database=database, principal=principal)
        assert caught.value.kind is AuthenticationFailureKind.UNAUTHENTICATED


def test_guard_requires_active_caller_transaction(login, postgres_session_factory):
    store, principal, _ = login
    with postgres_session_factory() as database:
        assert not database.in_transaction()
        with pytest.raises(AuthenticationFailure) as caught:
            store.revalidate_in_transaction(database=database, principal=principal)
        assert caught.value.kind is AuthenticationFailureKind.UNAVAILABLE
        assert not database.in_transaction()


@pytest.mark.parametrize("kind,status,message", [
    (AuthenticationFailureKind.UNAUTHENTICATED, 401, "Authentication required."),
    (AuthenticationFailureKind.INVALID_REQUEST_CONTEXT, 403, "Invalid authentication request context."),
    (AuthenticationFailureKind.UNAVAILABLE, 503, "Authentication is temporarily unavailable."),
])
def test_adaptive_guard_preserves_fixed_auth_failure_mapping(kind, status, message):
    class Store:
        def revalidate_in_transaction(self, **kwargs):
            raise AuthenticationFailure(kind)

    from app.auth import AuthenticatedPrincipal
    principal = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context="context")
    with pytest.raises(HTTPException) as caught:
        revalidate_authenticated_principal_in_transaction(principal, Store(), object())
    assert caught.value.status_code == status
    assert caught.value.detail == message
    assert caught.value.headers == {"Cache-Control": "no-store"}


@pytest.mark.parametrize("mode", ["absent", "not_callable", "unexpected", "wrong_return"])
def test_missing_or_malformed_transaction_guard_fails_closed_without_private_details(mode):
    class Store:
        def revalidate_in_transaction(self, **kwargs):
            if mode == "unexpected":
                raise RuntimeError("PRIVATE_STORE_ERROR")
            return "PRIVATE_STORE_VALUE"

    store = Store()
    if mode == "absent":
        store = object()
    elif mode == "not_callable":
        store.revalidate_in_transaction = "PRIVATE_STORE_VALUE"
    from app.auth import AuthenticatedPrincipal
    principal = AuthenticatedPrincipal(user_id=uuid4(), auth_session_id=uuid4(), request_context="context")
    with pytest.raises(HTTPException) as caught:
        revalidate_authenticated_principal_in_transaction(principal, store, object())
    assert caught.value.status_code == 503
    assert caught.value.detail == "Authentication is temporarily unavailable."
    assert caught.value.__cause__ is None
    assert "PRIVATE_STORE" not in str(caught.value)
