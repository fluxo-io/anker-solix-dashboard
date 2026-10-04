from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    """Raised when a dashboard setting is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name, "")
    if not value.strip():
        raise ConfigurationError(f"{name} fehlt")
    return value.strip()


def _secret(name: str) -> str:
    value = os.getenv(name, "")
    if not value or not value.strip():
        raise ConfigurationError(f"{name} fehlt")
    return value


def _port(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} muss eine ganze Zahl sein") from error
    if not 1 <= value <= 65_535:
        raise ConfigurationError(f"{name} muss zwischen 1 und 65535 liegen")
    return value


def _poll_interval() -> int:
    raw = os.getenv("POLL_INTERVAL_SECONDS", "300").strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(
            "POLL_INTERVAL_SECONDS muss eine ganze Zahl sein"
        ) from error
    if not 60 <= value <= 86_400:
        raise ConfigurationError(
            "POLL_INTERVAL_SECONDS muss zwischen 60 und 86400 liegen"
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    timezone_name: str
    poll_interval_seconds: int = 300
    source_timezone_name: str | None = None

    @classmethod
    def from_environment(cls) -> Settings:
        timezone_name = os.getenv("DASHBOARD_TIMEZONE", "Europe/Berlin").strip()
        source_timezone_name = os.getenv("ANKER_SOURCE_TIMEZONE", "").strip() or None
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ConfigurationError("DASHBOARD_TIMEZONE ist ungültig") from error
        if source_timezone_name:
            try:
                ZoneInfo(source_timezone_name)
            except ZoneInfoNotFoundError as error:
                raise ConfigurationError(
                    "ANKER_SOURCE_TIMEZONE ist ungültig"
                ) from error

        return cls(
            db_host=_required("DB_HOST"),
            db_port=_port("DB_PORT", 5432),
            db_name=_required("DB_NAME"),
            db_user=_required("DB_USER"),
            db_password=_secret("DB_PASSWORD"),
            timezone_name=timezone_name,
            poll_interval_seconds=_poll_interval(),
            source_timezone_name=source_timezone_name,
        )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def source_timezone(self) -> ZoneInfo | None:
        return (
            ZoneInfo(self.source_timezone_name) if self.source_timezone_name else None
        )
