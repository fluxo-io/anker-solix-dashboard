"""Admin activity views; all records and policies belong to the API."""

from flask import abort, current_app, render_template, request
from flask_login import current_user

from .repository import ApiError, request_api

RANGES = {
    "1": "Letzte 24 Stunden",
    "7": "Letzte 7 Tage",
    "30": "Letzte 30 Tage",
    "90": "Letzte 90 Tage",
    "all": "Gesamter Zeitraum",
}
BUCKETS = {"hour": "Stunde", "day": "Tag", "week": "Woche"}
EVENTS = {
    "bootstrap": "Einrichtung",
    "login_success": "Anmeldung",
    "login_failed": "Anmeldung fehlgeschlagen",
    "logout": "Abmeldung",
    "user_create": "Benutzer angelegt",
    "password_reset": "Passwort zurückgesetzt",
    "password_change": "Passwort geändert",
    "page_view": "Seitenaufruf",
    "access_denied": "Zugriff abgewiesen",
    "export_csv": "CSV-Export",
}
PAGES = {
    "/",
    "/password",
    "/users",
    "/users/password-reset",
    "/admin/analytics",
    "/admin/activity",
}


def register_activity(app, *, enabled):
    def report(usage):
        if not enabled:
            abort(404)
        if not current_user.is_authenticated or not current_user.can_manage_users:
            abort(403)
        payload = {
            key: request.args.get(key, "") for key in ("user", "event", "q", "bucket")
        }
        payload.update(id=current_user.get_id(), range=request.args.get("range", "30"))
        try:
            payload["page"] = int(request.args.get("page", "1"))
        except ValueError:
            abort(400)
        try:
            data = request_api(
                current_app.extensions["solarbank_settings"],
                "/v1/analytics" if usage else "/v1/activity",
                payload=payload,
            )
        except ApiError as error:
            if error.status in {400, 401, 403}:
                abort(error.status)
            return render_template(
                "activity.html",
                usage=usage,
                data=None,
                ranges=RANGES,
                events=EVENTS,
                buckets=BUCKETS,
            ), 503
        return render_template(
            "activity.html",
            usage=usage,
            data=data,
            ranges=RANGES,
            events=EVENTS,
            buckets=BUCKETS,
        )

    app.add_url_rule("/admin/analytics", "solarbank_analytics", lambda: report(True))
    app.add_url_rule("/admin/activity", "solarbank_activity", lambda: report(False))

    @app.after_request
    def record_page(response):
        if (
            enabled
            and request.method == "GET"
            and request.path in PAGES
            and response.status_code in {200, 403}
            and current_user.is_authenticated
        ):
            try:
                request_api(
                    current_app.extensions["solarbank_settings"],
                    "/v1/activity/events",
                    payload={
                        "id": current_user.get_id(),
                        "event": "page_view"
                        if response.status_code == 200
                        else "access_denied",
                        "target": request.path,
                    },
                )
            except ApiError:
                current_app.logger.warning("Seitenereignis nicht gespeichert.")
        return response
