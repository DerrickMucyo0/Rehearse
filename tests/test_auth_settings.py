"""Offline authentication configuration and secret-safety contracts."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import timedelta
import importlib.util
import sys

import pytest

from app.auth import AuthenticationFailure, AuthenticationFailureKind
import app.auth_settings as settings_module
from app.auth_settings import (
    AUTH_SESSION_MAX_TTL_SECONDS, AUTH_SESSION_MIN_TTL_SECONDS,
    AuthSettings, load_auth_settings,
)
from app.oidc_verifier import OIDCConfiguration


@pytest.fixture
def configuration():
    return {
        "AUTH_OIDC_ISSUER": "https://identity.example/realm",
        "AUTH_OIDC_CLIENT_ID": "rehearse-test-client",
        "AUTH_OIDC_REDIRECT_URI": "https://rehearse.example/api/auth/callback",
        "AUTH_APP_ORIGIN": "https://rehearse.example",
        "AUTH_SESSION_TTL_SECONDS": "3600",
    }


def assert_unavailable(configuration):
    with pytest.raises(AuthenticationFailure) as caught:
        load_auth_settings(configuration)
    assert caught.value.kind is AuthenticationFailureKind.UNAVAILABLE
    assert str(caught.value) == "Authentication is temporarily unavailable."
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_explicit_provider_neutral_settings(configuration):
    settings = load_auth_settings(configuration)
    assert settings.oidc.issuer == configuration["AUTH_OIDC_ISSUER"]
    assert settings.oidc.client_id == configuration["AUTH_OIDC_CLIENT_ID"]
    assert settings.oidc.redirect_uri == configuration["AUTH_OIDC_REDIRECT_URI"]
    assert settings.oidc.client_secret is None
    assert settings.oidc.allowed_algorithms == ("RS256",)
    assert settings.app_origin == configuration["AUTH_APP_ORIGIN"]
    assert settings.session_ttl_seconds == 3600
    assert settings.session_lifetime == timedelta(hours=1)


@pytest.mark.parametrize("name", [
    "AUTH_OIDC_ISSUER", "AUTH_OIDC_CLIENT_ID", "AUTH_OIDC_REDIRECT_URI",
    "AUTH_APP_ORIGIN", "AUTH_SESSION_TTL_SECONDS",
])
def test_each_required_setting_is_explicit(configuration, name):
    del configuration[name]
    assert_unavailable(configuration)


@pytest.mark.parametrize("name", [
    "AUTH_OIDC_ISSUER", "AUTH_OIDC_CLIENT_ID", "AUTH_OIDC_REDIRECT_URI",
    "AUTH_APP_ORIGIN", "AUTH_SESSION_TTL_SECONDS", "AUTH_OIDC_CLIENT_SECRET",
])
@pytest.mark.parametrize("value", ["", " ", "\x00"])
def test_blank_or_invalid_settings_fail_closed(configuration, name, value):
    configuration[name] = value
    assert_unavailable(configuration)


@pytest.mark.parametrize("value", [
    "0", "-1", "59", "86401", "3600.0", "true", "+3600", " 3600", "3600 ",
    "3_600", "٣٦٠٠", "NaN", 3600, True, None,
])
def test_runtime_ttl_is_strict_bounded_decimal_seconds(configuration, value):
    configuration["AUTH_SESSION_TTL_SECONDS"] = value
    assert_unavailable(configuration)


@pytest.mark.parametrize("seconds", [60, 86400])
def test_session_ttl_inclusive_bounds(configuration, seconds):
    configuration["AUTH_SESSION_TTL_SECONDS"] = str(seconds)
    settings = load_auth_settings(configuration)
    assert settings.session_lifetime == timedelta(seconds=seconds)
    assert AUTH_SESSION_MIN_TTL_SECONDS == 60
    assert AUTH_SESSION_MAX_TTL_SECONDS == 86400


@pytest.mark.parametrize("value", [True, False, 3600.0, "3600", None])
def test_direct_settings_require_strict_integer(configuration, value):
    loaded = load_auth_settings(configuration)
    with pytest.raises(TypeError, match="^Authentication session lifetime must be an integer\\.$"):
        AuthSettings(loaded.oidc, loaded.app_origin, value)


@pytest.mark.parametrize("value", [-1, 0, 59, 86401])
def test_direct_settings_require_bounded_integer(configuration, value):
    loaded = load_auth_settings(configuration)
    with pytest.raises(ValueError, match="^Authentication session lifetime is outside the supported bounds\\.$"):
        AuthSettings(loaded.oidc, loaded.app_origin, value)


@pytest.mark.parametrize("value", [
    "http://rehearse.example", "http://localhost", "http://127.0.0.1", "http://[::1]",
    "https://user:password@rehearse.example", "https://rehearse.example/path",
    "https://rehearse.example/?secret=marker", "https://rehearse.example?", "https://rehearse.example#",
    "https://rehearse.example/#fragment", "https://", "//rehearse.example", "https://rehearse.example:65536",
    "https://rehearse.example:0", "https://rehearse.example:port", "https://[invalid]",
    " https://rehearse.example", "https://rehearse.example ", "https://rehearse. example",
    "https://rehearse.example\n", "https://rehearse.example\x00", "https://rehearse.example\x7f",
    "https://rehearse.example\\other", "https://rehearse.example\u00a0", None, 12,
])
def test_application_origin_is_exact_trusted_https_root(configuration, value):
    configuration["AUTH_APP_ORIGIN"] = value
    assert_unavailable(configuration)


@pytest.mark.parametrize("value", [
    "https://rehearse.example", "https://rehearse.example/", "https://rehearse.example:8443",
    "https://localhost:8443", "https://[::1]:8443",
])
def test_valid_application_origin_is_preserved(configuration, value):
    configuration["AUTH_APP_ORIGIN"] = value
    configuration["AUTH_OIDC_REDIRECT_URI"] = value.rstrip("/") + "/api/auth/callback"
    assert load_auth_settings(configuration).app_origin == value


@pytest.mark.parametrize("value", [
    "https://rehearse.example", "https://rehearse.example/other", "https://rehearse.example/api/auth/callback/",
    "http://rehearse.example/api/auth/callback", "http://localhost/api/auth/callback",
    "https://rehearse.example/api/auth/callback?", "https://rehearse.example/api/auth/callback#",
    "https://rehearse.example/api/auth/callback?secret=marker",
    "https://user:password@rehearse.example/api/auth/callback", "https://rehearse.example:65536/api/auth/callback",
    "https://rehearse.example/api/auth/call\nback", " https://rehearse.example/api/auth/callback",
])
def test_redirect_uri_requires_the_fixed_https_callback_path(configuration, value):
    configuration["AUTH_OIDC_REDIRECT_URI"] = value
    assert_unavailable(configuration)


@pytest.mark.parametrize("value", [
    " http" , " https://identity.example", "https://identity.example\n", "https://identity. example",
    "https://identity.example:65536", "https://identity.example?", "https://identity.example#",
])
def test_issuer_url_is_trusted_and_exact(configuration, value):
    configuration["AUTH_OIDC_ISSUER"] = value
    assert_unavailable(configuration)


def test_secret_never_appears_in_settings_repr_or_sanitized_failure(configuration, caplog):
    marker = "synthetic-secret-unique-marker"
    configuration["AUTH_OIDC_CLIENT_SECRET"] = marker
    settings = load_auth_settings(configuration)
    assert settings.oidc.client_secret == marker
    assert marker not in repr(settings)
    configuration["AUTH_APP_ORIGIN"] = "https://invalid.example/" + marker
    with pytest.raises(AuthenticationFailure) as caught:
        load_auth_settings(configuration)
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert marker not in caplog.text
    assert caught.value.__cause__ is caught.value.__context__ is None


def test_settings_are_immutable(configuration):
    settings = load_auth_settings(configuration)
    with pytest.raises(FrozenInstanceError):
        settings.app_origin = "https://other.example"
    with pytest.raises(FrozenInstanceError):
        settings.oidc.client_id = "other-client"


def test_injected_oidc_signing_allowlist_is_reused(configuration):
    settings = load_auth_settings(configuration)
    oidc = OIDCConfiguration(
        settings.oidc.issuer, settings.oidc.client_id, settings.oidc.redirect_uri,
        allowed_algorithms=("ES256", "RS256"),
    )
    assert AuthSettings(oidc, settings.app_origin, 3600).oidc is oidc
    assert oidc.allowed_algorithms == ("ES256", "RS256")


def test_settings_do_not_cache_environment_or_principal(configuration, monkeypatch):
    for name, value in configuration.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AUTH_OIDC_CLIENT_SECRET", raising=False)
    first = load_auth_settings()
    monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "7200")
    second = load_auth_settings()
    assert first.session_ttl_seconds == 3600
    assert second.session_ttl_seconds == 7200
    assert first is not second
    assert set(AuthSettings.__slots__) == {"oidc", "app_origin", "session_ttl_seconds"}


def test_import_performs_no_configuration_read(monkeypatch):
    class NoEnvironment(Mapping):
        def __getitem__(self, key):
            raise AssertionError("Environment must not be read at import.")

        def __iter__(self):
            raise AssertionError("Environment must not be read at import.")

        def __len__(self):
            raise AssertionError("Environment must not be read at import.")

    with monkeypatch.context() as context:
        context.setattr(settings_module.os, "environ", NoEnvironment())
        spec = importlib.util.spec_from_file_location("_auth_settings_import_probe", settings_module.__file__)
        module = importlib.util.module_from_spec(spec)
        context.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    assert module.AUTH_SESSION_MIN_TTL_SECONDS == 60


def test_exception_text_and_chain_are_discarded():
    class BrokenMapping(Mapping):
        def __getitem__(self, key):
            raise RuntimeError("synthetic-secret-marker")

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

    assert_unavailable(BrokenMapping())


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit()])
def test_shutdown_exceptions_propagate(error):
    class InterruptedMapping(dict):
        def __getitem__(self, key):
            raise error

    with pytest.raises(type(error)):
        load_auth_settings(InterruptedMapping())


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_explicit_matching_loopback_http_origins_allow_port_differences(configuration, host):
    configuration["AUTH_APP_ORIGIN"] = f"http://{host}:5173"
    configuration["AUTH_OIDC_REDIRECT_URI"] = f"http://{host}:8000/api/auth/callback"
    settings = load_auth_settings(configuration)
    assert settings.app_origin == configuration["AUTH_APP_ORIGIN"]
    assert settings.oidc.redirect_uri == configuration["AUTH_OIDC_REDIRECT_URI"]
    assert settings.secure_cookies is False
    assert settings.oidc.issuer == configuration["AUTH_OIDC_ISSUER"]


def test_https_deployment_retains_secure_cookie_policy(configuration):
    assert load_auth_settings(configuration).secure_cookies is True


@pytest.mark.parametrize("origin,callback", [
    ("http://localhost:5173", "http://127.0.0.1:8000"),
    ("http://127.0.0.1:5173", "http://localhost:8000"),
    ("http://[::1]:5173", "http://localhost:8000"),
    ("http://localhost:5173", "https://localhost:8000"),
    ("https://localhost:5173", "http://localhost:8000"),
])
def test_browser_origin_and_callback_require_matching_scheme_and_host(configuration, origin, callback):
    configuration["AUTH_APP_ORIGIN"] = origin
    configuration["AUTH_OIDC_REDIRECT_URI"] = callback + "/api/auth/callback"
    assert_unavailable(configuration)


def test_existing_https_application_callback_configuration_policy_is_preserved(configuration):
    configuration["AUTH_APP_ORIGIN"] = "https://app.example"
    configuration["AUTH_OIDC_REDIRECT_URI"] = "https://callback.example/api/auth/callback"
    settings = load_auth_settings(configuration)
    assert settings.app_origin == configuration["AUTH_APP_ORIGIN"]
    assert settings.oidc.redirect_uri == configuration["AUTH_OIDC_REDIRECT_URI"]
    assert settings.secure_cookies is True


@pytest.mark.parametrize("host", [
    "example.com", "localhost.example.com", "example.localhost", "0.0.0.0", "192.168.1.1",
    "10.1.2.3", "172.16.0.1", "8.8.8.8", "*", "[::]", "[::ffff:127.0.0.1]",
    "127.1", "2130706433", "localhost.",
    "[::1]suffix", "[::1].example",
])
def test_http_requires_exact_approved_loopback_targets(configuration, host):
    configuration["AUTH_APP_ORIGIN"] = f"http://{host}:5173"
    configuration["AUTH_OIDC_REDIRECT_URI"] = f"http://{host}:8000/api/auth/callback"
    assert_unavailable(configuration)


@pytest.mark.parametrize("suffix", [":", ":0", ":65536", ":wrong", " ", "\\other", "\n", "\x00"])
def test_malformed_loopback_origin_is_rejected(configuration, suffix):
    configuration["AUTH_APP_ORIGIN"] = "http://localhost" + suffix
    configuration["AUTH_OIDC_REDIRECT_URI"] = "http://localhost:8000/api/auth/callback"
    assert_unavailable(configuration)


def test_insecure_cookie_environment_flags_cannot_override_https_policy(configuration):
    configuration.update({"AUTH_DISABLE_SECURE_COOKIES": "true", "DEBUG_INSECURE_COOKIES": "true"})
    assert load_auth_settings(configuration).secure_cookies is True
