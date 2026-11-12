from __future__ import annotations

import hmac
import json
import sqlite3
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from uuid import uuid4

from flask import Flask, Response, g, jsonify, request
from fluxo_auth import session_seconds
from werkzeug.exceptions import HTTPException

from .config import Settings, boolean
from .dashboard import MAX_DASHBOARD_BYTES, dashboard
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
            collector_api_token=app.config.get("COLLECTOR_API_TOKEN", ""),
            secret_key=app.config.get("SECRET_KEY", ""),
            admin_username=app.config.get("INIT_ADMIN_USERNAME", "").strip(),
            admin_password=app.config.get("INIT_ADMIN_PASSWORD", ""),
            admin_password_hash=app.config.get("INIT_ADMIN_PASSWORD_HASH", ""),
            admin_require_password_change=boolean(
                app.config.get("INIT_ADMIN_REQUIRE_PASSWORD_CHANGE", True),
                "INIT_ADMIN_REQUIRE_PASSWORD_CHANGE",
            ),
            session_lifetime_seconds=session_seconds(app.config),
            timezone_name=app.config.get("DASHBOARD_TIMEZONE", "Europe/Berlin"),
            analytics_timezone=app.config.get("ANALYTICS_TIMEZONE", "Europe/Berlin"),
            analytics_excluded_users=app.config.get("ANALYTICS_EXCLUDED_USERS", ""),
            poll_interval_seconds=int(app.config.get("POLL_INTERVAL_SECONDS", 300)),
            source_timezone_name=app.config.get("ANKER_SOURCE_TIMEZONE") or None,
        )
    else:
        settings = Settings.from_environment()
    store = Store(settings)
    store.initialize()
    app.extensions["solarbank_store"] = store
    # At most one CPU/memory-heavy evaluation per worker. Gunicorn's remaining
    # threads stay available for health/auth/collector operations; never queue
    # expensive calculations until every request thread is occupied.
    dashboard_slots = BoundedSemaphore(1)

    @app.before_request
    def require_api_token() -> tuple[Response, int] | None:
        if request.path == "/healthz" and request.method in {"GET", "HEAD"}:
            return None
        scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
        supplied_bytes = supplied.encode()
        web = scheme.lower() == "bearer" and hmac.compare_digest(
            supplied_bytes, settings.api_token.encode()
        )
        collector = (
            scheme.lower() == "bearer"
            and bool(settings.collector_api_token)
            and hmac.compare_digest(
                supplied_bytes, settings.collector_api_token.encode()
            )
        )
        if not web and not collector:
            response = jsonify(error="Anmeldung erforderlich")
            response.headers["WWW-Authenticate"] = "Bearer"
            return response, 401
        # Explicit service actors: new operations do not inherit authorization.
        actor = "collector" if collector else "web"
        allowed = {
            "web": {
                (method, endpoint)
                for method in ("GET", "HEAD")
                for endpoint in (
                    "devices",
                    "dashboard_view",
                    "measurements",
                    "latest",
                    "snapshot",
                    "collector_state",
                )
            }
            | {
                ("POST", "login"),
                ("POST", "validate_session"),
                ("POST", "logout"),
                ("POST", "manage_accounts"),
                ("POST", "change_password"),
                ("POST", "analytics_report"),
                ("POST", "activity_report"),
                ("POST", "activity_event"),
            },
            "collector": {
                ("POST", "attempt"),
                ("POST", "failure"),
                ("POST", "save"),
                ("GET", "collector_ready"),
                ("HEAD", "collector_ready"),
            },
        }
        if (request.method, request.endpoint) not in allowed[actor]:
            return jsonify(error="Zugriff nicht erlaubt"), 403
        g.service_actor = actor
        return None

    @app.after_request
    def response_headers(response: Response) -> Response:
        if response.status_code >= 400 and response.is_json:
            payload = response.get_json(silent=True)
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                payload["error"] = {
                    "code": {
                        400: "invalid_request",
                        401: "authentication_required",
                        403: "forbidden",
                        404: "not_found",
                        429: "rate_limited",
                        503: "unavailable",
                    }.get(response.status_code, "request_failed"),
                    "message": payload["error"],
                }
                payload["request_id"] = uuid4().hex
                response.set_data(json.dumps(payload, ensure_ascii=False))
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

    @app.get("/v1/collector/ready")
    def collector_ready() -> tuple[Response, int]:
        # Checks the write actor before any source request or mutation is attempted.
        return health()

    @app.get("/v1/dashboard")
    def dashboard_view() -> Response:
        if not dashboard_slots.acquire(blocking=False):
            response = jsonify(error="Daten werden berechnet. Bitte erneut versuchen.")
            response.status_code = 503
            response.headers["Retry-After"] = "1"
            return response
        try:
            result = dashboard(store, settings, request.args)
            encoded = json.dumps(
                result, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            if len(encoded) > MAX_DASHBOARD_BYTES:
                raise ValueError("Zu viele Werte. Bitte Zeitraum verkürzen.")
            return Response(encoded, mimetype="application/json")
        finally:
            dashboard_slots.release()

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

    def activity_filters(data):
        for key in ("id", "range", "bucket", "user", "event", "q", "target"):
            if key in data and (not isinstance(data[key], str) or len(data[key]) > 254):
                raise ValueError("Ungültiger Filter.")
        if (
            type(data.get("page", 1)) is not int
            or not 1 <= data.get("page", 1) <= 1000000
        ):
            raise ValueError("Ungültige Seite.")
        return data

    def report_response(usage):
        data = activity_filters(body())
        try:
            return jsonify(store.activity_report(data.get("id", ""), data, usage=usage))
        except PermissionError:
            return jsonify(error="Zugriff nicht erlaubt"), 403

    @app.post("/v1/analytics")
    def analytics_report():
        return report_response(True)

    @app.post("/v1/activity")
    def activity_report():
        return report_response(False)

    @app.post("/v1/activity/events")
    def activity_event():
        data = activity_filters(body())
        try:
            return jsonify(
                ok=store.record_usage(
                    data.get("id", ""), data.get("event", ""), data.get("target", "")
                )
            )
        except PermissionError:
            return jsonify(error="Anmeldung erforderlich"), 401

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
        username = text(data.get("username"), "username", limit=254)
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
        if (
            not isinstance(user_id, str)
            or not user_id.startswith("fa1_")
            or len(user_id) != 47
        ):
            return jsonify(error="Anmeldung erforderlich"), 401
        user = store.session_user(user_id)
        if user is None:
            return jsonify(error="Anmeldung erforderlich"), 401
        return jsonify(user)

    @app.post("/v1/auth/logout")
    def logout() -> Response:
        token = body().get("id")
        return jsonify(ok=store.logout(token) if isinstance(token, str) else False)

    @app.post("/v1/auth/accounts")
    def manage_accounts() -> tuple[Response, int] | Response:
        data = body()
        try:
            return jsonify(
                store.manage_accounts(
                    data.get("id", ""), data.get("operation", "list"), data
                )
            )
        except PermissionError:
            return jsonify(error="Zugriff nicht erlaubt"), 403

    @app.post("/v1/auth/password")
    def change_password() -> tuple[Response, int] | Response:
        data = body()
        try:
            return jsonify(
                store.change_password(
                    data.get("id", ""), data.get("old_password"), data.get("password")
                )
            )
        except PermissionError:
            return jsonify(error="Anmeldung erforderlich"), 401

    return app
