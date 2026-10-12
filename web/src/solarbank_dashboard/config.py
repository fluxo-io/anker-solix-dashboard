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
    api_url: str
    api_token: str = field(repr=False)
    timezone_name: str
    poll_interval_seconds: int = 300
    source_timezone_name: str | None = None

    api_timeout_seconds: int = 5

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
            api_timeout_seconds=_api_timeout(),
            api_url=_api_url(),
            api_token=_api_token(),
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
