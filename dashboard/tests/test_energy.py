from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from solarbank_dashboard.energy import ENERGY_METRICS, daily_energy

START = datetime(2026, 9, 20, tzinfo=UTC)


def sample(moment: datetime, **overrides: object) -> dict:
    return {
        "bucket_start": moment,
        "site_id": "site-a",
        "device_sn": "device-a",
        "data_valid": True,
        "pv_reported_total_w": 1000.0,
        "battery_power_w": 300.0,
        "home_load_w": 500.0,
        "grid_import_w": 200.0,
        "grid_export_w": 100.0,
        **overrides,
    }


def calculate(
    rows: list[dict], *, minutes: int = 60, **options: object
) -> pd.DataFrame:
    end = START + timedelta(minutes=minutes)
    return daily_energy(
        pd.DataFrame(rows), START, end, "UTC", 300, now=end, **options
    )


def test_daily_totals_use_directional_grid_and_signed_battery_power() -> None:
    rows = [
        sample(
            START + timedelta(minutes=minute),
            battery_power_w=300.0 if minute < 30 else -600.0,
            grid_power_w=100.0,
        )
        for minute in range(0, 60, 5)
    ]

    result = calculate(rows).iloc[0]

    assert result["pv_kwh"] == pytest.approx(1.0)
    assert result["home_kwh"] == pytest.approx(0.5)
    assert result["grid_import_kwh"] == pytest.approx(0.2)
    assert result["grid_export_kwh"] == pytest.approx(0.1)
    assert result["battery_charge_kwh"] == pytest.approx(0.15)
    assert result["battery_discharge_kwh"] == pytest.approx(0.3)
    assert result["coverage_pct"] == 100
    assert result["period_seconds"] == 3600
    assert result["is_partial_day"]


def test_devices_sum_but_repeated_site_metrics_count_once() -> None:
    rows = [
        sample(START),
        sample(START, device_sn="device-b", pv_reported_total_w=2000.0),
        sample(START, site_id="site-b", device_sn="device-c", home_load_w=250.0),
    ]

    result = calculate(rows, minutes=5).iloc[0]

    assert result["pv_kwh"] == pytest.approx(4 / 12)
    assert result["home_kwh"] == pytest.approx(0.75 / 12)
    assert result["coverage_pct"] == 100


def test_entirely_missing_selected_device_reduces_only_its_scope_coverage() -> None:
    result = calculate(
        [sample(START)],
        minutes=5,
        expected_devices=[("site-a", "device-a"), ("site-a", "device-b")],
    ).iloc[0]

    assert result["pv_kwh"] == pytest.approx(1 / 12)
    assert result["pv_coverage_pct"] == 50
    assert result["home_coverage_pct"] == 100
    assert result["coverage_pct"] == 50


def test_expected_devices_exclude_unselected_measurements() -> None:
    result = calculate(
        [sample(START), sample(START, device_sn="device-b")],
        minutes=5,
        expected_devices=[("site-a", "device-a")],
    ).iloc[0]

    assert result["pv_kwh"] == pytest.approx(1 / 12)
    assert result["coverage_pct"] == 100


@pytest.mark.parametrize("unusable", [{"data_valid": False}, {"source_stale": True}])
def test_invalid_or_stale_samples_do_not_fill_gaps(unusable: dict) -> None:
    result = calculate(
        [
            sample(START),
            sample(START + timedelta(minutes=5), **unusable),
            sample(START + timedelta(minutes=10)),
        ],
        minutes=15,
    ).iloc[0]

    assert result["pv_kwh"] == pytest.approx(1 / 6)
    assert result["coverage_pct"] == pytest.approx(200 / 3)


def test_missing_samples_are_not_bridged() -> None:
    result = calculate(
        [sample(START), sample(START + timedelta(minutes=15))], minutes=20
    ).iloc[0]

    assert result["pv_kwh"] == pytest.approx(1 / 6)
    assert result["coverage_pct"] == 50


def test_irregular_next_sample_ends_previous_hold_early() -> None:
    result = calculate(
        [
            sample(START),
            sample(START + timedelta(minutes=2), pv_reported_total_w=2000.0),
        ],
        minutes=7,
    ).iloc[0]

    assert result["pv_kwh"] == pytest.approx((2 + 2 * 5) / 60)
    assert result["coverage_pct"] == 100


def test_duplicate_device_bucket_uses_latest_collection_once() -> None:
    result = calculate(
        [
            sample(START, collected_at=START),
            sample(
                START,
                collected_at=START + timedelta(seconds=10),
                pv_reported_total_w=2000.0,
            ),
        ],
        minutes=5,
    ).iloc[0]

    assert result["pv_kwh"] == pytest.approx(2 / 12)
    assert result["coverage_pct"] == 100


