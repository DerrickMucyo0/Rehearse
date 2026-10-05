import pytest
from sqlalchemy.engine import make_url

from app.database import (
    DatabaseConfigurationError, create_database_engine, create_session_factory,
    get_database_url, get_test_database_url,
)

APPLICATION = "postgresql+psycopg://local_dev:PRIVATE_SENTINEL@localhost/rehearse_dev"
TEST = "postgresql+psycopg://rehearse_test:PRIVATE_SENTINEL@localhost/rehearse_test"


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
