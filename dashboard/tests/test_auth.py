from __future__ import annotations

import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import pytest
from flask import Flask, jsonify, request
from werkzeug.security import generate_password_hash

from solarbank_dashboard.auth import (
    AuthSettings,
    SQLiteLoginRateLimiter,
    configure_auth,
    credential_lines,
    main,
)
from solarbank_dashboard.config import ConfigurationError
from solarbank_dashboard.public_pages import register_public_pages

AUTH_ENVIRONMENT = (
    "DASHBOARD_AUTH_ENABLED",
    "DASHBOARD_AUTH_USERNAME",
    "DASHBOARD_AUTH_PASSWORD_HASH",
    "DASHBOARD_SECRET_KEY",
    "DASHBOARD_HEALTH_TOKEN",
    "DASHBOARD_AUTH_SECURE_COOKIE",
    "DASHBOARD_AUTH_SESSION_HOURS",
    "DASHBOARD_TRUSTED_PROXY_COUNT",
    "DASHBOARD_TRUSTED_HOSTS",
    "DASHBOARD_AUTH_RATE_LIMIT_DB",
)
PASSWORD = "a sufficiently long test password"
FIVE_MINUTES_AND_ONE_SECOND = 301


def _settings(
    tmp_path: Path,
    *,
    secure_cookie: bool = False,
    trusted_proxy_count: int = 0,
    trusted_hosts: tuple[str, ...] = (),
) -> AuthSettings:
    return AuthSettings(
        enabled=True,
        username="thomas",
        password_hash=generate_password_hash(PASSWORD, method="scrypt"),
        secret_key="s" * 48,
        health_token="h" * 48,
        secure_cookie=secure_cookie,
        trusted_proxy_count=trusted_proxy_count,
        trusted_hosts=trusted_hosts,
        rate_limit_database=tmp_path / "login-attempts.sqlite3",
    )


def _app(config: AuthSettings) -> Flask:
    app = Flask(__name__)
    app.testing = True

    @app.get("/")
    def index() -> str:
        return "dashboard"

    @app.get("/assets/style.css")
    def asset() -> str:
        return "css"

    @app.route("/_dash-layout", methods=["GET", "POST"])
    def dash_layout() -> str:
        return "layout"

    @app.get("/health")
    def health() -> object:
        return jsonify(status="ok")

    configure_auth(app, config)
    register_public_pages(app)
    return app


def _csrf_token(body: bytes) -> str:
    match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', body)
    assert match is not None
    return match.group(1).decode()


def _login(client: object, *, password: str = PASSWORD) -> object:
    login_page = client.get("/login")
    return client.post(
        "/login",
        data={
            "csrf_token": _csrf_token(login_page.data),
            "username": "thomas",
            "password": password,
        },
    )


def test_authentication_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in AUTH_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    config = AuthSettings.from_environment()
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return "dashboard"

    state = configure_auth(app, config)

    assert state.settings.enabled is False
    assert app.test_client().get("/").status_code == 200


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DASHBOARD_AUTH_USERNAME", "", "USERNAME"),
        ("DASHBOARD_AUTH_PASSWORD_HASH", "pbkdf2:sha256$bad$bad", "Scrypt"),
        ("DASHBOARD_SECRET_KEY", "short", "SECRET_KEY"),
        ("DASHBOARD_HEALTH_TOKEN", "short", "HEALTH_TOKEN"),
    ],
)
def test_enabled_authentication_fails_closed_on_invalid_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    environment = {
        "DASHBOARD_AUTH_ENABLED": "true",
        "DASHBOARD_AUTH_USERNAME": "thomas",
        "DASHBOARD_AUTH_PASSWORD_HASH": generate_password_hash(
            PASSWORD, method="scrypt"
        ),
        "DASHBOARD_SECRET_KEY": "s" * 48,
        "DASHBOARD_HEALTH_TOKEN": "h" * 48,
        "DASHBOARD_AUTH_RATE_LIMIT_DB": str(tmp_path / "rate.sqlite3"),
    }
    environment[name] = value
    for variable, configured_value in environment.items():
        monkeypatch.setenv(variable, configured_value)

    with pytest.raises(ConfigurationError, match=message):
        AuthSettings.from_environment()


def test_all_dashboard_routes_are_protected(tmp_path: Path) -> None:
    client = _app(_settings(tmp_path)).test_client()

    assert client.get("/").status_code == 302
    assert client.get("/assets/style.css").status_code == 302
    assert client.get("/_dash-layout").status_code == 401
    assert client.post("/_dash-layout", json={}).status_code == 401
    assert client.get("/login").status_code == 200
    assert client.get("/impressum").status_code == 200
    assert client.get("/datenschutz").status_code == 200

    response = _login(client)

    assert response.status_code == 302
    assert response.location == "/"
    assert client.get("/").data == b"dashboard"
    assert client.get("/assets/style.css").data == b"css"
    assert client.get("/_dash-layout").data == b"layout"


