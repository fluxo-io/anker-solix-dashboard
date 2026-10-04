from datetime import UTC, datetime, timedelta

import pandas as pd

from solarbank_dashboard.config import Settings
from solarbank_dashboard.details import (
    freshness_view,
    redact_snapshot,
    source_frame,
    temperature_fields,
)


def settings() -> Settings:
    return Settings(
        "database",
        5432,
        "solarbank",
        "reader",
        "secret",
        "Europe/Berlin",
        source_timezone_name="Europe/Berlin",
    )


def test_fresh_fetch_does_not_hide_stale_cloud_measurement() -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    row = {
        "device_sn": "test",
        "collected_at": now,
        "source_updated_at": "2026-09-22 13:00:00",
        "data_valid": True,
    }
    text, css, _table = freshness_view([row], settings(), now=now)
    assert text == "Veraltet"
    assert css == "status warning"


def test_historical_eligibility_compares_source_with_collection_time() -> None:
    collected = datetime(2026, 9, 22, 12, tzinfo=UTC)
    frame = pd.DataFrame(
        [
            {
                "collected_at": collected,
                "source_updated_at": "2026-09-22 14:00:00",
                "data_valid": True,
            },
            {
                "collected_at": collected + timedelta(hours=1),
                "source_updated_at": "2026-09-22 14:00:00",
                "data_valid": True,
            },
        ]
    )
    assert source_frame(frame, settings())["source_stale"].tolist() == [False, True]


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
