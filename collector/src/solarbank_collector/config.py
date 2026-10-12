from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field


class ConfigurationError(ValueError):
    """Raised when an environment setting is missing or invalid."""


def _required(name: str) -> str:
    raw = os.getenv(name, "")
    if not raw.strip():
        raise ConfigurationError(f"{name} fehlt")
    return raw.strip()


def _secret(name: str) -> str:
    value = os.getenv(name, "")
    if not value or not value.strip():
        raise ConfigurationError(f"{name} fehlt")
    return value


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} muss eine ganze Zahl sein") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} muss zwischen {minimum} und {maximum} liegen")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} muss true oder false sein")


def _api_timeout() -> int:
    try:
        value = int(os.getenv("API_TIMEOUT_SECONDS", "5"))
    except ValueError as error:
        raise ConfigurationError("API_TIMEOUT_SECONDS ist ungültig") from error
    if not 1 <= value <= 60:
        raise ConfigurationError("API_TIMEOUT_SECONDS muss zwischen 1 und 60 liegen")
    return value


def _api_url() -> str:
    from urllib.parse import urlsplit

    value = os.getenv("API_URL", "http://api:8001").rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("API_URL ist ungültig")
    return value


def _api_token() -> str:
    value = _secret("API_TOKEN")
    if len(value) < 32 or any(c.isspace() for c in value):
        raise ConfigurationError(
            "API_TOKEN benötigt mindestens 32 Zeichen ohne Leerzeichen"
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    anker_username: str
    anker_password: str = field(repr=False)
    anker_country: str
    anker_device_sn: str | None
    anker_payload_encryption: bool
    poll_interval_seconds: int
    api_request_timeout_seconds: int
    max_retries: int
    log_level: str
    api_url: str
    api_token: str = field(repr=False)

    api_timeout_seconds: int = 5

    @classmethod
    def from_environment(cls) -> Settings:
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in logging.getLevelNamesMapping():
            raise ConfigurationError("LOG_LEVEL ist ungültig")

        device_sn = os.getenv("ANKER_DEVICE_SN", "").strip() or None
        country = os.getenv("ANKER_COUNTRY", "DE").strip().upper()
        if len(country) != 2 or not country.isascii() or not country.isalpha():
            raise ConfigurationError(
                "ANKER_COUNTRY muss ein zweistelliger Ländercode sein"
            )

        return cls(
            anker_username=_required("ANKER_USERNAME"),
            anker_password=_secret("ANKER_PASSWORD"),
            anker_country=country,
            anker_device_sn=device_sn,
            anker_payload_encryption=_boolean("ANKER_PAYLOAD_ENCRYPTION", False),
            poll_interval_seconds=_integer(
                "POLL_INTERVAL_SECONDS", 300, minimum=60, maximum=86_400
            ),
            api_request_timeout_seconds=_integer(
                "API_REQUEST_TIMEOUT_SECONDS", 90, minimum=10, maximum=120
            ),
            max_retries=_integer("MAX_RETRIES", 2, minimum=0, maximum=10),
            log_level=log_level,
            api_timeout_seconds=_api_timeout(),
            api_url=_api_url(),
            api_token=_api_token(),
        )