def test_real_dash_endpoints_use_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environment = {
        "DASHBOARD_AUTH_ENABLED": "true",
        "DASHBOARD_AUTH_USERNAME": "thomas",
        "DASHBOARD_AUTH_PASSWORD_HASH": generate_password_hash(
            PASSWORD, method="scrypt"
        ),
        "DASHBOARD_SECRET_KEY": "s" * 48,
        "DASHBOARD_HEALTH_TOKEN": "h" * 48,
        "DASHBOARD_AUTH_SECURE_COOKIE": "false",
        "DASHBOARD_AUTH_RATE_LIMIT_DB": str(tmp_path / "dash-rate.sqlite3"),
    }
    for name in AUTH_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    import solarbank_dashboard.app as app_module

    monkeypatch.setattr(app_module, "settings", lambda: object())
    monkeypatch.setattr(app_module, "verify_schema", lambda _config: None)
    dash_app = app_module.create_app()
    client = dash_app.server.test_client()

    assert client.get("/").status_code == 302
    assert client.get("/_dash-layout").status_code == 401
    assert client.get("/health").status_code == 401
    assert client.get("/impressum").status_code == 200
    assert client.get("/datenschutz").status_code == 200
    assert client.get("/static/brand.css").status_code == 200
    assert client.get("/static/toasts.css").status_code == 200
    assert client.get("/static/toasts.js").status_code == 200
    assert client.get("/static/private.css").status_code == 302
    assert (
        client.get(
            "/health", headers={"Authorization": f"Bearer {'h' * 48}"}
        ).status_code
        == 200
    )
    assert _login(client).status_code == 302
    layout = client.get("/_dash-layout")
    assert layout.status_code == 200
    assert b"logout-link" in layout.data


def test_login_ignores_external_next_target(tmp_path: Path) -> None:
    client = _app(_settings(tmp_path)).test_client()
    page = client.get("/login?next=https://evil.example/")

    response = client.post(
        "/login?next=https://evil.example/",
        data={
            "csrf_token": _csrf_token(page.data),
            "username": "thomas",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 302
    assert response.location == "/"


@pytest.mark.parametrize(
    ("username", "password"),
    [("unknown", PASSWORD), ("thomas", "wrong password")],
)
def test_login_error_is_generic(tmp_path: Path, username: str, password: str) -> None:
    client = _app(_settings(tmp_path)).test_client()
    page = client.get("/login")

    response = client.post(
        "/login",
        data={
            "csrf_token": _csrf_token(page.data),
            "username": username,
            "password": password,
        },
    )

    assert response.status_code == 200
    assert b"Anmeldung fehlgeschlagen." in response.data


def test_login_and_logout_require_csrf(tmp_path: Path) -> None:
    client = _app(_settings(tmp_path)).test_client()

    assert (
        client.post(
            "/login", data={"username": "thomas", "password": PASSWORD}
        ).status_code
        == 400
    )

    assert _login(client).status_code == 302
    assert client.post("/logout").status_code == 400
    assert client.get("/").status_code == 200

    logout_page = client.get("/logout")
    response = client.post(
        "/logout", data={"csrf_token": _csrf_token(logout_page.data)}
    )

    assert response.status_code == 302
    assert response.location == "/login"
    assert client.get("/").status_code == 302


def test_health_requires_its_bearer_token(tmp_path: Path) -> None:
    client = _app(_settings(tmp_path)).test_client()

    missing = client.get("/health")
    wrong = client.get("/health", headers={"Authorization": "Bearer wrong"})
    valid = client.get("/health", headers={"Authorization": f"Bearer {'h' * 48}"})

    assert missing.status_code == 401
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert valid.status_code == 200
    assert valid.get_json() == {"status": "ok"}


def test_login_session_cookie_and_security_headers(tmp_path: Path) -> None:
    client = _app(_settings(tmp_path, secure_cookie=True)).test_client()
    page = client.get("/login", base_url="https://localhost")

    response = client.post(
        "/login",
        base_url="https://localhost",
        data={
            "csrf_token": _csrf_token(page.data),
            "username": "thomas",
            "password": PASSWORD,
        },
    )

    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith("__Host-solarbank_session=")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
    assert response.headers["Cache-Control"] == "no-store"

    dashboard = client.get("/", base_url="https://localhost")
    assert dashboard.headers["Content-Security-Policy"] == (
        "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
    )


def test_hsts_is_only_sent_over_https(tmp_path: Path) -> None:
    client = _app(_settings(tmp_path, secure_cookie=True)).test_client()

    response = client.get("/login", base_url="http://localhost")

    assert "Strict-Transport-Security" not in response.headers


def test_non_fresh_session_must_log_in_again(tmp_path: Path) -> None:
    client = _app(_settings(tmp_path)).test_client()
    assert _login(client).status_code == 302
    with client.session_transaction() as saved_session:
        saved_session["_fresh"] = False

    response = client.get("/")

    assert response.status_code == 302
    assert response.location == "/login"


def test_login_rate_limit_counts_only_failures_and_sets_retry_after(
    tmp_path: Path,
) -> None:
    client = _app(_settings(tmp_path)).test_client()
    page = client.get("/login")
    csrf_token = _csrf_token(page.data)
    data = {
        "csrf_token": csrf_token,
        "username": "thomas",
        "password": "wrong password",
    }

    for _ in range(5):
        assert client.post("/login", data=data).status_code == 200
    blocked = client.post("/login", data=data)

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


def test_parallel_login_requests_reserve_slots_before_password_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(_settings(tmp_path))
    verifier_lock = threading.Lock()
    five_verifiers_started = threading.Event()
    release_verifiers = threading.Event()
    verifier_count = 0

    def blocking_password_check(_password_hash: str, _password: str) -> bool:
        nonlocal verifier_count
        with verifier_lock:
            verifier_count += 1
            if verifier_count == 5:
                five_verifiers_started.set()
        assert release_verifiers.wait(timeout=10)
        return False

    monkeypatch.setattr(
        "solarbank_dashboard.auth.check_password_hash", blocking_password_check
    )

    def failed_login() -> int:
        client = app.test_client()
        page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "csrf_token": _csrf_token(page.data),
                "username": "thomas",
                "password": "wrong password",
            },
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(failed_login) for _ in range(10)]
        assert five_verifiers_started.wait(timeout=5)
        try:
            completed, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert completed
        finally:
            release_verifiers.set()
        statuses = [future.result(timeout=10) for future in futures]

    assert statuses.count(200) == 5
    assert statuses.count(429) == 5
    assert verifier_count == 5


