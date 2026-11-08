from datetime import UTC, datetime

import pandas as pd
from solarbank_api.aggregation import aggregate_measurements


def test_aggregation_sums_devices_and_averages_state_of_charge() -> None:
    timestamp = datetime(2026, 9, 21, 12, tzinfo=UTC)
    frame = pd.DataFrame(
        [
            {
                "bucket_start": timestamp,
                "site_id": "site-a",
                "pv_reported_total_w": 100.0,
                "pv_calculated_total_w": 99.0,
                "pv_1_w": 100.0,
                "battery_soc_pct": 60.0,
                "home_load_w": 150.0,
            },
            {
                "bucket_start": timestamp,
                "site_id": "site-a",
                "pv_reported_total_w": None,
                "pv_calculated_total_w": 50.0,
                "pv_1_w": 50.0,
                "battery_soc_pct": 80.0,
                "home_load_w": 150.0,
            },
            {
                "bucket_start": timestamp,
                "site_id": "site-b",
                "pv_reported_total_w": 25.0,
                "pv_calculated_total_w": 25.0,
                "pv_1_w": 25.0,
                "battery_soc_pct": 70.0,
                "home_load_w": 75.0,
            },
        ]
    )

    result = aggregate_measurements(frame)

    assert result.loc[0, "pv_total_w"] == 175.0
    assert result.loc[0, "pv_1_w"] == 175.0
    assert result.loc[0, "home_load_w"] == 225.0
    assert result.loc[0, "battery_soc_pct"] == 70.0


def test_missing_device_does_not_turn_partial_power_into_total() -> None:
    frame = pd.DataFrame(
        [
            {
                "bucket_start": datetime(2026, 9, 21, 12, tzinfo=UTC),
                "site_id": "site-a",
                "device_sn": "a",
                "pv_reported_total_w": 500.0,
                "pv_calculated_total_w": None,
                "home_load_w": 100.0,
                "battery_soc_pct": 50.0,
            }
        ]
    )
    result = aggregate_measurements(
        frame, expected_devices=[("site-a", "a"), ("site-a", "b")]
    )
    assert pd.isna(result.loc[0, "pv_total_w"])
    assert pd.isna(result.loc[0, "battery_soc_pct"])
    assert result.loc[0, "home_load_w"] == 100.0
