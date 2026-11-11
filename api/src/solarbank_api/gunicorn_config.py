"""Initialize new data before forking API workers; existing data is only checked."""

from .config import Settings
from .store import Store


def on_starting(server):
    Store(Settings.from_environment()).initialize()