def test_auth_settings_repr_hides_secrets(tmp_path: Path) -> None:
    config = _settings(tmp_path)

    representation = repr(config)

    assert config.password_hash not in representation
    assert config.secret_key not in representation
    assert config.health_token not in representation


def test_rate_limit_is_shared_between_instances(tmp_path: Path) -> None:
    database = tmp_path / "shared.sqlite3"
    first = SQLiteLoginRateLimiter(database, clock=lambda: 1_000.0)
    second = SQLiteLoginRateLimiter(database, clock=lambda: 1_000.0)

    for _ in range(3):
        assert first.retry_after("client") is None
        first.record_failure("client")
    for _ in range(2):
        assert second.retry_after("client") is None
        second.record_failure("client")

    assert first.retry_after("client") == 300
    assert second.retry_after("other-client") is None
    second.reset("client")
    assert first.retry_after("client") is None


def test_failure_limit_is_atomic_under_parallel_requests(tmp_path: Path) -> None:
    limiter = SQLiteLoginRateLimiter(
        tmp_path / "parallel.sqlite3", clock=lambda: 1_000.0
    )

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(
            executor.map(lambda _number: limiter.record_failure("client"), range(20))
        )

    assert results.count(None) == 5
    assert results.count(300) == 15


def test_hourly_rate_limit_applies_across_five_minute_windows(
    tmp_path: Path,
) -> None:
    current_time = [10_000.0]
    limiter = SQLiteLoginRateLimiter(
        tmp_path / "hour.sqlite3", clock=lambda: current_time[0]
    )

    for _window in range(4):
        for _ in range(5):
            assert limiter.retry_after("client") is None
            limiter.record_failure("client")
        current_time[0] += FIVE_MINUTES_AND_ONE_SECOND

    assert limiter.retry_after("client") is not None


def test_proxy_count_and_trusted_hosts_are_applied(tmp_path: Path) -> None:
    config = AuthSettings(
        trusted_proxy_count=1,
        trusted_hosts=("solar.example",),
        rate_limit_database=tmp_path / "unused.sqlite3",
    )
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return request.remote_addr or ""

    configure_auth(app, config)
    client = app.test_client()

    assert client.get("/", headers={"Host": "untrusted.example"}).status_code == 400
    response = client.get(
        "/",
        headers={
            "Host": "internal",
            "X-Forwarded-Host": "solar.example",
            "X-Forwarded-For": "203.0.113.10",
        },
    )
    assert response.status_code == 200
    assert response.data == b"203.0.113.10"


def test_credential_generator_outputs_scrypt_and_random_secrets() -> None:
    first = credential_lines("thomas", PASSWORD)
    second = credential_lines("thomas", PASSWORD)

    assert first[0] == "DASHBOARD_AUTH_ENABLED=true"
    assert first[1] == "DASHBOARD_AUTH_USERNAME='thomas'"
    assert first[2].startswith("DASHBOARD_AUTH_PASSWORD_HASH='scrypt:")
    assert first[3].startswith("DASHBOARD_SECRET_KEY='")
    assert first[4].startswith("DASHBOARD_HEALTH_TOKEN='")
    assert first[3:] != second[3:]


def test_credentials_cli_prompts_twice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    passwords = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("builtins.input", lambda _prompt: "thomas")
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(passwords))

    result = main(["credentials"])

    assert result == 0
    output = capsys.readouterr().out
    assert "DASHBOARD_AUTH_ENABLED=true" in output
    assert "DASHBOARD_AUTH_PASSWORD_HASH='scrypt:" in output
