from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

DEVICE_POWER_COLUMNS = [
    "pv_total_w",
    "pv_1_w",
    "pv_2_w",
    "pv_3_w",
    "pv_4_w",
    "battery_power_w",
    "output_power_w",
]

SITE_POWER_COLUMNS = [
    "home_load_w",
    "grid_power_w",
    "grid_import_w",
    "grid_export_w",
    "grid_to_home_w",
    "grid_to_battery_w",
]


def aggregate_measurements(
    frame: pd.DataFrame, *, expected_devices: Sequence[tuple[str, str]] | None = None
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()

    working = frame.copy()
    working["pv_total_w"] = working["pv_reported_total_w"].combine_first(
        working["pv_calculated_total_w"]
    )
    device_power = [column for column in DEVICE_POWER_COLUMNS if column in working]
    device_count = len(set(expected_devices)) if expected_devices else 1
    result = (
        working.groupby("bucket_start", as_index=False)[device_power]
        .sum(min_count=device_count)
        .sort_values("bucket_start")
    )

    site_power = [column for column in SITE_POWER_COLUMNS if column in working]
    if site_power:
        if "site_id" in working:
            site_values = working.groupby(["bucket_start", "site_id"], as_index=False)[
                site_power
            ].first()
        else:
            site_values = working[["bucket_start", *site_power]]
        site_totals = (
            site_values.groupby("bucket_start", as_index=False)[site_power]
            .sum(
                min_count=len({site for site, _ in expected_devices})
                if expected_devices
                else 1
            )
            .sort_values("bucket_start")
        )
        result = result.merge(site_totals, on="bucket_start", how="left")

    if "battery_soc_pct" in working:
        state_of_charge = (
            working.groupby("bucket_start", as_index=False)["battery_soc_pct"]
            .mean()
            .sort_values("bucket_start")
        )
        counts = working.groupby("bucket_start")["battery_soc_pct"].count()
        state_of_charge.loc[
            state_of_charge["bucket_start"].map(counts) < device_count,
            "battery_soc_pct",
        ] = float("nan")
        result = result.merge(state_of_charge, on="bucket_start", how="left")
    return result
