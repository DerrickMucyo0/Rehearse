"""Real PostgreSQL fixtures shared by schema and database-backed API tests."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.auth import AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind
from app.auth_http import (
    AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME, AuthenticatedPrincipalDependency,
    get_auth_session_store,
)
from app.database import create_database_engine, create_session_factory, get_test_database_url
from app.database_models import AuthSession, StoredInterviewSession, User

ROOT = Path(__file__).resolve().parents[1]
TABLES = {
    "interview_sessions", "question_attempts", "transcription_measurements",
    "users", "auth_sessions",
}


def migration_config(connection):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes.update(connection=connection, skip_logging=True)
    return config


@pytest.fixture(scope="session")
def postgres_engine():
    if not os.environ.get("TEST_DATABASE_URL", "").strip():
        if os.environ.get("REHEARSE_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("PostgreSQL tests require explicit TEST_DATABASE_URL; they cannot be skipped.", pytrace=False)
        pytest.skip("BLOCKED_BY_LOCAL_DB_ENV: explicit isolated PostgreSQL TEST_DATABASE_URL is required")
    target = get_test_database_url()
    engine = create_database_engine(target)
    try:
        try:
            with engine.connect() as connection:
                actual = connection.execute(text("SELECT current_database(), current_user")).one()
                if tuple(actual) != ("rehearse_test", "rehearse_test"):
                    pytest.fail("Connected target is not the dedicated test database and role; nothing removed.", pytrace=False)
        except SQLAlchemyError:
            pytest.fail("Configured PostgreSQL test connection failed; details omitted.", pytrace=False)
        with engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if tables - TABLES - {"alembic_version"}:
                pytest.fail("Test database contains unexpected tables; nothing removed.", pytrace=False)
            if "alembic_version" in tables:
                command.downgrade(migration_config(connection), "base")
            elif tables:
                pytest.fail("Test database contains unversioned tables; nothing removed.", pytrace=False)
            command.upgrade(migration_config(connection), "head")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def postgres_session_factory(postgres_engine):
    """Service operations commit on independent connections, including threads.

    Clean only the dedicated test schema after each test. No outer transaction
    hides rows from concurrent connections, and no application URL is substituted.
    """
    try:
        yield create_session_factory(postgres_engine)
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(delete(StoredInterviewSession))
            # User FKs are restrictive: remove their children before test users.
            connection.execute(delete(AuthSession))
            connection.execute(delete(User))


@pytest.fixture
def authenticated_principal(postgres_session_factory):
    """Direct-service tests use a real persisted owner, never a magic FK bypass.

    HTTP authentication tests use real auth-store issuance or an explicit fake
    store boundary. This fixture attests identity only for trusted service tests.
    """
    with postgres_session_factory.begin() as database:
        user = User(auth_provider="https://identity.example.test", provider_subject=str(uuid4()))
        database.add(user)
        database.flush()
        user_id = user.id
    return AuthenticatedPrincipal(
        user_id=user_id, auth_session_id=uuid4(), request_context="direct-service-test-context",
    )


@pytest.fixture
def authenticated_http_headers(authenticated_principal):
    return {
        "Cookie": f"{AUTH_SESSION_COOKIE_NAME}=synthetic-service-test-credential",
        AUTH_REQUEST_CONTEXT_HEADER: authenticated_principal.request_context,
    }


@pytest.fixture
def authenticated_session_override(authenticated_principal, monkeypatch):
    """Keep injected service regressions behind the real HTTP auth boundary.

    Only the auth store is fake; the cookie/context checks and immutable owner
    binding remain real. Ownership matrix tests separately use persisted auth
    sessions and the default service dependency without these overrides.
    """
    from app.main import app

    class Store:
        def resolve(self, *, credential):
            if credential != "synthetic-service-test-credential":
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            return authenticated_principal

    monkeypatch.setitem(app.dependency_overrides, get_auth_session_store, lambda: Store())

    def bind(service):
        def override(principal: AuthenticatedPrincipalDependency):
            assert principal == authenticated_principal
            return service
        return override

    return bind
