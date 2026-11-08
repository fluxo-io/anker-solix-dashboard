import pytest
from solarbank_dashboard.config import ConfigurationError, Settings


def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "API_TOKEN": "t" * 32,
    }
    for name in [
        *values,
        "API_URL",
        "DASHBOARD_TIMEZONE",
        "POLL_INTERVAL_SECONDS",
        "ANKER_SOURCE_TIMEZONE",
        "API_DASHBOARD_TIMEOUT_SECONDS",
        "API_TIMEOUT_SECONDS",
    ]:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)
    settings = Settings.from_environment()

    assert settings.api_url == "http://api:8001"
    assert settings.timezone_name == "Europe/Berlin"
    assert settings.poll_interval_seconds == 300
    assert settings.api_token == "t" * 32
    assert settings.source_timezone is None
    assert settings.api_timeout_seconds == 5
    assert settings.api_dashboard_timeout_seconds == 20


@pytest.mark.parametrize("value", ["0", "31", "invalid"])
def test_dashboard_timeout_has_separate_finite_budget(monkeypatch, value):
    _environment(monkeypatch)
    monkeypatch.setenv("API_DASHBOARD_TIMEOUT_SECONDS", value)
    with pytest.raises(ConfigurationError, match="API_DASHBOARD_TIMEOUT_SECONDS"):
        Settings.from_environment()


def test_dashboard_timeout_does_not_change_normal_timeout(monkeypatch):
    _environment(monkeypatch)
    monkeypatch.setenv("API_DASHBOARD_TIMEOUT_SECONDS", "15")
    config = Settings.from_environment()
    assert config.api_dashboard_timeout_seconds == 15
    assert config.api_timeout_seconds == 5


def test_explicit_source_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("ANKER_SOURCE_TIMEZONE", "Europe/Berlin")
    assert str(Settings.from_environment().source_timezone) == "Europe/Berlin"
    monkeypatch.setenv("ANKER_SOURCE_TIMEZONE", "Nowhere/Unknown")
    with pytest.raises(ConfigurationError, match="ANKER_SOURCE_TIMEZONE"):
        Settings.from_environment()


def test_invalid_timezone_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("DASHBOARD_TIMEZONE", "Nowhere/Unknown")

    with pytest.raises(ConfigurationError, match="DASHBOARD_TIMEZONE"):
        Settings.from_environment()


def test_invalid_poll_interval_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "30")

    with pytest.raises(ConfigurationError, match="POLL_INTERVAL_SECONDS"):
        Settings.from_environment()


def test_secret_is_not_shown_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch)

    assert "t" * 32 not in repr(Settings.from_environment())
