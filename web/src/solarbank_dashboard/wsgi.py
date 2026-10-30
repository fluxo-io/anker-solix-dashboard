"""Gunicorn entry point, separated from the importable factory."""

from .app import create_app

app = create_app()
server = app.server
