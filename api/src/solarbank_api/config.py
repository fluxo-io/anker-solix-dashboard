from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from pathlib import Path


def _valid_password_hash(value: str) -> bool:
    if len(value) > 1024:
        return False
    try:
        method, salt, digest = value.split("$")
        parts = method.split(":")
        if parts[0] == "scrypt" and len(parts) == 4:
            n, r, p = (int(part) for part in parts[1:])
            valid_method = (
                32768 <= n <= 262144
                and n & (n - 1) == 0
                and 1 <= r <= 8
                and 1 <= p <= 4
            )
            digest_length = 128
        elif parts[0] == "pbkdf2" and len(parts) == 3:
            valid_method = (
                parts[1] in {"sha256", "sha512"} and 600000 <= int(parts[2]) <= 10000000
            )
            digest_length = 64 if parts[1] == "sha256" else 128
        else:
            return False
    except (ValueError, TypeError):
        return False
    return (
        valid_method
        and 8 <= len(salt) <= 128
        and all(character in string.ascii_letters + string.digits for character in salt)
        and len(digest) == digest_length
        and all(character in string.hexdigits for character in digest)
    )


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    api_token: str = field(repr=False)
    secret_key: str = field(repr=False)
    admin_username: str = ""
    admin_password: str = field(default="", repr=False)
    admin_password_hash: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.db_path.is_absolute():
            raise ValueError("DATA_DIR muss ein absoluter Pfad sein")
        if len(self.api_token.encode()) < 32 or not self.api_token.strip():
            raise ValueError("API_TOKEN muss mindestens 32 Zeichen lang sein")
        if any(character.isspace() for character in self.api_token):
            raise ValueError("API_TOKEN darf keine Leerzeichen enthalten")
        if len(self.secret_key.encode()) < 32 or not self.secret_key.strip():
            raise ValueError("SECRET_KEY muss mindestens 32 Zeichen lang sein")
        if self.admin_username and (
            len(self.admin_username) > 128
            or any(ord(character) < 32 for character in self.admin_username)
        ):
            raise ValueError("INIT_ADMIN_USERNAME ist ungültig")
        if self.admin_password and not 12 <= len(self.admin_password) <= 1024:
            raise ValueError("INIT_ADMIN_PASSWORD muss 12 bis 1024 Zeichen lang sein")
        if (
            not self.admin_password
            and self.admin_password_hash
            and not (_valid_password_hash(self.admin_password_hash))
        ):
            raise ValueError("INIT_ADMIN_PASSWORD_HASH muss ein sicherer Hash sein")

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            db_path=Path(os.getenv("DATA_DIR", "/data")) / "app.sqlite3",
            api_token=os.getenv("API_TOKEN", ""),
            secret_key=os.getenv("SECRET_KEY", ""),
            admin_username=os.getenv("INIT_ADMIN_USERNAME", "").strip(),
            admin_password=os.getenv("INIT_ADMIN_PASSWORD", ""),
            admin_password_hash=os.getenv("INIT_ADMIN_PASSWORD_HASH", ""),
        )
