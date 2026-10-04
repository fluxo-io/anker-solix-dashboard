from __future__ import annotations

import re
from pathlib import Path

from flask import Flask
from werkzeug.security import generate_password_hash

from solarbank_dashboard.auth import AuthSettings, configure_auth
from solarbank_dashboard.public_pages import (
    PUBLIC_STATIC_PATHS,
    register_public_pages,
)

PASSWORD = "a sufficiently long test password"


def _app(tmp_path: Path) -> Flask:
    app = Flask("solarbank_dashboard.app")
    app.testing = True

    @app.get("/")
    def index() -> str:
        return "dashboard"

    settings = AuthSettings(
        enabled=True,
        username="thomas",
        password_hash=generate_password_hash(PASSWORD, method="scrypt"),
        secret_key="s" * 48,
        health_token="h" * 48,
        secure_cookie=False,
        rate_limit_database=tmp_path / "login-attempts.sqlite3",
    )
    configure_auth(app, settings)
    register_public_pages(app)
    return app


def _csrf_token(body: bytes) -> str:
    match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', body)
    assert match is not None
    return match.group(1).decode()


def test_legal_pages_are_public_and_use_fluxo_brand(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()

    imprint = client.get("/impressum")
    privacy = client.get("/datenschutz")

    assert imprint.status_code == 200
    assert privacy.status_code == 200
    assert b"fluxo.io UG (haftungsbeschr\xc3\xa4nkt)" in imprint.data
    assert b"Thomas Walawgo" in imprint.data
    assert b"HRB 15272" in imprint.data
    assert b"DE328023177" in imprint.data
    assert b"fluxo.io-Demoprojekt" in imprint.data
    assert b"Architektur einer selbst gehosteten Datenplattform" in imprint.data
    assert b"logo-fluxo_io.svg" in imprint.data
    assert b"Private Solardatenbank" in privacy.data
    assert b"Datenschutz" in privacy.data
    assert b"datenschutz@fluxo.io" in privacy.data
    assert b'aria-current="page"' in imprint.data
    assert b'aria-current="page"' in privacy.data
    assert client.get("/").status_code == 302


def test_privacy_page_describes_actual_processing(tmp_path: Path) -> None:
    response = _app(tmp_path).test_client().get("/datenschutz")
    body = response.get_data(as_text=True)

    assert "Scrypt-Passwort-Hash" in body
    assert "HMAC pseudonymisiert" in body
    assert "höchstens etwa eine Stunde" in body
    assert "Sitzungscookie" in body
    assert "PostgreSQL" in body
    assert "JSON gespeichert" in body
    assert "als CSV" in body
    assert "inoffizielle" in body
    assert "lokalen Speicher des Browsers" in body
    assert "kein Tracking" in body


def test_login_and_logout_share_public_fluxo_layout(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()
    login = client.get("/login")

    assert login.status_code == 200
    assert b"logo-fluxo_io.svg" in login.data
    assert b"brand.css" in login.data
    assert b"toasts.css" in login.data
    assert b'<script defer src="/static/toasts.js"></script>' in login.data
    assert b'<p class="eyebrow">FLUXO.IO</p>' in login.data
    assert b">Solarbank Dashboard</h1>" in login.data
    assert "Geschützter Bereich" in login.get_data(as_text=True)
    assert b"FLUXO.IO DEMOPROJEKT" not in login.data
    assert b"Impressum" in login.data
    assert b"Datenschutz" in login.data
    assert b"<style" not in login.data
    assert b'class="skip-link" href="#main-content"' in login.data
    assert b'class="form-field form-floating"' in login.data
    assert b'placeholder="Benutzername"' in login.data
    assert b'placeholder="Passwort"' in login.data
    assert b'autocomplete="username"' in login.data
    assert b'autocomplete="current-password"' in login.data

    logged_in = client.post(
        "/login",
        data={
            "csrf_token": _csrf_token(login.data),
            "username": "thomas",
            "password": PASSWORD,
        },
    )
    assert logged_in.status_code == 302

    logout = client.get("/logout")
    assert logout.status_code == 200
    assert b"Abmelden?" in logout.data
    assert b"Private Solardatenbank" in logout.data
    assert b"logo-fluxo_io.svg" in logout.data

    confirmed_logout = client.post(
        "/logout", data={"csrf_token": _csrf_token(logout.data)}
    )
    assert confirmed_logout.status_code == 302
    login_after_logout = client.get(confirmed_logout.location)
    assert b"Abgemeldet." in login_after_logout.data
    assert b"data-toast-message" in login_after_logout.data
    assert b'data-toast-type="info"' in login_after_logout.data
    assert b"data-toast-text" in login_after_logout.data
    assert b"data-toast-remove" in login_after_logout.data
    assert b"Abgemeldet." not in client.get("/login").data


def test_login_error_is_bound_to_both_fields(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()
    login = client.get("/login")

    response = client.post(
        "/login",
        data={
            "csrf_token": _csrf_token(login.data),
            "username": "thomas",
            "password": "wrong password",
        },
    )
    body = response.get_data(as_text=True)

    assert 'id="login-error"' in body
    assert body.count('aria-describedby="login-error"') == 2
    assert body.count('aria-invalid="true"') == 2


def test_only_required_brand_static_files_are_public(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()

    for path in PUBLIC_STATIC_PATHS:
        response = client.get(path)
        assert response.status_code == 200, path

    blocked = client.get("/static/private.css")
    assert blocked.status_code == 302
    assert blocked.location == "/login"


def test_bad_requests_use_branded_error_page(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()

    response = client.post(
        "/login",
        data={"username": "thomas", "password": PASSWORD},
    )

    assert response.status_code == 400
    assert "Anfrage ungültig" in response.get_data(as_text=True)
    assert b'<p class="eyebrow">FLUXO.IO</p>' in response.data
    assert b"Zur Anmeldung" in response.data
    assert b"logo-fluxo_io.svg" in response.data


def test_public_pages_use_restrictive_content_security_policy(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()

    for path in ("/login", "/logout", "/impressum", "/datenschutz"):
        response = client.get(path)
        if path == "/logout":
            assert response.status_code == 302
            continue
        policy = response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy
        assert "style-src 'self'" in policy
        assert "script-src 'self'" in policy
        assert "img-src 'self'" in policy
        assert "font-src 'self'" in policy
        assert "'unsafe-inline'" not in policy
        assert "'unsafe-eval'" not in policy


def test_public_page_registration_is_idempotent(tmp_path: Path) -> None:
    app = _app(tmp_path)

    register_public_pages(app)

    assert app.test_client().get("/impressum").status_code == 200
