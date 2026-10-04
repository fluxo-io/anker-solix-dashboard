import pytest

from solarbank_collector.config import ConfigurationError, Settings

REQUIRED = {
    "ANKER_USERNAME": "owner@example.com",
    "ANKER_PASSWORD": "secret",
    "DB_HOST": "database",
    "DB_NAME": "solarbank",
    "DB_USER": "solarbank",
    "DB_PASSWORD": "db-secret",
}


def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(REQUIRED) + [
        "ANKER_COUNTRY",
        "ANKER_DEVICE_SN",
        "ANKER_PAYLOAD_ENCRYPTION",
        "POLL_INTERVAL_SECONDS",
        "API_REQUEST_TIMEOUT_SECONDS",
        "MAX_RETRIES",
        "LOG_LEVEL",
        "DB_PORT",
    ]:
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)
    settings = Settings.from_environment()

    assert settings.anker_country == "DE"
    assert settings.anker_device_sn is None
    assert settings.anker_payload_encryption is False
    assert settings.poll_interval_seconds == 300
    assert settings.db_port == 5432


def test_optional_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("ANKER_COUNTRY", "at")
    monkeypatch.setenv("ANKER_DEVICE_SN", " SN123 ")
    monkeypatch.setenv("ANKER_PAYLOAD_ENCRYPTION", "yes")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "600")

    settings = Settings.from_environment()

    assert settings.anker_country == "AT"
    assert settings.anker_device_sn == "SN123"
    assert settings.anker_payload_encryption is True
    assert settings.poll_interval_seconds == 600


def test_short_poll_interval_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "30")

    with pytest.raises(ConfigurationError, match="POLL_INTERVAL_SECONDS"):
        Settings.from_environment()


def test_missing_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)
    monkeypatch.delenv("ANKER_PASSWORD")

    with pytest.raises(ConfigurationError, match="ANKER_PASSWORD"):
        Settings.from_environment()
