from __future__ import annotations

import logging
import os
from dataclasses import dataclass


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


@dataclass(frozen=True, slots=True)
class Settings:
    anker_username: str
    anker_password: str
    anker_country: str
    anker_device_sn: str | None
    anker_payload_encryption: bool
    poll_interval_seconds: int
    api_request_timeout_seconds: int
    max_retries: int
    log_level: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

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
            db_host=_required("DB_HOST"),
            db_port=_integer("DB_PORT", 5432, minimum=1, maximum=65_535),
            db_name=_required("DB_NAME"),
            db_user=_required("DB_USER"),
            db_password=_secret("DB_PASSWORD"),
        )
