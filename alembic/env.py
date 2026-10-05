"""Explicit migrations only; no URL is stored in alembic.ini or logged here."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy.exc import SQLAlchemyError

from app.database import DatabaseConfigurationError, create_database_engine, get_database_url
from app.database_models import Base

config = context.config
if config.config_file_name and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def configure(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    # Tests inject a connection validated through TEST_DATABASE_URL; they never
    # substitute DATABASE_URL or modify application database configuration.
    connection = config.attributes.get("connection")
    if connection is not None:
        configure(connection)
        return
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            configure(connection)
    except SQLAlchemyError:
        raise DatabaseConfigurationError("Database migration failed; connection details are omitted.") from None
    finally:
        engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=get_database_url(), target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
