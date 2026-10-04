from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import ipaddress
import logging
import math
import os
import secrets
import sqlite3
import string
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    flash,
    get_flashed_messages,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_fresh,
    login_user,
    logout_user,
)
from flask_wtf import FlaskForm
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import InputRequired, Length

from solarbank_dashboard.config import ConfigurationError
from solarbank_dashboard.public_pages import PUBLIC_PAGE_PATHS, PUBLIC_STATIC_PATHS

LOGGER = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_DATABASE = Path("/tmp/solarbank-auth-rate-limit.sqlite3")
FIVE_MINUTES = 5 * 60
ONE_HOUR = 60 * 60


def _boolean_environment(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} muss true oder false sein")


def _integer_environment(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} muss eine ganze Zahl sein") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} muss zwischen {minimum} und {maximum} liegen")
    return value


def _is_scrypt_hash(value: str) -> bool:
    if len(value) > 1_024:
        return False
    try:
        method, salt, digest = value.split("$")
        algorithm, raw_n, raw_r, raw_p = method.split(":")
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
    except (TypeError, ValueError):
        return False
    return (
        algorithm == "scrypt"
        and 32_768 <= n <= 262_144
        and n & (n - 1) == 0
        and 1 <= r <= 32
        and 1 <= p <= 16
        and 8 <= len(salt) <= 128
        and 64 <= len(digest) <= 256
        and all(character in string.hexdigits for character in digest)
    )


def _validated_hosts(raw: str) -> tuple[str, ...]:
    hosts = tuple(part.strip() for part in raw.split(",") if part.strip())
    for host in hosts:
        if (
            len(host) > 255
            or "://" in host
            or "/" in host
            or any(character.isspace() or ord(character) < 32 for character in host)
        ):
            raise ConfigurationError("DASHBOARD_TRUSTED_HOSTS ist ungültig")
    return hosts


@dataclass(frozen=True, slots=True)
class AuthSettings:
    enabled: bool = False
    username: str = ""
    password_hash: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    health_token: str = field(default="", repr=False)
    secure_cookie: bool = True
    session_hours: int = 12
    trusted_proxy_count: int = 0
    trusted_hosts: tuple[str, ...] = ()
    rate_limit_database: Path = DEFAULT_RATE_LIMIT_DATABASE

    def __post_init__(self) -> None:
        if not 1 <= self.session_hours <= 168:
            raise ConfigurationError(
                "DASHBOARD_AUTH_SESSION_HOURS muss zwischen 1 und 168 liegen"
            )
        if not 0 <= self.trusted_proxy_count <= 10:
            raise ConfigurationError(
                "DASHBOARD_TRUSTED_PROXY_COUNT muss zwischen 0 und 10 liegen"
            )
        if not self.rate_limit_database.is_absolute():
            raise ConfigurationError(
                "DASHBOARD_AUTH_RATE_LIMIT_DB muss ein absoluter Pfad sein"
            )
        if not self.enabled:
            return
        if not self.username or len(self.username) > 128:
            raise ConfigurationError("DASHBOARD_AUTH_USERNAME fehlt oder ist zu lang")
        if any(ord(character) < 32 for character in self.username):
            raise ConfigurationError("DASHBOARD_AUTH_USERNAME ist ungültig")
        if not _is_scrypt_hash(self.password_hash):
            raise ConfigurationError(
                "DASHBOARD_AUTH_PASSWORD_HASH muss ein sicherer Scrypt-Hash sein"
            )
        if len(self.secret_key.encode()) < 32:
            raise ConfigurationError("DASHBOARD_SECRET_KEY ist zu kurz")
        if len(self.health_token.encode()) < 32:
            raise ConfigurationError("DASHBOARD_HEALTH_TOKEN ist zu kurz")

    @classmethod
    def from_environment(cls) -> AuthSettings:
        enabled = _boolean_environment("DASHBOARD_AUTH_ENABLED", False)
        username = os.getenv("DASHBOARD_AUTH_USERNAME", "").strip()
        password_hash = os.getenv("DASHBOARD_AUTH_PASSWORD_HASH", "").strip()
        secret_key = os.getenv("DASHBOARD_SECRET_KEY", "")
        health_token = os.getenv("DASHBOARD_HEALTH_TOKEN", "")
        rate_limit_database = Path(
            os.getenv(
                "DASHBOARD_AUTH_RATE_LIMIT_DB", str(DEFAULT_RATE_LIMIT_DATABASE)
            ).strip()
        )
        return cls(
            enabled=enabled,
            username=username,
            password_hash=password_hash,
            secret_key=secret_key,
            health_token=health_token,
            secure_cookie=_boolean_environment("DASHBOARD_AUTH_SECURE_COOKIE", True),
            session_hours=_integer_environment(
                "DASHBOARD_AUTH_SESSION_HOURS", 12, minimum=1, maximum=168
            ),
            trusted_proxy_count=_integer_environment(
                "DASHBOARD_TRUSTED_PROXY_COUNT", 0, minimum=0, maximum=10
            ),
            trusted_hosts=_validated_hosts(os.getenv("DASHBOARD_TRUSTED_HOSTS", "")),
            rate_limit_database=rate_limit_database,
        )


