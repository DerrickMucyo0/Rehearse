from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import inspect
from threading import Barrier, Event

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.database import (
    DatabaseConfigurationError, create_database_engine, create_session_factory,
    get_database_url, get_test_database_url,
)

APPLICATION = "postgresql+psycopg://local_dev:PRIVATE_SENTINEL@localhost/rehearse_dev"
TEST = "postgresql+psycopg://rehearse_test:PRIVATE_SENTINEL@localhost/rehearse_test"


@pytest.fixture
def shared_factory_engines(monkeypatch):
    from app import database, session_routes

    session_routes.get_session_service.cache_clear()
    database.get_database_session_factory.cache_clear()
    engines = []
    actual_create_engine = database.create_database_engine

    def captured_engine(*args, **kwargs):
        engine = actual_create_engine(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(database, "create_database_engine", captured_engine)
    try:
        yield engines
    finally:
        session_routes.get_session_service.cache_clear()
        database.get_database_session_factory.cache_clear()
        for engine in engines:
            engine.dispose()


@pytest.mark.parametrize("environment", [{}, {"DATABASE_URL": ""}, {"DATABASE_URL": " "}])
def test_application_configuration_is_explicit(environment):
    with pytest.raises(DatabaseConfigurationError, match="must be explicitly configured"):
        get_database_url(environment)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_supported_postgres_urls_select_psycopg3(scheme):
    url = get_database_url({"DATABASE_URL": APPLICATION.replace("postgresql+psycopg", scheme)})
    assert url.drivername == "postgresql+psycopg"
    assert url.database == "rehearse_dev"
    assert "PRIVATE_SENTINEL" not in str(url)


@pytest.mark.parametrize("raw", [
    "PRIVATE_SENTINEL", "sqlite:///PRIVATE_SENTINEL",
    "postgresql+psycopg2://local:PRIVATE_SENTINEL@localhost/db",
    "postgresql://local:PRIVATE_SENTINEL@localhost:bad/db",
    "postgresql://local:PRIVATE_SENTINEL@localhost",
])
def test_configuration_errors_do_not_expose_urls_or_parser_details(raw, caplog):
    with pytest.raises(DatabaseConfigurationError) as caught:
        get_database_url({"DATABASE_URL": raw})
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    assert raw not in str(caught.value)
    assert not caplog.records


@pytest.mark.parametrize("name, base, reader", [
    ("DATABASE_URL", APPLICATION, get_database_url),
    ("TEST_DATABASE_URL", TEST, get_test_database_url),
])
@pytest.mark.parametrize("query", [
    "dbname=rehearse_test",
    "dbname=production",
    "db%6eame=rehearse_test",
    "dbname=rehearse_dev&dbname=rehearse_test",
    "dbname=",
    "dbname",
])
def test_database_name_query_parameters_are_rejected(name, base, reader, query, caplog):
    raw = base + "?" + query
    with pytest.raises(DatabaseConfigurationError) as caught:
        reader({name: raw})
    assert str(caught.value) == f"{name} must not contain a dbname query parameter."
    assert raw not in str(caught.value)
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    assert not caplog.records


def test_disguised_application_database_is_rejected_before_isolation_comparison():
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL must not contain a dbname"):
        get_test_database_url({
            "TEST_DATABASE_URL": TEST,
            "DATABASE_URL": APPLICATION + "?dbname=rehearse_test",
        })


@pytest.mark.parametrize("query, expected", [
    ("sslmode=require", {"sslmode": "require"}),
    (
        "sslmode=verify-full&sslrootcert=%2Ftmp%2Fca.pem",
        {"sslmode": "verify-full", "sslrootcert": "/tmp/ca.pem"},
    ),
])
def test_application_ssl_query_options_remain_supported(query, expected):
    environment = {"DATABASE_URL": APPLICATION + "?" + query, "TEST_DATABASE_URL": TEST}
    application = get_database_url(environment)
    assert application.database == "rehearse_dev"
    assert dict(application.query) == expected
    assert get_test_database_url(environment).database == "rehearse_test"


@pytest.mark.parametrize("query", ["sslmode=require", "sslrootcert=%2Ftmp%2Fca.pem"])
def test_test_database_ssl_query_options_remain_forbidden(query):
    with pytest.raises(DatabaseConfigurationError, match="must not contain connection overrides"):
        get_test_database_url({"TEST_DATABASE_URL": TEST + "?" + query})


def test_test_configuration_never_defaults_to_application_url():
    with pytest.raises(DatabaseConfigurationError, match="TEST_DATABASE_URL must be explicitly configured"):
        get_test_database_url({"DATABASE_URL": APPLICATION})


@pytest.mark.parametrize("raw", [
    APPLICATION,
    TEST.replace("//rehearse_test:", "//local_dev:"),
    TEST.replace("/rehearse_test", "/production"),
    TEST + "?dbname=production", TEST + "?host=elsewhere",
])
def test_destructive_tests_reject_nonisolated_targets(raw):
    with pytest.raises(DatabaseConfigurationError):
        get_test_database_url({"TEST_DATABASE_URL": raw})


def test_same_database_rejected_even_with_other_user_or_host_alias():
    with pytest.raises(DatabaseConfigurationError, match="must be distinct"):
        get_test_database_url({
            "TEST_DATABASE_URL": TEST,
            "DATABASE_URL": "postgresql://local_dev@127.0.0.1/rehearse_test",
        })


def test_explicit_isolated_database_and_role_are_accepted():
    url = get_test_database_url({"DATABASE_URL": APPLICATION, "TEST_DATABASE_URL": TEST})
    assert url.database == url.username == "rehearse_test"


def test_engine_and_session_factory_are_lazy_and_hide_parameters(monkeypatch):
    import psycopg

    def blocked(*args, **kwargs):
        raise AssertionError("Database connections are forbidden in this offline test")

    monkeypatch.setattr(psycopg, "connect", blocked)
    engine = create_database_engine(get_database_url({"DATABASE_URL": APPLICATION}))
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            assert session.bind is engine
        assert engine.echo is False
        assert engine.hide_parameters is True
    finally:
        engine.dispose()


def test_engine_does_not_accept_sqlite_or_unvalidated_string():
    for target in (make_url("sqlite://"), APPLICATION):
        with pytest.raises(DatabaseConfigurationError):
            create_database_engine(target)


@pytest.mark.parametrize("raw", [
    None, "", " ", "PRIVATE_SENTINEL", "sqlite:///PRIVATE_SENTINEL",
    APPLICATION + "?dbname=rehearse_test",
])
def test_shared_factory_preserves_configuration_errors_without_test_url_fallback(
    monkeypatch, shared_factory_engines, raw,
):
    from app import database

    monkeypatch.setenv("TEST_DATABASE_URL", TEST)
    if raw is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", raw)

    def forbidden_factory(*args, **kwargs):
        raise AssertionError("Invalid application configuration must not create a session factory.")

    monkeypatch.setattr(database, "create_session_factory", forbidden_factory)
    with pytest.raises(DatabaseConfigurationError) as caught:
        database.get_database_session_factory()
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    if raw is None or not raw.strip():
        assert str(caught.value) == "DATABASE_URL must be explicitly configured."
    assert shared_factory_engines == []
    assert database.get_database_session_factory.cache_info().currsize == 0


def test_both_consumers_share_one_lazy_factory_without_request_or_session_state(
    monkeypatch, shared_factory_engines,
):
    import psycopg
    from app import auth_http, database, session_routes

    monkeypatch.setenv("DATABASE_URL", APPLICATION)
    monkeypatch.setenv("TEST_DATABASE_URL", TEST)
    factory_calls, service_calls, store_calls = [], [], []
    actual_create_factory = database.create_session_factory

    def forbidden(*args, **kwargs):
        raise AssertionError("Factory composition must not open connections or construct ORM sessions.")

    def captured_factory(engine):
        factory = actual_create_factory(engine)
        factory_calls.append((engine, factory))
        return factory

    def interview_service(factory):
        service = object()
        service_calls.append((factory, service))
        return service

    def authentication_store(factory, *, session_lifetime):
        store = object()
        store_calls.append((factory, session_lifetime, store))
        return store

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(Session, "__init__", forbidden)
    monkeypatch.setattr(database, "create_session_factory", captured_factory)
    monkeypatch.setattr(session_routes, "InterviewSessionService", interview_service)
    monkeypatch.setattr(auth_http, "PostgreSQLAuthSessionStore", authentication_store)
    assert shared_factory_engines == factory_calls == []
    helper = database.get_database_session_factory
    assert session_routes.get_database_session_factory is auth_http.get_database_session_factory is helper
    assert tuple(inspect.signature(helper).parameters) == ()
    assert helper.cache_info().currsize == 0

    service = session_routes.get_session_service()
    stores = []
    for credential in ("first-login-credential", "replacement-login-credential"):
        request = Request({
            "type": "http", "method": "GET", "path": "/future-private",
            "headers": [(b"cookie", f"{auth_http.AUTH_SESSION_COOKIE_NAME}={credential}".encode("ascii"))],
        })
        stores.append(auth_http.get_auth_session_store(request))
    factory = helper()
    assert isinstance(factory, sessionmaker) and not isinstance(factory, Session)
    assert len(shared_factory_engines) == len(factory_calls) == len(service_calls) == 1
    engine = shared_factory_engines[0]
    assert factory_calls == [(engine, factory)]
    assert service_calls == [(factory, service)]
    assert store_calls == [(factory, timedelta(hours=8), store) for store in stores]
    assert engine.url == get_database_url({"DATABASE_URL": APPLICATION})
    assert engine.echo is False and engine.hide_parameters is True
    assert engine.pool.checkedout() == 0
    assert session_routes.get_session_service() is service
    monkeypatch.setenv("DATABASE_URL", TEST)
    assert helper() is factory
    assert len(shared_factory_engines) == len(factory_calls) == 1
    assert helper.cache_info().maxsize == helper.cache_info().currsize == 1


def test_concurrent_initial_factory_calls_construct_one_engine_and_sessionmaker(
    monkeypatch, shared_factory_engines,
):
    import psycopg
    from app import database

    monkeypatch.setenv("DATABASE_URL", APPLICATION)
    actual_create_engine = database.create_database_engine
    actual_create_factory = database.create_session_factory
    creator_calls, factory_calls = [], []
    callers = 4
    start = Barrier(callers)
    requested = [Event() for _ in range(callers)]
    creator_started, release_creator = Event(), Event()

    def forbidden(*args, **kwargs):
        raise AssertionError("Concurrent configuration must not connect or construct ORM sessions.")

    def gated_engine():
        creator_calls.append(object())
        creator_started.set()
        assert release_creator.wait(5), "Test did not release lazy engine creation."
        return actual_create_engine()

    def captured_factory(engine):
        factory = actual_create_factory(engine)
        factory_calls.append(factory)
        return factory

    def configure(index):
        start.wait(timeout=5)
        requested[index].set()
        return database.get_database_session_factory()

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(Session, "__init__", forbidden)
    monkeypatch.setattr(database, "create_database_engine", gated_engine)
    monkeypatch.setattr(database, "create_session_factory", captured_factory)
    with ThreadPoolExecutor(max_workers=callers) as executor:
        futures = [executor.submit(configure, index) for index in range(callers)]
        try:
            assert all(caller.wait(5) for caller in requested)
            assert creator_started.wait(5)
            assert all(not future.done() for future in futures)
        finally:
            release_creator.set()
        factories = [future.result(timeout=5) for future in futures]
    assert len(creator_calls) == len(shared_factory_engines) == len(factory_calls) == 1
    assert all(factory is factories[0] for factory in factories)
    assert isinstance(factories[0], sessionmaker)
    assert database.get_database_session_factory() is factories[0]
    assert database.get_database_session_factory.cache_info().currsize == 1
