"""Real HTTP API and synthetic SQLite; no production configuration or data."""

import re
import sqlite3
import threading
import time
from dataclasses import replace

import pytest
from solarbank_api.app import create_app as api_factory
from solarbank_api.config import Settings as APISettings
from solarbank_dashboard.app import create_app as web_factory
from solarbank_dashboard.auth import AuthSettings
from solarbank_dashboard.config import Settings as WebSettings
from werkzeug.serving import make_server

PASSWORD = "synthetic-login-password"
SECRET = "synthetic-secret-" + "x" * 40
TOKEN = "synthetic-api-" + "y" * 40


class RunningAPI:
    def __init__(self, settings):
        self.settings = settings
        self.server = None
        self.start()

    def start(self):
        self.app = api_factory(self.settings)
        self.server = make_server(
            "127.0.0.1", getattr(self, "port", 0), self.app, threaded=True
        )
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.server = None


@pytest.fixture
def api(tmp_path):
    settings = APISettings(
        db_path=tmp_path / "app.sqlite3",
        api_token=TOKEN,
        secret_key=SECRET,
        admin_username="admin",
        admin_password=PASSWORD,
        collector_api_token="synthetic-collector-" + "z" * 40,
    )
    running = RunningAPI(settings)
    yield running
    running.stop()


def web(api, secure=False):
    app = web_factory(
        WebSettings(
            api_url=f"http://127.0.0.1:{api.port}",
            api_token=TOKEN,
            timezone_name="Europe/Berlin",
            api_timeout_seconds=1,
        ),
        auth_config=AuthSettings(
            secret_key=SECRET,
            secure_cookie=secure,
            trusted_proxy_count=1 if secure else 0,
        ),
    ).server
    app.testing = True
    return app


def csrf(response):
    assert response.status_code == 200
    return re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', response.data)[
        1
    ].decode()


def login(client, password=PASSWORD, **options):
    token = csrf(client.get("/login", **options))
    return client.post(
        "/login",
        data={"csrf_token": token, "username": "admin", "password": password},
        **options,
    )


def test_real_api_login_wrong_password_csrf_dashboard_and_logout(api):
    client = web(api).test_client()
    assert client.get("/").status_code == 302
    assert (
        client.post(
            "/login", data={"username": "admin", "password": PASSWORD}
        ).status_code
        == 400
    )
    failed = login(client, "wrong")
    assert failed.status_code == 200 and b"Anmeldung fehlgeschlagen" in failed.data
    assert login(client).status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/_dash-layout").status_code == 200
    assert client.post("/logout").status_code == 400
    token = csrf(client.get("/logout"))
    assert client.get("/").status_code == 200
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302
    assert client.get("/").status_code == 302


def test_cookie_and_account_survive_api_and_web_restart(api):
    app = web(api)
    client = app.test_client()
    assert login(client).status_code == 302
    cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    api.stop()
    api.settings = replace(
        api.settings,
        admin_username="different",
        admin_password="changed-initial-password",
    )
    api.start()
    restarted = web(api).test_client()
    restarted.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie.value)
    assert restarted.get("/").status_code == 200
    assert login(web(api).test_client()).status_code == 302
    rejected = login(web(api).test_client(), "changed-initial-password")
    assert rejected.status_code == 200 and b"Anmeldung fehlgeschlagen" in rejected.data
    with sqlite3.connect(api.settings.db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT username FROM auth_users").fetchall() == [
            ("admin",)
        ]


def test_password_change_revokes_old_cookie(api):
    client = web(api).test_client()
    assert login(client).status_code == 302
    changed = "synthetic-new-password"
    api.app.extensions["solarbank_store"].reset_password("admin", changed)
    assert client.get("/").status_code == 302
    assert login(client, changed).status_code == 302
    assert client.get("/").status_code == 200


def test_absolute_expiry_does_not_extend_on_requests(api):
    client = web(api).test_client()
    assert login(client).status_code == 302
    with client.session_transaction() as state:
        initial = state["authenticated_at"]
    assert client.get("/").status_code == 200
    with client.session_transaction() as state:
        assert state["authenticated_at"] == initial
        state["authenticated_at"] = time.time() - 8 * 3600 - 1
    assert client.get("/").status_code == 302
    with client.session_transaction() as state:
        assert "_user_id" not in state


