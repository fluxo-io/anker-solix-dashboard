"""Manifest-backed assets shared by public Jinja pages and Dash."""

import json
from pathlib import Path

from flask import current_app, has_app_context, url_for

DIST = Path(__file__).parent / "static" / "dist"


def load_design_tokens(manifest):
    path = (DIST / manifest["design-tokens.json"]).resolve()
    if not path.is_relative_to(DIST.resolve()) or not path.is_file():
        raise ValueError("design-tokens.json")
    tokens = json.loads(path.read_text())
    if not isinstance(tokens, dict):
        raise ValueError("Invalid design tokens")
    for key in ("font-body-name", "text", "muted", "line", "soft", "surface"):
        if not isinstance(tokens.get(key), str) or not tokens[key].strip():
            raise ValueError(f"Design token missing: {key}")
    return tokens


def design_tokens():
    """Read the pinned build output; never use machine-local design sources."""
    if has_app_context() and "design_tokens" in current_app.extensions:
        return current_app.extensions["design_tokens"]
    return load_design_tokens(json.loads((DIST / "manifest.json").read_text()))


def install_assets(app):
    try:
        manifest = json.loads((DIST / "manifest.json").read_text())
        for key in ("public.js", "public.css", "dashboard.js", "dashboard.css"):
            path = (DIST / manifest[key]).resolve()
            if not path.is_relative_to(DIST.resolve()) or not path.is_file():
                raise ValueError(key)
        tokens = load_design_tokens(manifest)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "Frontend fehlt. npm --prefix frontend run build ausführen."
        ) from exc
    app.extensions["asset_manifest"] = manifest
    app.extensions["design_tokens"] = tokens
    app.jinja_env.globals["asset"] = lambda key: url_for(
        "static", filename="dist/" + manifest[key]
    )
    return manifest
