from __future__ import annotations

from datetime import date

from flask import Blueprint, Flask, render_template
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

PUBLIC_PAGE_PATHS = frozenset({"/datenschutz", "/impressum"})
PUBLIC_STATIC_PATHS = frozenset(
    {
        "/static/brand.css",
        "/static/fonts/anonymous-pro-v14-latin-regular.woff2",
        "/static/fonts/open-sans-v18-latin-regular.woff2",
        "/static/img/logo-fluxo_io.svg",
        "/static/toasts.css",
        "/static/toasts.js",
    }
)

public_pages = Blueprint(
    "solarbank_public",
    __name__,
    template_folder="templates",
)


@public_pages.get("/impressum")
def imprint() -> str:
    return render_template("imprint.html")


@public_pages.get("/datenschutz")
def privacy() -> str:
    return render_template("privacy.html")


def bad_request(_error: BadRequest) -> tuple[str, int]:
    return (
        render_template(
            "error.html",
            error_title="Anfrage ungültig",
            error_message="Bitte erneut versuchen.",
        ),
        400,
    )


def request_too_large(_error: RequestEntityTooLarge) -> tuple[str, int]:
    return (
        render_template(
            "error.html",
            error_title="Anfrage zu groß",
            error_message="Bitte Eingabe prüfen und erneut versuchen.",
        ),
        413,
    )


def register_public_pages(app: Flask) -> None:
    """Register the public legal pages once on the dashboard server."""

    if app.extensions.get("solarbank_public_pages"):
        return
    app.register_blueprint(public_pages)
    app.register_error_handler(BadRequest, bad_request)
    app.register_error_handler(RequestEntityTooLarge, request_too_large)

    @app.context_processor
    def public_template_context() -> dict[str, int]:
        return {"current_year": date.today().year}

    app.extensions["solarbank_public_pages"] = True
