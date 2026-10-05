"""Real PostgreSQL fixtures shared by schema and database-backed API tests."""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.database import create_database_engine, create_session_factory, get_test_database_url
from app.database_models import StoredInterviewSession

ROOT = Path(__file__).resolve().parents[1]
TABLES = {"interview_sessions", "question_attempts", "transcription_measurements"}


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
