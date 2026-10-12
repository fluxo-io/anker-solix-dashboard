from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

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
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import InputRequired, Length

from .config import ConfigurationError, Settings
from .public_pages import PUBLIC_PAGE_PATHS, PUBLIC_STATIC_PATHS
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
    session_hours: int = 8
    trusted_proxy_count: int = 0
    trusted_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.enabled and len(self.secret_key) < 32:
            raise ConfigurationError("SECRET_KEY benötigt mindestens 32 Zeichen")
        if not 1 <= self.session_hours <= 168:
            raise ConfigurationError("SESSION_HOURS muss zwischen 1 und 168 liegen")
        if self.trusted_proxy_count not in {0, 1}:
            raise ConfigurationError("TRUST_PROXY muss true oder false sein")

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
            session_hours = int(os.getenv("SESSION_HOURS", "8"))
        except ValueError as error:
            raise ConfigurationError("SESSION_HOURS ist ungültig") from error
        return cls(
            enabled=_boolean_environment("AUTH_ENABLED", True),
            secret_key=os.getenv("SECRET_KEY", ""),
            secure_cookie=_boolean_environment("SESSION_COOKIE_SECURE", False),
            session_hours=session_hours,
            trusted_proxy_count=int(_boolean_environment("TRUST_PROXY", False)),
            trusted_hosts=(parsed.hostname, "localhost", "127.0.0.1") if origin else (),
        )


class LoginForm(FlaskForm):
    username = StringField(
        validators=[InputRequired(), Length(max=128)], render_kw={"maxlength": 128}
    )
    password = PasswordField(
        validators=[InputRequired(), Length(max=1024)], render_kw={"maxlength": 1024}
    )
    submit = SubmitField("Anmelden")


class LogoutForm(FlaskForm):
    submit = SubmitField("Abmelden")


class _DashboardUser(UserMixin):
    def __init__(self, user_id: str) -> None:
        self.id = user_id


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
            "img-src 'self'; font-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
            if public
            else "frame-ancestors 'none'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", policy)
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
        SESSION_COOKIE_NAME="__Host-solarbank_session"
        if config.secure_cookie
        else "solarbank_session",
        SESSION_COOKIE_PATH="/",
        SESSION_COOKIE_DOMAIN=None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.session_hours),
        SESSION_REFRESH_EACH_REQUEST=False,
        WTF_CSRF_TIME_LIMIT=config.session_hours * 3600,
    )
    manager = LoginManager()
    manager.session_protection = "strong"
    manager.init_app(app)

    @manager.user_loader
    def load_user(user_id: str) -> _DashboardUser | None:
        if not user_id:
            return None
        try:
            user = request_api(
                Settings.from_environment(), "/v1/auth/session", payload={"id": user_id}
            )
        except ApiError as error:
            if error.status == 401:
                return None
            abort(503)
        return _DashboardUser(str(user["id"]))

    @app.before_request
    def require_authentication() -> Response | None:
        if request.path in {
            "/healthz",
            "/login",
            *PUBLIC_PAGE_PATHS,
            *PUBLIC_STATIC_PATHS,
        }:
            return None
        if current_user.is_authenticated:
            if login_fresh():
                return None
            logout_user()
            session.clear()
        if request.method in {"GET", "HEAD"} and not request.path.startswith("/_dash-"):
            return redirect(url_for("solarbank_auth_login"))
        return jsonify(error="Anmeldung erforderlich"), 401

    @app.route("/login", methods=["GET", "POST"], endpoint="solarbank_auth_login")
    def login() -> Any:
        if current_user.is_authenticated and login_fresh():
            return redirect("/")
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
                Settings.from_environment(),
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
        login_user(_DashboardUser(str(user["id"])), remember=False, fresh=True)
        session.permanent = True
        return redirect("/")

    @app.route("/logout", methods=["GET", "POST"], endpoint="solarbank_auth_logout")
    def logout() -> Any:
        form = LogoutForm()
        if request.method == "GET":
            return render_template("logout.html", form=form)
        if not form.validate_on_submit():
            abort(400)
        logout_user()
        session.clear()
        flash("Abgemeldet.", "notice")
        return redirect(url_for("solarbank_auth_login"))

    state = AuthState(config, manager)
    app.extensions["solarbank_auth"] = state
    return state
