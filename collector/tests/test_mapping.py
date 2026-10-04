from datetime import UTC, datetime

import pytest

from solarbank_collector.mapping import (
    NoSolarbankFound,
    bucket_start,
    build_measurements,
    seconds_until_next_bucket,
)

COLLECTED_AT = datetime(2026, 9, 21, 12, 7, 45, tzinfo=UTC)


def _response() -> tuple[dict, dict]:
    device = {
        "device_sn": "SN-A17C5",
        "type": "solarbank",
        "site_id": "site-1",
        "device_pn": "A17C5",
        "alias": "Solarbank 3 E2700 Pro",
        "data_valid": True,
        "input_power": "301",
        "solar_power_1": "69",
        "solar_power_2": "66",
        "solar_power_3": "85",
        "solar_power_4": "81",
        "battery_soc": "78",
        "charging_power": "-42",
        "output_power": "343",
        "unknown_future_field": {"value": 17},
    }
    site = {
        "site_id": "site-1",
        "data_valid": True,
        "home_load_power": "424",
        "solarbank_info": {
            "updated_time": "2026-09-21 14:07:31",
            "grid_to_battery_power": "7",
        },
        "grid_info": {
            "grid_to_home_power": "123",
            "photovoltaic_to_grid_power": "5",
        },
    }
    return {"SN-A17C5": device}, {"site-1": site}


def test_maps_a17c5_snapshot() -> None:
    devices, sites = _response()
    row = build_measurements(
        devices,
        sites,
        target_device_sn=None,
        collected_at=COLLECTED_AT,
        interval_seconds=300,
    )[0]

    assert row.bucket_start == datetime(2026, 9, 21, 12, 5, tzinfo=UTC)
    assert row.pv_reported_total_w == 301
    assert row.pv_calculated_total_w == 301
    assert (row.pv_1_w, row.pv_2_w, row.pv_3_w, row.pv_4_w) == (
        69,
        66,
        85,
        81,
    )
    assert row.battery_soc_pct == 78
    assert row.battery_power_w == -42
    assert row.home_load_w == 424
    assert row.grid_to_home_w == 123
    assert row.grid_to_battery_w == 7
    assert row.grid_import_w == 130
    assert row.grid_export_w == 5
    assert row.grid_power_w == 125
    assert row.raw_data["device"]["unknown_future_field"] == {"value": 17}


def test_missing_values_stay_null() -> None:
    devices, sites = _response()
    device = devices["SN-A17C5"]
    device["solar_power_4"] = None
    device["input_power"] = ""
    sites["site-1"]["grid_info"].pop("photovoltaic_to_grid_power")
    sites["site-1"]["solarbank_info"].pop("grid_to_battery_power")

    row = build_measurements(
        devices,
        sites,
        target_device_sn="SN-A17C5",
        collected_at=COLLECTED_AT,
        interval_seconds=300,
    )[0]

    assert row.pv_4_w is None
    assert row.pv_calculated_total_w is None
    assert row.pv_reported_total_w is None
    assert row.grid_to_home_w == 123
    assert row.grid_to_battery_w is None
    assert row.grid_import_w == 123
    assert row.grid_export_w is None
    assert row.grid_power_w is None


def test_invalid_data_is_marked_and_non_finite_numbers_are_rejected() -> None:
    devices, sites = _response()
    devices["SN-A17C5"]["data_valid"] = "false"
    devices["SN-A17C5"]["charging_power"] = "NaN"

    row = build_measurements(
        devices,
        sites,
        target_device_sn=None,
        collected_at=COLLECTED_AT,
        interval_seconds=300,
    )[0]

    assert row.data_valid is False
    assert row.battery_power_w is None


def test_target_must_be_a_solarbank() -> None:
    devices, sites = _response()
    devices["meter"] = {
        "device_sn": "meter",
        "type": "smartmeter",
        "site_id": "site-1",
    }

    with pytest.raises(NoSolarbankFound):
        build_measurements(
            devices,
            sites,
            target_device_sn="meter",
            collected_at=COLLECTED_AT,
            interval_seconds=300,
        )


def test_bucket_helpers_are_utc_aligned() -> None:
    assert bucket_start(COLLECTED_AT, 300) == datetime(2026, 9, 21, 12, 5, tzinfo=UTC)
    assert seconds_until_next_bucket(COLLECTED_AT, 300) == 135
