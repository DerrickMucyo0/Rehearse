"""Synchronous database configuration; importing it never opens a connection.

Session routes resolve DATABASE_URL explicitly on first use. No dotenv loading,
runtime storage fallback, or automatic migrations are introduced here.
"""
import os
from collections.abc import Mapping

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker


class DatabaseConfigurationError(ValueError):
    """Fixed messages only: never include URL values or parser exception text."""


def _read_url(name: str, environment: Mapping[str, str]) -> URL:
    raw = environment.get(name, "").strip()
    if not raw:
        raise DatabaseConfigurationError(f"{name} must be explicitly configured.")
    try:
        url = make_url(raw)
        # Accessing port also validates a malformed numeric port.
        _ = url.port
    except (ArgumentError, ValueError, TypeError):
        raise DatabaseConfigurationError(f"{name} is not a valid PostgreSQL URL.") from None
    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise DatabaseConfigurationError(f"{name} must use PostgreSQL with psycopg 3.")
    if not url.database:
        raise DatabaseConfigurationError(f"{name} must name a database.")
    return url.set(drivername="postgresql+psycopg")


def get_database_url(environment: Mapping[str, str] | None = None) -> URL:
    return _read_url("DATABASE_URL", os.environ if environment is None else environment)


def get_test_database_url(environment: Mapping[str, str] | None = None) -> URL:
    """Destructive tests require the dedicated rehearse_test database AND role.

    Never fall back to DATABASE_URL. Also reject an application database with the
    same name, even if host aliases or different users disguise the same target.
    """
    values = os.environ if environment is None else environment
    url = _read_url("TEST_DATABASE_URL", values)
    if url.query:
        raise DatabaseConfigurationError("TEST_DATABASE_URL must not contain connection overrides.")
    if url.database != "rehearse_test" or url.username != "rehearse_test":
        raise DatabaseConfigurationError(
            "TEST_DATABASE_URL requires the dedicated rehearse_test database and role."
        )
    if values.get("DATABASE_URL", "").strip():
        application = _read_url("DATABASE_URL", values)
        if application.database == url.database:
            raise DatabaseConfigurationError("Test and application databases must be distinct.")
    return url


def create_database_engine(url: URL | None = None) -> Engine:
    """Lazy engine: no SQL echo and no bound parameter values in errors."""
    target = get_database_url() if url is None else url
    if not isinstance(target, URL) or target.drivername != "postgresql+psycopg":
        raise DatabaseConfigurationError("Database engines require a validated psycopg 3 PostgreSQL URL.")
    return create_engine(
        target,
        echo=False, hide_parameters=True, pool_pre_ping=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a factory, never a shared ORM session; callers own transactions."""
    return sessionmaker(bind=engine)