def test_https_proxy_cookie_and_csrf_with_real_api(api):
    app = web(api, secure=True)
    client = app.test_client()
    headers = {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "kraftwerk.example.test",
        "X-Forwarded-Port": "443",
        "X-Forwarded-For": "192.0.2.1",
    }
    opts = {"base_url": "https://kraftwerk.example.test", "headers": headers}
    form = client.get("/login", **opts)
    cookie = form.headers["Set-Cookie"]
    assert cookie.startswith("__Host-solarbank_session=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie
    token = csrf(form)
    fields = {"username": "admin", "password": PASSWORD, "csrf_token": token}
    assert client.post("/login", data=fields, **opts).status_code == 400
    opts["headers"] = {**headers, "Referer": "https://kraftwerk.example.test/login"}
    assert client.post("/login", data=fields, **opts).status_code == 302
    assert client.get("/", **opts).status_code == 200


def test_logout_confirmation_remains_available_during_api_outage(api):
    client = web(api).test_client()
    assert login(client).status_code == 302
    api.stop()
    assert client.get("/").status_code == 503
    token = csrf(client.get("/logout"))
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302
    assert client.get("/login").status_code == 200


def test_ip_change_keeps_server_validated_session(api):
    client = web(api).test_client()
    assert login(client).status_code == 302
    assert (
        client.get("/", environ_overrides={"REMOTE_ADDR": "192.0.2.22"}).status_code
        == 200
    )


def test_logout_rejects_browser_cookie_replay_and_preserves_other_session(api):
    app = web(api)
    client = app.test_client()
    assert login(client).status_code == 302
    original = client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    other = app.test_client()
    assert login(other).status_code == 302
    token = csrf(client.get("/logout"))
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302
    client.set_cookie(app.config["SESSION_COOKIE_NAME"], original)
    assert client.get("/").status_code == 302
    assert other.get("/").status_code == 200


def test_api_expiry_is_authoritative_over_longer_web_cookie(api):
    from unittest.mock import patch

    api.stop()
    api.settings = replace(api.settings, session_lifetime_seconds=10)
    api.start()
    client = web(api).test_client()
    assert login(client).status_code == 302
    with patch("time.time", return_value=time.time() + 11):
        assert client.get("/").status_code == 302


def test_account_management_and_initial_password_flow(api):
    application = web(api)
    client = application.test_client()
    assert login(client).status_code == 302
    assert client.get("/users").status_code == 200
    assert (
        client.post(
            "/users", data={"username": "initial", "password": PASSWORD}
        ).status_code
        == 400
    )
    for name, mode in [("fixed", "fixed"), ("initial", "initial")]:
        response = client.post(
            "/users",
            data={
                "csrf_token": csrf(client.get("/users")),
                "username": name,
                "password": PASSWORD,
                "password_mode": mode,
            },
        )
        assert response.status_code == 302
    table = client.get("/users")
    assert b"Noch nicht erfasst" in table.data
    assert re.search(rb"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2} (CET|CEST)", table.data)
    user_client = application.test_client()
    response = user_client.post(
        "/login",
        data={
            "csrf_token": csrf(user_client.get("/login")),
            "username": "initial",
            "password": PASSWORD,
        },
    )
    assert response.location == "/password"
    assert user_client.get("/").location == "/password"
    assert user_client.get("/users").location == "/password"
    response = user_client.post(
        "/password",
        data={
            "csrf_token": csrf(user_client.get("/password")),
            "old_password": PASSWORD,
            "password": PASSWORD,
            "confirm": PASSWORD,
        },
    )
    assert response.status_code == 400
    response = user_client.post(
        "/password",
        data={
            "csrf_token": csrf(user_client.get("/password")),
            "old_password": PASSWORD,
            "password": "Changed-password-456",
            "confirm": "Changed-password-456",
        },
    )
    assert response.status_code == 302
    assert user_client.get("/").status_code == 200
    assert user_client.get("/users").status_code == 403
    fixed_client = application.test_client()
    response = fixed_client.post(
        "/login",
        data={
            "csrf_token": csrf(fixed_client.get("/login")),
            "username": "fixed",
            "password": PASSWORD,
        },
    )
    assert response.location == "/"
    response = client.post(
        "/users/password-reset",
        data={
            "csrf_token": csrf(client.get("/users/password-reset?username=fixed")),
            "username": "fixed",
            "password": "Reset-password-123",
            "password_mode": "initial",
        },
    )
    assert response.status_code == 302
    assert fixed_client.get("/").location == "/login"
