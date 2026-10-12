from __future__ import annotations

import re

import pytest
from flask import Flask
from solarbank_dashboard.auth import AuthSettings, configure_auth
from solarbank_dashboard.repository import ApiError


def app():
    application = Flask("solarbank_dashboard.app")
    application.testing = True
    application.add_url_rule("/", view_func=lambda: "dashboard")
    application.add_url_rule(
        "/healthz", endpoint="health", view_func=lambda: {"status": "ok"}
    )
    configure_auth(application, AuthSettings(secret_key="s" * 32))
    return application


def csrf(response):
    return (
        re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', response.data)
        .group(1)
        .decode()
    )


def test_login_delegates_credentials_and_keeps_token_out_of_cookie(monkeypatch):
    captured = {}

    def authenticate(_settings, path, *, payload):
        if path == "/v1/auth/session":
            return {"id": "admin", "username": "admin"}
        captured.update(payload)
        assert path == "/v1/auth/login"
        return {"id": "admin", "username": "admin"}

    monkeypatch.setattr("solarbank_dashboard.auth.request_api", authenticate)
    client = app().test_client()
    assert client.get("/").status_code == 302
    assert client.get("/healthz").status_code == 200
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf(client.get("/login")),
            "username": "admin",
            "password": "my-test-password",
        },
    )
    assert response.status_code == 302
    assert captured["username"] == "admin"
    assert len(captured["client_key"]) == 64
    assert "my-test-password" not in response.headers.get("Set-Cookie", "")
    assert client.get("/").status_code == 200
    assert client.post("/logout").status_code == 400
    assert (
        client.post(
            "/logout", data={"csrf_token": csrf(client.get("/logout"))}
        ).status_code
        == 302
    )
    assert client.get("/").status_code == 302


@pytest.mark.parametrize("api_status, expected", [(401, 200), (429, 429), (503, 503)])
def test_api_authentication_errors_are_handled(monkeypatch, api_status, expected):
    def failed(*args, **kwargs):
        raise ApiError(api_status, 123)

    monkeypatch.setattr("solarbank_dashboard.auth.request_api", failed)
    client = app().test_client()
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf(client.get("/login")),
            "username": "admin",
            "password": "wrong",
        },
    )
    assert response.status_code == expected
    if api_status == 429:
        assert response.headers["Retry-After"] == "123"
    assert client.get("/").status_code == 302


def test_csrf_failure_never_calls_api(monkeypatch):
    def failed(*args, **kwargs):
        pytest.fail("CSRF failure called API")

    monkeypatch.setattr("solarbank_dashboard.auth.request_api", failed)
    assert (
        app()
        .test_client()
        .post("/login", data={"username": "admin", "password": "wrong"})
        .status_code
        == 400
    )


def test_common_env_configuration(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("CANONICAL_ORIGIN", "https://kraftwerk.fluxo.io")
    monkeypatch.setenv("TRUST_PROXY", "true")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    settings = AuthSettings.from_environment()
    assert settings.trusted_proxy_count == 1
    assert settings.secure_cookie
    assert settings.session_hours == 8


def test_saved_session_is_revalidated_and_fails_closed(monkeypatch):
    status = None

    def authenticate(settings, path, *, payload):
        if status is not None:
            raise ApiError(status)
        return {"id": "opaque-fingerprint", "username": "admin"}

    monkeypatch.setattr("solarbank_dashboard.auth.request_api", authenticate)
    client = app().test_client()
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf(client.get("/login")),
            "username": "admin",
            "password": "my-test-password",
        },
    )
    assert response.status_code == 302
    assert client.get("/").status_code == 200
    status = 503
    assert client.get("/").status_code == 503
    status = 401
    assert client.get("/").status_code == 302
