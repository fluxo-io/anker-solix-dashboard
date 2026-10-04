"""Estimated energy from bounded power samples, with explicit data coverage."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

DEVICE_METRICS = ("pv", "battery_charge", "battery_discharge")
SITE_METRICS = ("home", "grid_import", "grid_export")
ENERGY_METRICS = (*DEVICE_METRICS, *SITE_METRICS)
ENERGY_COLUMNS = [
    "day",
    *(f"{metric}_kwh" for metric in ENERGY_METRICS),
    *(f"{metric}_coverage_pct" for metric in ENERGY_METRICS),
    "coverage_pct",
    "period_seconds",
    "is_partial_day",
]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Zeitpunkt muss eine Zeitzone enthalten")
    return value.astimezone(UTC)


def _numbers(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    values = pd.to_numeric(frame[name], errors="coerce").astype(float)
    return values.where(np.isfinite(values))


def _intervals(
    frame: pd.DataFrame, keys: list[str], metrics: Sequence[str], seconds: float
) -> pd.DataFrame:
    # Invalid samples remain in the timeline and end the preceding sample.
    result = frame[[*keys, "bucket_start", *metrics]].sort_values("bucket_start")
    next_sample = result.groupby(keys)["bucket_start"].shift(-1)
    end = result["bucket_start"] + pd.Timedelta(seconds=seconds)
    end = end.where(next_sample.isna() | (end <= next_sample), next_sample)
    result["_start_ns"] = result["bucket_start"].dt.as_unit("ns").astype("int64")
    result["_end_ns"] = end.dt.as_unit("ns").astype("int64")
    return result


def daily_energy(
    frame: pd.DataFrame,
    start: datetime,
    end: datetime,
    timezone_name: str,
    poll_interval_seconds: float,
    *,
    now: datetime | None = None,
    expected_devices: Sequence[tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """Return local daily kWh estimates and coverage within the selected window.

    Each power sample applies from its bucket start until the next sample, but
    never longer than one polling interval. Missing periods are not filled.
    Invalid rows and rows marked ``source_stale`` provide no energy or coverage.
    The caller supplies that flag using its shared source-time assessment; an
    absent/unknown source time alone is not evidence of stale power readings.

    Positive battery power means charging. Device powers are added; site powers
    repeated on device rows count once. Coverage measures observed entity-seconds
    divided by expected entity-seconds for each metric. Supplying all selected
    ``expected_devices`` also exposes devices with no rows in the window. Without
    it, the expected devices are inferred from the supplied frame.

    A missing metric is NaN, while a measured zero is 0 kWh. Partial totals remain
    estimates of the covered periods and must be shown with their coverage.
    """
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError("Zeitraum ist ungültig")
    if not np.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise ValueError("Abfrageintervall muss positiv sein")
    timezone = ZoneInfo(timezone_name)
    end = min(end, _utc(now or datetime.now(UTC)))
    if end <= start:
        return pd.DataFrame(columns=ENERGY_COLUMNS)

    working = frame.copy()
    required = ["site_id", "device_sn", "bucket_start"]
    if working.empty:
        working = pd.DataFrame(columns=required)
    elif any(column not in working for column in required):
        raise ValueError("Messwerte benötigen Anlage, Gerät und Zeitstempel")
    working["bucket_start"] = pd.to_datetime(
        working["bucket_start"], utc=True, errors="coerce"
    )
    working = working.dropna(subset=required)
    expected = (
        set(expected_devices)
        if expected_devices is not None
        else set(zip(working["site_id"], working["device_sn"], strict=True))
    )
    selected = pd.MultiIndex.from_tuples(list(expected), names=required[:2])
    working = working.loc[
        pd.MultiIndex.from_frame(working[required[:2]]).isin(selected)
    ].copy()
    # Keep a sample immediately before start if supplied by the caller. Its hold
    # may overlap the window; coverage must not pretend the leading gap is full.
    working = working.loc[
        (working["bucket_start"] >= start - timedelta(seconds=poll_interval_seconds))
        & (working["bucket_start"] < end)
    ].copy()
    if "collected_at" in working:
        working["collected_at"] = pd.to_datetime(
            working["collected_at"], utc=True, errors="coerce"
        )
        working = working.sort_values("collected_at", na_position="first")
    working = working.drop_duplicates(required, keep="last")

    usable = pd.Series(True, index=working.index)
    if "data_valid" in working:
        usable &= working["data_valid"].eq(True).fillna(False)
    if "source_stale" in working:
        usable &= ~working["source_stale"].fillna(False).astype(bool)
    working["pv"] = (
        _numbers(working, "pv_reported_total_w")
        .combine_first(_numbers(working, "pv_calculated_total_w"))
        .combine_first(_numbers(working, "pv_total_w"))
    )
    battery = _numbers(working, "battery_power_w")
    working["battery_charge"] = battery.clip(lower=0)
    working["battery_discharge"] = (-battery).clip(lower=0)
    working["home"] = _numbers(working, "home_load_w")
    grid = _numbers(working, "grid_power_w")
    working["grid_import"] = _numbers(working, "grid_import_w").combine_first(
        grid.clip(lower=0)
    )
    working["grid_export"] = _numbers(working, "grid_export_w").combine_first(
        (-grid).clip(lower=0)
    )
    for metric in ENERGY_METRICS:
        working[metric] = working[metric].where(usable & (working[metric] >= 0))

    device_samples = _intervals(
        working, required[:2], DEVICE_METRICS, poll_interval_seconds
    )
    site_samples = _intervals(
        working.groupby(["site_id", "bucket_start"], as_index=False)[
            list(SITE_METRICS)
        ].first(),
        ["site_id"],
        SITE_METRICS,
        poll_interval_seconds,
    )
    scopes = (
        (device_samples, DEVICE_METRICS, len(expected)),
        (site_samples, SITE_METRICS, len({site for site, _ in expected})),
    )

    rows = []
    day = start.astimezone(timezone).date()
    while True:
        midnight = datetime.combine(day, time.min, timezone).astimezone(UTC)
        tomorrow = datetime.combine(
            day + timedelta(days=1), time.min, timezone
        ).astimezone(UTC)
        if midnight >= end:
            break
        window_start, window_end = max(start, midnight), min(end, tomorrow)
        duration = (window_end - window_start).total_seconds()
        row = {
            "day": day.isoformat(),
            "period_seconds": duration,
            "is_partial_day": window_start != midnight or window_end != tomorrow,
        }
        for samples, metrics, entity_count in scopes:
            seconds = np.maximum(
                np.minimum(samples["_end_ns"], pd.Timestamp(window_end).value)
                - np.maximum(samples["_start_ns"], pd.Timestamp(window_start).value),
                0,
            ).to_numpy(dtype=float) / 1e9
            for metric in metrics:
                values = samples[metric].to_numpy(dtype=float, na_value=np.nan)
                observed = np.isfinite(values) & (seconds > 0)
                covered = seconds[observed].sum()
                row[f"{metric}_kwh"] = (
                    (values[observed] * seconds[observed]).sum() / 3_600_000
                    if covered > 0
                    else np.nan
                )
                row[f"{metric}_coverage_pct"] = (
                    min(100.0, 100 * covered / (duration * entity_count))
                    if entity_count
                    else 0.0
                )
        row["coverage_pct"] = min(
            row[f"{metric}_coverage_pct"] for metric in ENERGY_METRICS
        )
        rows.append(row)
        day += timedelta(days=1)
    return pd.DataFrame(rows, columns=ENERGY_COLUMNS)
