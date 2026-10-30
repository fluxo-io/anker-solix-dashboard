"""Manifest-backed assets shared by public Jinja pages and Dash."""

import json
from pathlib import Path

from flask import url_for

DIST = Path(__file__).parent / "static" / "dist"


def install_assets(app):
    try:
        manifest = json.loads((DIST / "manifest.json").read_text())
        for key in ("public.js", "public.css", "dashboard.js", "dashboard.css"):
            path = (DIST / manifest[key]).resolve()
            if not path.is_relative_to(DIST.resolve()) or not path.is_file():
                raise ValueError(key)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "Frontend fehlt. npm --prefix frontend run build ausführen."
        ) from exc
    app.extensions["asset_manifest"] = manifest
    app.jinja_env.globals["asset"] = lambda key: url_for(
        "static", filename="dist/" + manifest[key]
    )
    return manifest
