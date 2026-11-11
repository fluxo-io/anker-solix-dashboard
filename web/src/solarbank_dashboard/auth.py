from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    g,
    get_flashed_messages,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    AnonymousUserMixin,
    LoginManager,
    UserMixin,
    current_user,
    login_user,
)
from flask_wtf import CSRFProtect, FlaskForm
from fluxo_auth import configure_flask_session, session_seconds
from werkzeug.middleware.proxy_fix import ProxyFix
from wtforms import PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import EqualTo, InputRequired, Length

from .config import ConfigurationError, Settings
from .public_pages import PUBLIC_PAGE_PATHS, PUBLIC_STATIC_PATHS, SHORTCUT_STATIC_PATHS
from .repository import ApiError, request_api


def _boolean_environment(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} muss true oder false sein")


@dataclass(frozen=True, slots=True)
class AuthSettings:
    enabled: bool = True
    secret_key: str = field(default="", repr=False)
    secure_cookie: bool = False
    session_hours: float = 8
    trusted_proxy_count: int = 0
    trusted_hosts: tuple[str, ...] = ()
    session_cookie_name: str | None = None
    session_lifetime_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.enabled and len(self.secret_key) < 32:
            raise ConfigurationError("SECRET_KEY benötigt mindestens 32 Zeichen")
        try:
            session_seconds({}, default=self.duration_seconds)
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        if self.trusted_proxy_count not in {0, 1}:
            raise ConfigurationError("TRUST_PROXY muss true oder false sein")
        if self.session_cookie_name is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.session_cookie_name):
                raise ConfigurationError("SESSION_COOKIE_NAME ist ungültig")
            if (
                self.session_cookie_name.startswith(("__Host-", "__Secure-"))
                and not self.secure_cookie
            ):
                raise ConfigurationError(
                    "Der Cookie-Präfix benötigt SESSION_COOKIE_SECURE=true"
                )

    @property
    def duration_seconds(self) -> int:
        if self.session_lifetime_seconds is not None:
            return session_seconds({}, default=self.session_lifetime_seconds)
        return session_seconds({"SESSION_HOURS": self.session_hours})

    @classmethod
    def from_environment(cls) -> AuthSettings:
        origin = os.getenv("CANONICAL_ORIGIN", "http://localhost:8501").rstrip("/")
        parsed = urlsplit(origin)
        if origin and (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ConfigurationError("CANONICAL_ORIGIN ist ungültig")
        try:
            duration_seconds = session_seconds(os.environ)
        except ValueError as error:
            raise ConfigurationError("SESSION_HOURS ist ungültig") from error
        return cls(
            enabled=_boolean_environment("AUTH_ENABLED", True),
            secret_key=os.getenv("SECRET_KEY", ""),
            secure_cookie=_boolean_environment("SESSION_COOKIE_SECURE", False),
            session_lifetime_seconds=duration_seconds,
            session_hours=duration_seconds / 3600,
            trusted_proxy_count=int(_boolean_environment("TRUST_PROXY", False)),
            trusted_hosts=(parsed.hostname, "localhost", "127.0.0.1") if origin else (),
            session_cookie_name=os.environ.get("SESSION_COOKIE_NAME"),
        )


class LoginForm(FlaskForm):
    username = StringField(
        validators=[InputRequired(), Length(max=254)], render_kw={"maxlength": 254}
    )
    password = PasswordField(
        validators=[InputRequired(), Length(max=1024)], render_kw={"maxlength": 1024}
    )
    submit = SubmitField("Anmelden")


class LogoutForm(FlaskForm):
    submit = SubmitField("Abmelden")


class PasswordForm(FlaskForm):
    old_password = PasswordField(
        "Aktuelles Passwort", validators=[InputRequired(), Length(max=1024)]
    )
    password = PasswordField(
        "Neues Passwort", validators=[InputRequired(), Length(min=12, max=1024)]
    )
    confirm = PasswordField(
        "Passwort wiederholen",
        validators=[
            InputRequired(),
            EqualTo("password", message="Die Passwörter stimmen nicht überein."),
        ],
    )
    submit = SubmitField("Passwort ändern")


class AccountForm(FlaskForm):
    username = StringField(
        "Benutzername", validators=[InputRequired(), Length(max=254)]
    )
    password = PasswordField(
        "Passwort", validators=[InputRequired(), Length(min=12, max=1024)]
    )
    password_mode = SelectField(
        "Passwortart",
        choices=[
            ("initial", "Initialpasswort"),
            ("fixed", "Festes Passwort"),
        ],
        default="initial",
    )
    submit = SubmitField("Speichern")


class _DashboardUser(UserMixin):
    def __init__(self, data: dict) -> None:
        self.id = str(data["id"])
        self.username = data.get("username", "")
        self.must_change_password = data.get("must_change_password", False)
        self.can_manage_users = data.get("can_manage_users", False)


@dataclass(frozen=True, slots=True)
class AuthState:
    settings: AuthSettings
    login_manager: LoginManager | None = None


def _login_response(form: LoginForm, message: str = "") -> str:
    notices = get_flashed_messages(category_filter=["notice"])
    return render_template(
        "login.html", form=form, message=message, notice=notices[0] if notices else ""
    )


def configure_auth(app: Flask, config: AuthSettings | None = None) -> AuthState:
    if "solarbank_auth" in app.extensions:
        return app.extensions["solarbank_auth"]
    from .assets import install_assets

    if "asset_manifest" not in app.extensions:
        install_assets(app)
    config = config or AuthSettings.from_environment()
    if config.trusted_hosts:
        app.config["TRUSTED_HOSTS"] = list(config.trusted_hosts)
    if config.trusted_proxy_count:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), geolocation=(), microphone=()"
        )
        public = request.path in {
            "/login",
            "/logout",
            *PUBLIC_PAGE_PATHS,
            *PUBLIC_STATIC_PATHS,
        }
        policy = (
            "default-src 'none'; style-src 'self'; script-src 'self'; "
            "img-src 'self'; font-src 'self'; manifest-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
            if public
            else "frame-ancestors 'none'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", policy)
        if request.path == "/static/site.webmanifest" and response.status_code == 200:
            response.mimetype = "application/manifest+json"
        if not request.path.startswith(("/static/", "/assets/")):
            response.headers.setdefault("Cache-Control", "no-store")
        if config.secure_cookie and request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    if not config.enabled:
        state = AuthState(config)
        app.extensions["solarbank_auth"] = state
        return state
    app.config.update(
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.secure_cookie,
        SESSION_COOKIE_NAME=config.session_cookie_name
        or (
            "__Host-solarbank_session" if config.secure_cookie else "solarbank_session"
        ),
        REMEMBER_COOKIE_NAME=(
            (config.session_cookie_name or "solarbank_session") + "_remember"
        ),
        REMEMBER_COOKIE_SECURE=config.secure_cookie,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_PATH="/",
        REMEMBER_COOKIE_DOMAIN=None,
        SESSION_COOKIE_PATH="/",
        SESSION_COOKIE_DOMAIN=None,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=config.duration_seconds),
        SESSION_REFRESH_EACH_REQUEST=False,
        WTF_CSRF_TIME_LIMIT=config.duration_seconds,
    )
    configure_flask_session(
        app,
        cookie_name=app.config["SESSION_COOKIE_NAME"],
        secure=config.secure_cookie,
        lifetime_seconds=config.duration_seconds,
    )
    # Dash POST callbacks declared read-only by this factory are the only exemption.
    app.config["WTF_CSRF_CHECK_DEFAULT"] = False
    csrf = CSRFProtect(app)

    @app.before_request
    def browser_boundary():
        if request.path in SHORTCUT_STATIC_PATHS:
            return None
        if "_user_id" in session:
            issued = session.get("authenticated_at")
            if (
                not isinstance(issued, (int, float))
                or not 0 <= time.time() - issued < config.duration_seconds
            ):
                session.clear()
                flash("Bitte erneut anmelden.", "notice")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint:
            payload = (
                request.get_json(silent=True)
                if request.path == "/_dash-update-component"
                else None
            )
            if (
                request.method == "POST"
                and isinstance(payload, dict)
                and payload.get("output")
                in app.extensions.get("solarbank_read_callbacks", set())
            ):
                return None
            csrf.protect()

    manager = LoginManager()
    manager.session_protection = "basic"
    manager.init_app(app)

    @manager.user_loader
    def load_user(user_id: str) -> _DashboardUser | None:
        if not user_id:
            return None
        try:
            user = request_api(
                (
                    current_app.extensions.get("solarbank_settings")
                    or Settings.from_environment()
                ),
                "/v1/auth/session",
                payload={"id": user_id},
            )
        except ApiError as error:
            if error.status == 401:
                return None
            abort(503)
        return _DashboardUser(user)

    @app.before_request
    def require_authentication() -> Response | None:
        if request.path == "/logout":
            return None
        if request.path.startswith("/static/dist/"):
            return None
        if request.path in {
            "/healthz",
            "/login",
            *PUBLIC_PAGE_PATHS,
            *PUBLIC_STATIC_PATHS,
        }:
            return None
        if current_user.is_authenticated:
            if current_user.must_change_password and request.path != "/password":
                if request.method in {"GET", "HEAD"}:
                    return redirect("/password")
                return jsonify(error="Passwortwechsel erforderlich"), 403
            return None
        if request.method in {"GET", "HEAD"} and not request.path.startswith("/_dash-"):
            return redirect(url_for("solarbank_auth_login"))
        return jsonify(error="Anmeldung erforderlich"), 401

    @app.route("/login", methods=["GET", "POST"], endpoint="solarbank_auth_login")
    def login() -> Any:
        if current_user.is_authenticated:
            return redirect("/password" if current_user.must_change_password else "/")
        if request.content_length is not None and request.content_length > 16384:
            abort(413)
        form = LoginForm()
        if request.method == "GET":
            return _login_response(form)
        if not form.validate_on_submit():
            if form.csrf_token.errors:
                abort(400)
            return _login_response(form, "Anmeldung fehlgeschlagen.")
        client_key = hmac.new(
            config.secret_key.encode(),
            (request.remote_addr or "unknown").encode(),
            hashlib.sha256,
        ).hexdigest()
        try:
            user = request_api(
                (
                    current_app.extensions.get("solarbank_settings")
                    or Settings.from_environment()
                ),
                "/v1/auth/login",
                payload={
                    "username": form.username.data,
                    "password": form.password.data,
                    "client_key": client_key,
                },
            )
        except ApiError as error:
            if error.status == 401:
                return _login_response(form, "Anmeldung fehlgeschlagen.")
            if error.status == 429:
                response = app.make_response(
                    (
                        _login_response(
                            form, "Zu viele Versuche. Später erneut versuchen."
                        ),
                        429,
                    )
                )
                response.headers["Retry-After"] = str(error.retry_after)
                return response
            return _login_response(form, "Anmeldung nicht verfügbar."), 503
        session.clear()
        session["authenticated_at"] = time.time()
        login_user(_DashboardUser(user), remember=False, fresh=True)
        session.permanent = True
        return redirect("/password" if user.get("must_change_password") else "/")

    @app.route("/logout", methods=["GET", "POST"], endpoint="solarbank_auth_logout")
    def logout() -> Any:
        form = LogoutForm()
        # The confirmation page must render without revalidating a stale account.
        # Keep the stored session until the CSRF-protected POST succeeds.
        g._login_user = AnonymousUserMixin()
        if request.method == "GET":
            return render_template("logout.html", form=form)
        if not form.validate_on_submit():
            abort(400)
        token = session.get("_user_id")
        if token:
            try:
                request_api(
                    current_app.extensions.get("solarbank_settings")
                    or Settings.from_environment(),
                    "/v1/auth/logout",
                    payload={"id": token},
                )
            except ApiError:
                current_app.logger.warning(
                    "Abmeldung lokal beendet; API nicht erreichbar."
                )
        session.clear()
        flash("Abgemeldet.", "notice")
        return redirect(url_for("solarbank_auth_login"))

    def account_api(path, payload):
        return request_api(
            current_app.extensions.get("solarbank_settings")
            or Settings.from_environment(),
            path,
            payload={"id": current_user.get_id(), **payload},
        )

    @app.template_filter("login_time")
    def login_time(value):
        if not value:
            return "Noch nicht erfasst"
        return (
            datetime.fromisoformat(value)
            .astimezone(ZoneInfo("Europe/Berlin"))
            .strftime("%d.%m.%Y %H:%M:%S %Z")
        )

    @app.route("/password", methods=["GET", "POST"])
    def solarbank_password():
        form = PasswordForm()
        message, status = "", 200
        if request.method == "POST":
            if form.validate_on_submit():
                try:
                    user = account_api(
                        "/v1/auth/password",
                        {
                            "old_password": form.old_password.data,
                            "password": form.password.data,
                        },
                    )
                    issued = session.get("authenticated_at")
                    session.clear()
                    session["authenticated_at"] = issued
                    login_user(_DashboardUser(user), remember=False, fresh=True)
                    session.permanent = True
                    flash("Passwort geändert.", "notice")
                    return redirect("/password")
                except ApiError as error:
                    if error.status == 401:
                        session.clear()
                        return redirect("/login")
                    message = (
                        "Passwort konnte nicht geändert werden. Bitte Eingaben prüfen."
                        if error.status == 400
                        else "Änderung nicht bestätigt. Bitte erneut anmelden."
                    )
                    status = 400 if error.status == 400 else 503
            else:
                message, status = (
                    "Bitte Eingaben prüfen. Mindestens 12 Zeichen verwenden.",
                    400,
                )
        return render_template("password.html", form=form, message=message), status

    @app.route("/users", methods=["GET", "POST"])
    @app.route("/users/password-reset", methods=["GET", "POST"])
    def solarbank_users():
        if not current_user.can_manage_users:
            abort(403)
        resetting = request.path.endswith("password-reset")
        form = AccountForm()
        if resetting and request.method == "GET":
            form.username.data = request.args.get("username", "")
        message, status = "", 200
        if request.method == "POST":
            if form.validate_on_submit():
                try:
                    account_api(
                        "/v1/auth/accounts",
                        {
                            "operation": "reset" if resetting else "create",
                            "username": form.username.data,
                            "password": form.password.data,
                            "must_change_password": form.password_mode.data
                            == "initial",
                        },
                    )
                    flash(
                        "Passwort zurückgesetzt." if resetting else "Nutzer angelegt.",
                        "notice",
                    )
                    if (
                        resetting
                        and form.username.data.strip().casefold()
                        == current_user.username.casefold()
                    ):
                        session.clear()
                        return redirect("/login")
                    return redirect("/users")
                except ApiError as error:
                    if error.status in {401, 403}:
                        abort(error.status)
                    message = (
                        "Bitte Benutzername und Passwort prüfen."
                        if error.status == 400
                        else "Änderung nicht bestätigt. Bitte aktuellen Stand prüfen."
                    )
                    status = 400 if error.status == 400 else 503
            else:
                message, status = (
                    "Bitte Eingaben prüfen. Mindestens 12 Zeichen verwenden.",
                    400,
                )
        try:
            users = account_api("/v1/auth/accounts", {"operation": "list"})["users"]
        except ApiError as error:
            abort(error.status if error.status in {401, 403} else 503)
        return render_template(
            "users.html", form=form, users=users, resetting=resetting, message=message
        ), status

    state = AuthState(config, manager)
    app.extensions["solarbank_auth"] = state
    return state