def test_metric_missing_is_distinct_from_observed_zero() -> None:
    result = calculate(
        [sample(START, pv_reported_total_w=None, home_load_w=0.0)], minutes=5
    ).iloc[0]

    assert pd.isna(result["pv_kwh"])
    assert result["pv_coverage_pct"] == 0
    assert result["home_kwh"] == 0
    assert result["home_coverage_pct"] == 100


def test_invalid_duplicate_site_row_does_not_hide_valid_site_reading() -> None:
    result = calculate(
        [sample(START, data_valid=False), sample(START, device_sn="device-b")],
        minutes=5,
    ).iloc[0]

    assert result["pv_coverage_pct"] == 50
    assert result["home_coverage_pct"] == 100
    assert result["home_kwh"] == pytest.approx(0.5 / 12)


def test_empty_window_keeps_days_and_unknown_totals() -> None:
    result = calculate([], expected_devices=[("site-a", "device-a")]).iloc[0]

    assert result["day"] == "2026-09-20"
    assert all(pd.isna(result[f"{metric}_kwh"]) for metric in ENERGY_METRICS)
    assert result["coverage_pct"] == 0


def test_sample_is_split_at_local_midnight_and_clipped_to_now() -> None:
    # Berlin midnight is 22:00 UTC in September.
    start = datetime(2026, 9, 20, 21, 57, tzinfo=UTC)
    result = daily_energy(
        pd.DataFrame([sample(start)]),
        start,
        start + timedelta(hours=1),
        "Europe/Berlin",
        600,
        now=start + timedelta(minutes=5),
    )

    assert result["day"].tolist() == ["2026-09-20", "2026-09-21"]
    assert result["pv_kwh"].tolist() == pytest.approx([3 / 60, 2 / 60])
    assert result["period_seconds"].tolist() == [180, 120]
    assert result["coverage_pct"].tolist() == [100, 100]


@pytest.mark.parametrize("day, hours", [("2026-03-29", 23), ("2026-10-25", 25)])
def test_daylight_saving_days_use_actual_elapsed_time(day: str, hours: int) -> None:
    timezone = ZoneInfo("Europe/Berlin")
    start_local = datetime.fromisoformat(day).replace(tzinfo=timezone)
    start = start_local.astimezone(UTC)
    end = (start_local + timedelta(days=1)).astimezone(UTC)
    rows = [
        sample(start + timedelta(minutes=minute))
        for minute in range(0, hours * 60, 5)
    ]

    result = daily_energy(
        pd.DataFrame(rows), start, end, "Europe/Berlin", 300, now=end
    ).iloc[0]

    assert result["pv_kwh"] == hours
    assert result["period_seconds"] == hours * 3600
    assert not result["is_partial_day"]
    assert result["coverage_pct"] == 100


def test_preceding_sample_only_covers_remaining_hold_inside_filter() -> None:
    result = daily_energy(
        pd.DataFrame([sample(START)]),
        START + timedelta(minutes=3),
        START + timedelta(minutes=8),
        "UTC",
        300,
        now=START + timedelta(minutes=8),
    ).iloc[0]

    assert result["pv_kwh"] == pytest.approx(2 / 60)
    assert result["coverage_pct"] == 40


def test_fallbacks_and_missing_metric_coverage() -> None:
    result = calculate(
        [
            sample(
                START,
                pv_reported_total_w=None,
                pv_calculated_total_w=600.0,
                grid_import_w=None,
                grid_export_w=None,
                grid_power_w=-120.0,
                battery_power_w=None,
            )
        ],
        minutes=5,
    ).iloc[0]

    assert result["pv_kwh"] == pytest.approx(0.05)
    assert result["grid_export_kwh"] == pytest.approx(0.01)
    assert result["grid_import_kwh"] == 0
    assert result["grid_import_coverage_pct"] == 100
    assert result["battery_charge_coverage_pct"] == 0


def test_rejects_invalid_range_and_poll_interval() -> None:
    with pytest.raises(ValueError, match="Zeitzone"):
        daily_energy(pd.DataFrame(), START.replace(tzinfo=None), START, "UTC", 300)
    with pytest.raises(ValueError, match="Zeitraum"):
        daily_energy(pd.DataFrame(), START, START, "UTC", 300)
    with pytest.raises(ValueError, match="positiv"):
        daily_energy(pd.DataFrame(), START, START + timedelta(hours=1), "UTC", 0)
