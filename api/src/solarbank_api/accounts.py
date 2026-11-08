"""Interactive account maintenance using the same authoritative auth store."""

from __future__ import annotations

import argparse
from getpass import getpass

from .config import Settings
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Passwort eines Kontos ändern")
    parser.add_argument("username")
    args = parser.parse_args()
    password = getpass("Neues Passwort: ")
    if password != getpass("Passwort wiederholen: "):
        parser.error("Die Passwörter stimmen nicht überein.")
    store = Store(Settings.from_environment())
    # Do not initialize/recreate a missing database during a password reset.
    try:
        store.reset_password(args.username, password)
    except ValueError as error:
        parser.error(str(error))
    print("Passwort geändert.")


if __name__ == "__main__":
    main()
