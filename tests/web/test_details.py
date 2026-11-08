from datetime import UTC, datetime

from solarbank_dashboard.config import Settings
from solarbank_dashboard.details import (
    freshness_view,
    redact_snapshot,
    temperature_fields,
)


def settings() -> Settings:
    return Settings(
        "http://api:8001",
        "token",
        "Europe/Berlin",
        source_timezone_name="Europe/Berlin",
    )


def test_freshness_renders_api_status_without_reassessing_clock() -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    row = {
        "device_sn": "test",
        "collected_at": now,
        "source_updated_at": "2026-09-22 13:00:00",
        "data_valid": True,
        "status": "stale",
        "source_timestamp": None,
    }
    text, css, _table = freshness_view(
        {"rows": [row], "warning": True, "oldest_timestamp": None}, settings()
    )
    assert text == "Veraltet"
    assert css == "status warning"


def test_snapshot_redacts_nested_secrets_and_keeps_temperature_raw_values() -> None:
    raw = {
        "device": {
            "battery_temp": 260,
            "access_token": "secret",
            "modules": [{"temperature": 27, "password": "secret"}],
        }
    }
    safe = redact_snapshot(raw)
    assert safe["device"]["access_token"] == "***"
    assert safe["device"]["modules"][0]["password"] == "***"
    assert raw["device"]["access_token"] == "secret"
    assert temperature_fields(safe) == [
        ("device.battery_temp", 260),
        ("device.modules[0].temperature", 27),
    ]