class LoginForm(FlaskForm):
    username = StringField(
        validators=[InputRequired(), Length(max=128)],
        render_kw={"maxlength": 128},
    )
    password = PasswordField(
        validators=[InputRequired(), Length(max=1_024)],
        render_kw={"maxlength": 1_024},
    )
    submit = SubmitField("Anmelden")


class LogoutForm(FlaskForm):
    submit = SubmitField("Abmelden")


@dataclass(frozen=True, slots=True)
class _DashboardUser(UserMixin):
    id: str


class SQLiteLoginRateLimiter:
    """A small process-safe rolling limiter backed by a shared SQLite file."""

    def __init__(self, database: Path, *, clock: Any = time.time) -> None:
        self.database = database
        self.clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS login_attempt (
                    client_key TEXT NOT NULL,
                    attempted_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS login_attempt_client_time
                ON login_attempt (client_key, attempted_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS login_attempt_reservation (
                    reservation_id TEXT PRIMARY KEY,
                    client_key TEXT NOT NULL,
                    attempted_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS login_attempt_reservation_client_time
                ON login_attempt_reservation (client_key, attempted_at)
                """
            )
        os.chmod(self.database, 0o600)

    @staticmethod
    def _retry_after_locked(
        connection: sqlite3.Connection, client_key: str, now: float
    ) -> int | None:
        five_minute_cutoff = now - FIVE_MINUTES
        hour_cutoff = now - ONE_HOUR
        connection.execute(
            "DELETE FROM login_attempt WHERE attempted_at <= ?", (hour_cutoff,)
        )
        connection.execute(
            "DELETE FROM login_attempt_reservation WHERE attempted_at <= ?",
            (hour_cutoff,),
        )
        timestamps = sorted(
            float(row[0])
            for row in connection.execute(
                """
                SELECT attempted_at
                FROM login_attempt
                WHERE client_key = ? AND attempted_at > ?
                UNION ALL
                SELECT attempted_at
                FROM login_attempt_reservation
                WHERE client_key = ? AND attempted_at > ?
                """,
                (client_key, hour_cutoff, client_key, hour_cutoff),
            )
        )

        waits: list[float] = []
        recent = [
            timestamp for timestamp in timestamps if timestamp > five_minute_cutoff
        ]
        if len(recent) >= 5:
            waits.append(recent[-5] + FIVE_MINUTES - now)
        if len(timestamps) >= 20:
            waits.append(timestamps[-20] + ONE_HOUR - now)
        if not waits:
            return None
        return max(1, math.ceil(max(waits)))

    def retry_after(self, client_key: str) -> int | None:
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._retry_after_locked(connection, client_key, now)

    def record_failure(self, client_key: str) -> int | None:
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            retry_after = self._retry_after_locked(connection, client_key, now)
            if retry_after is not None:
                return retry_after
            connection.execute(
                "INSERT INTO login_attempt (client_key, attempted_at) VALUES (?, ?)",
                (client_key, now),
            )
        return None

    def reserve_attempt(self, client_key: str) -> tuple[str | None, int | None]:
        """Atomically reserve a rate-limit slot before checking credentials."""

        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            retry_after = self._retry_after_locked(connection, client_key, now)
            if retry_after is not None:
                return None, retry_after
            reservation_id = secrets.token_hex(16)
            connection.execute(
                """
                INSERT INTO login_attempt_reservation (
                    reservation_id, client_key, attempted_at
                ) VALUES (?, ?, ?)
                """,
                (reservation_id, client_key, now),
            )
        return reservation_id, None

    def finalize_failure(self, client_key: str, reservation_id: str) -> None:
        """Atomically turn a reservation into a recorded failure."""

        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT attempted_at
                FROM login_attempt_reservation
                WHERE reservation_id = ? AND client_key = ?
                """,
                (reservation_id, client_key),
            ).fetchone()
            attempted_at = float(row[0]) if row is not None else now
            connection.execute(
                """
                DELETE FROM login_attempt_reservation
                WHERE reservation_id = ? AND client_key = ?
                """,
                (reservation_id, client_key),
            )
            connection.execute(
                "INSERT INTO login_attempt (client_key, attempted_at) VALUES (?, ?)",
                (client_key, attempted_at),
            )

    def finalize_success(self, client_key: str, reservation_id: str) -> None:
        """Release this reservation and clear earlier completed failures."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM login_attempt_reservation
                WHERE reservation_id = ? AND client_key = ?
                """,
                (reservation_id, client_key),
            )
            connection.execute(
                "DELETE FROM login_attempt WHERE client_key = ?", (client_key,)
            )

    def reset(self, client_key: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM login_attempt WHERE client_key = ?", (client_key,)
            )
            connection.execute(
                "DELETE FROM login_attempt_reservation WHERE client_key = ?",
                (client_key,),
            )


@dataclass(frozen=True, slots=True)
class AuthState:
    settings: AuthSettings
    login_manager: LoginManager | None = None
    rate_limiter: SQLiteLoginRateLimiter | None = None


def _user_id(config: AuthSettings) -> str:
    material = f"{config.username}\0{config.password_hash}".encode()
    return hmac.new(config.secret_key.encode(), material, hashlib.sha256).hexdigest()


def _client_key(config: AuthSettings) -> str:
    remote_address = request.remote_addr or "unknown"
    try:
        remote_address = ipaddress.ip_address(remote_address).compressed
    except ValueError:
        remote_address = "unknown"
    return hmac.new(
        config.secret_key.encode(), remote_address.encode(), hashlib.sha256
    ).hexdigest()


def _valid_health_token(expected: str) -> bool:
    scheme, separator, supplied = request.headers.get("Authorization", "").partition(
        " "
    )
    return bool(
        separator
        and scheme.lower() == "bearer"
        and supplied
        and hmac.compare_digest(supplied.encode(), expected.encode())
    )


def _unauthorized() -> Response:
    response = jsonify(error="Anmeldung erforderlich")
    response.status_code = 401
    return response


def _login_response(form: LoginForm, message: str = "") -> str:
    notices = get_flashed_messages(category_filter=["notice"])
    return render_template(
        "login.html",
        form=form,
        message=message,
        notice=notices[0] if notices else "",
    )


def _configure_common_security(app: Flask, config: AuthSettings) -> None:
    if config.trusted_hosts:
        app.config["TRUSTED_HOSTS"] = list(config.trusted_hosts)
    if config.trusted_proxy_count:
        count = config.trusted_proxy_count
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=count,
            x_proto=count,
            x_host=count,
            x_port=count,
            x_prefix=0,
        )

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), geolocation=(), microphone=()"
        )
        if config.enabled:
            public_document_paths = {
                "/login",
                "/logout",
                *PUBLIC_PAGE_PATHS,
                *PUBLIC_STATIC_PATHS,
            }
            content_security_policy = (
                "default-src 'none'; style-src 'self'; script-src 'self'; "
                "img-src 'self'; font-src 'self'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'"
                if request.path in public_document_paths
                else "frame-ancestors 'none'; object-src 'none'; "
                "base-uri 'self'; form-action 'self'"
            )
            response.headers.setdefault(
                "Content-Security-Policy", content_security_policy
            )
        if (
            config.enabled
            and not request.path.startswith("/assets/")
            and request.path not in PUBLIC_STATIC_PATHS
        ):
            response.headers.setdefault("Cache-Control", "no-store")
        if config.enabled and config.secure_cookie and request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response


