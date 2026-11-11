"""Explicit offline migration of an existing API database."""

import argparse

from .config import Settings
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Datenbank verwalten")
    parser.add_argument("command", choices=["migrate"])
    parser.parse_args()
    Store(Settings.from_environment()).migrate()
    print("Datenbankmigration abgeschlossen.")


if __name__ == "__main__":
    main()
