from __future__ import annotations

import hmac
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from .config import Settings
from .store import Store
from .validation import (
    MAX_BATCH,
    filters,
    measurement,
    measurement_filter,
    text,
    timestamp,
)


def create_app(config_override: dict[str, Any] | Settings | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    if isinstance(config_override, Settings):
        settings = config_override
    elif config_override is not None:
        app.config.update(config_override)
        settings = Settings(
            db_path=Path(app.config.get("DATA_DIR", "/data")) / "app.sqlite3",
            api_token=app.config.get("API_TOKEN", ""),
            secret_key=app.config.get("SECRET_KEY", ""),
            admin_username=app.config.get("INIT_ADMIN_USERNAME", "").strip(),
            admin_password=app.config.get("INIT_ADMIN_PASSWORD", ""),
            admin_password_hash=app.config.get("INIT_ADMIN_PASSWORD_HASH", ""),
        )
    else:
        settings = Settings.from_environment()
    store = Store(settings)
    store.initialize()
    app.extensions["solarbank_store"] = store

    @app.before_request
    def require_api_token() -> tuple[Response, int] | None:
        if request.path == "/healthz" and request.method in {"GET", "HEAD"}:
            return None
        scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.encode(), settings.api_token.encode()
        ):
            response = jsonify(error="Anmeldung erforderlich")
            response.headers["WWW-Authenticate"] = "Bearer"
            return response, 401
        return None

    @app.after_request
    def response_headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(ValueError)
    def invalid_request(error: ValueError) -> tuple[Response, int]:
        return jsonify(error=str(error)), 400

    @app.errorhandler(sqlite3.Error)
    def storage_unavailable(error: sqlite3.Error) -> tuple[Response, int]:
        app.logger.error("Datenbank nicht verfügbar: %s", type(error).__name__)
        return jsonify(error="Datenbank nicht verfügbar"), 503

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> tuple[Response, int]:
        return jsonify(error=error.name), error.code or 500

    @app.get("/healthz")
    def health() -> tuple[Response, int]:
        ready = store.ready()
        return jsonify(status="ok" if ready else "unavailable"), 200 if ready else 503

    @app.get("/v1/devices")
    def devices() -> Response:
        return jsonify(rows=store.devices())

    @app.get("/v1/measurements")
    def measurements() -> Response:
        return jsonify(rows=store.measurements(*measurement_filter(request.args)))

    @app.get("/v1/measurements/latest")
    def latest() -> Response:
        return jsonify(rows=store.latest(*filters(request.args)))

    @app.get("/v1/snapshot")
    def snapshot() -> Response:
        result = store.snapshot(
            timestamp(request.args.get("timestamp"), "timestamp"),
            text(request.args.get("site_id"), "site_id"),
            text(request.args.get("device_sn"), "device_sn"),
        )
        return jsonify(snapshot=result)

    @app.get("/v1/collector-state")
    def collector_state() -> Response:
        return jsonify(state=store.collector_state())

    def body() -> dict[str, Any]:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Anfrage muss ein Objekt sein")
        return data

    @app.post("/v1/collector/attempt")
    def attempt() -> Response:
        store.mark_attempt(timestamp(body().get("attempted_at"), "attempted_at"))
        return jsonify(ok=True)

    @app.post("/v1/collector/failure")
    def failure() -> Response:
        error = body().get("error")
        if not isinstance(error, str):
            raise ValueError("error muss Text enthalten")
        store.mark_failure(error)
        return jsonify(ok=True)

    @app.post("/v1/measurements")
    def save() -> Response:
        rows = body().get("rows")
        if not isinstance(rows, list) or len(rows) > MAX_BATCH:
            raise ValueError(f"rows muss bis zu {MAX_BATCH} Messungen enthalten")
        return jsonify(written=store.save([measurement(row) for row in rows]))

    @app.post("/v1/auth/login")
    def login() -> tuple[Response, int] | Response:
        data = body()
        username = text(data.get("username"), "username", limit=128)
        client_key = text(data.get("client_key"), "client_key", limit=256)
        password = data.get("password")
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise ValueError("password ist ungültig")
        user, retry_after = store.login(username, password, client_key)
        if retry_after is not None:
            response = jsonify(error="Zu viele Versuche")
            response.headers["Retry-After"] = str(retry_after)
            return response, 429
        if user is None:
            return jsonify(error="Anmeldung fehlgeschlagen"), 401
        return jsonify(user)

    @app.post("/v1/auth/session")
    def validate_session() -> tuple[Response, int] | Response:
        user_id = body().get("id")
        if not isinstance(user_id, str) or len(user_id) != 64:
            return jsonify(error="Anmeldung erforderlich"), 401
        user = store.session_user(user_id)
        if user is None:
            return jsonify(error="Anmeldung erforderlich"), 401
        return jsonify(user)

    return app