def configure_auth(app: Flask, config: AuthSettings | None = None) -> AuthState:
    """Configure optional single-user authentication for the complete Flask app."""

    existing = app.extensions.get("solarbank_auth")
    if existing is not None:
        return existing

    auth_config = config or AuthSettings.from_environment()
    _configure_common_security(app, auth_config)
    if not auth_config.enabled:
        state = AuthState(settings=auth_config)
        app.extensions["solarbank_auth"] = state
        return state

    app.config.update(
        SECRET_KEY=auth_config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=auth_config.secure_cookie,
        SESSION_COOKIE_NAME=(
            "__Host-solarbank_session"
            if auth_config.secure_cookie
            else "solarbank_session"
        ),
        SESSION_COOKIE_PATH="/",
        SESSION_COOKIE_DOMAIN=None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=auth_config.session_hours),
        SESSION_REFRESH_EACH_REQUEST=False,
        WTF_CSRF_TIME_LIMIT=auth_config.session_hours * ONE_HOUR,
    )

    user_id = _user_id(auth_config)
    login_manager = LoginManager()
    login_manager.session_protection = "strong"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(saved_user_id: str) -> _DashboardUser | None:
        if hmac.compare_digest(saved_user_id.encode(), user_id.encode()):
            return _DashboardUser(user_id)
        return None

    rate_limiter = SQLiteLoginRateLimiter(auth_config.rate_limit_database)

    def rate_limited_response(form: LoginForm, retry_after: int) -> Response:
        response = app.make_response(
            (
                _login_response(form, "Zu viele Versuche. Später erneut versuchen."),
                429,
            )
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    @app.before_request
    def require_authentication() -> Response | None:
        if request.path == "/health":
            if _valid_health_token(auth_config.health_token):
                return None
            response = _unauthorized()
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
        if request.path == "/login":
            return None
        if request.path in PUBLIC_PAGE_PATHS or request.path in PUBLIC_STATIC_PATHS:
            return None
        if current_user.is_authenticated:
            if login_fresh():
                return None
            logout_user()
            session.clear()
        if request.method in {"GET", "HEAD"} and not request.path.startswith("/_dash-"):
            return redirect(url_for("solarbank_auth_login"))
        return _unauthorized()

    @app.route("/login", methods=["GET", "POST"], endpoint="solarbank_auth_login")
    def login() -> Any:
        if current_user.is_authenticated and login_fresh():
            return redirect("/")
        if current_user.is_authenticated:
            logout_user()
            session.clear()
        if request.content_length is not None and request.content_length > 16_384:
            abort(413)
        form = LoginForm()
        if request.method == "GET":
            return _login_response(form)
        if not form.validate_on_submit():
            if form.csrf_token.errors:
                abort(400, description="Ungültige Anfrage")
            return _login_response(form, "Anmeldung fehlgeschlagen."), 200
        try:
            client_key = _client_key(auth_config)
            reservation_id, retry_after = rate_limiter.reserve_attempt(client_key)
        except sqlite3.Error:
            LOGGER.exception("Anmeldedrosselung ist nicht verfügbar")
            return _login_response(form, "Anmeldung nicht verfügbar."), 503
        if retry_after is not None:
            return rate_limited_response(form, retry_after)
        if reservation_id is None:
            LOGGER.error("Anmeldedrosselung hat keine Reservierung erstellt")
            return _login_response(form, "Anmeldung nicht verfügbar."), 503

        supplied_username = form.username.data or ""
        supplied_password = form.password.data or ""
        username_matches = hmac.compare_digest(
            supplied_username.encode(), auth_config.username.encode()
        )
        password_matches = check_password_hash(
            auth_config.password_hash, supplied_password
        )
        if not (username_matches and password_matches):
            try:
                rate_limiter.finalize_failure(client_key, reservation_id)
            except sqlite3.Error:
                LOGGER.exception("Anmeldedrosselung ist nicht verfügbar")
                return _login_response(form, "Anmeldung nicht verfügbar."), 503
            return _login_response(form, "Anmeldung fehlgeschlagen."), 200

        try:
            rate_limiter.finalize_success(client_key, reservation_id)
        except sqlite3.Error:
            LOGGER.exception("Anmeldedrosselung ist nicht verfügbar")
            return _login_response(form, "Anmeldung nicht verfügbar."), 503
        session.clear()
        login_user(_DashboardUser(user_id), remember=False, fresh=True)
        session.permanent = True
        return redirect("/")

    @app.route("/logout", methods=["GET", "POST"], endpoint="solarbank_auth_logout")
    def logout() -> Any:
        form = LogoutForm()
        if request.method == "GET":
            return render_template("logout.html", form=form)
        if not form.validate_on_submit():
            abort(400, description="Ungültige Anfrage")
        logout_user()
        session.clear()
        flash("Abgemeldet.", "notice")
        return redirect(url_for("solarbank_auth_login"))

    state = AuthState(
        settings=auth_config,
        login_manager=login_manager,
        rate_limiter=rate_limiter,
    )
    app.extensions["solarbank_auth"] = state
    return state


def _dotenv_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def credential_lines(username: str, password: str) -> list[str]:
    username = username.strip()
    if (
        not username
        or len(username) > 128
        or any(ord(character) < 32 for character in username)
    ):
        raise ValueError("Benutzername ist ungültig")
    if len(password) < 14:
        raise ValueError("Passwort muss mindestens 14 Zeichen lang sein")
    password_hash = generate_password_hash(password, method="scrypt")
    return [
        "DASHBOARD_AUTH_ENABLED=true",
        f"DASHBOARD_AUTH_USERNAME={_dotenv_quote(username)}",
        f"DASHBOARD_AUTH_PASSWORD_HASH={_dotenv_quote(password_hash)}",
        f"DASHBOARD_SECRET_KEY={_dotenv_quote(secrets.token_urlsafe(48))}",
        f"DASHBOARD_HEALTH_TOKEN={_dotenv_quote(secrets.token_urlsafe(32))}",
    ]


def _credentials_command() -> int:
    username = input("Benutzername: ").strip()
    password = getpass.getpass("Passwort: ")
    confirmation = getpass.getpass("Passwort wiederholen: ")
    if password != confirmation:
        raise ValueError("Passwörter stimmen nicht überein")
    print("\n".join(credential_lines(username, password)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Solarbank-Anmeldung verwalten")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("credentials", help="Zugangsdaten erzeugen")
    arguments = parser.parse_args(argv)
    if arguments.command == "credentials":
        try:
            return _credentials_command()
        except ValueError as error:
            parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
