"""Authoritative dashboard calculations over API-owned measurement records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from .aggregation import aggregate_measurements
from .config import Settings
from .energy import daily_energy
from .freshness import assess_source_freshness
from .store import Store
from .validation import filters, measurement_filter, timestamp

TABLE_ROW_LIMIT = 5_000
MAX_DEVICES = 1_000
MAX_DASHBOARD_BYTES = 64 * 1024 * 1024


def source_frame(frame: pd.DataFrame, config: Settings) -> pd.DataFrame:
    """Judge historical samples at acquisition time, never against today's age."""
    result = frame.copy()
    assessments = [
        assess_source_freshness(
            row.get("source_updated_at"),
            observed_at=pd.Timestamp(row["collected_at"]).to_pydatetime(),
            max_age=timedelta(seconds=config.poll_interval_seconds * 3),
            source_timezone=config.source_timezone,
            data_valid=row.get("data_valid") is not False,
        )
        for row in frame.to_dict("records")
    ]
    result["source_timestamp"] = [value.timestamp for value in assessments]
    result["source_stale"] = [not value.is_usable for value in assessments]
    result["source_status"] = [value.status for value in assessments]
    return result


def freshness_status(
    rows: list[dict[str, Any]], config: Settings, *, now: datetime
) -> dict[str, Any]:
    maximum_age = timedelta(seconds=config.poll_interval_seconds * 3)
    records, timestamps = [], []
    for row in rows:
        source = assess_source_freshness(
            row.get("source_updated_at"),
            observed_at=now,
            max_age=maximum_age,
            source_timezone=config.source_timezone,
            data_valid=row.get("data_valid") is not False,
        )
        collected = datetime.fromisoformat(row["collected_at"])
        status = "collection_stale" if now - collected > maximum_age else source.status
        if source.timestamp is not None:
            timestamps.append(source.timestamp)
        records.append(
            {
                **row,
                "status": status,
                "source_timestamp": source.timestamp.isoformat()
                if source.timestamp
                else None,
            }
        )
    return {
        "rows": records,
        "warning": not rows or any(row["status"] != "fresh" for row in records),
        "oldest_timestamp": min(timestamps).isoformat() if timestamps else None,
    }


def collector_status(
    state: dict[str, Any] | None, config: Settings, *, now: datetime
) -> dict[str, Any]:
    if state is None or state.get("last_success_at") is None:
        return {"status": "missing", "last_success_at": None, "consecutive_failures": 0}
    failures = int(state.get("consecutive_failures") or 0)
    last_success = datetime.fromisoformat(state["last_success_at"])
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=UTC)
    status = "fresh"
    if failures:
        status = "failed"
    elif now - last_success.astimezone(UTC) > timedelta(
        seconds=config.poll_interval_seconds * 3
    ):
        status = "stale"
    return {
        "status": status,
        "last_success_at": last_success.isoformat(),
        "consecutive_failures": failures,
    }


def records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    # Normalize pandas NaN/NaT and numpy scalars at the service boundary. Power
    # and coverage remain numeric; only presentation belongs to Web.
    return json.loads(
        frame.to_json(
            orient="records", date_format="iso", date_unit="us", double_precision=15
        )
    )


def dashboard(
    store: Store, config: Settings, args: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    clauses, parameters, limit = measurement_filter(args)
    start = datetime.fromisoformat(timestamp(args.get("start"), "start"))
    end = datetime.fromisoformat(timestamp(args.get("end"), "end"))
    latest = store.latest(*filters(args), limit=MAX_DEVICES + 1)
    if len(latest) > MAX_DEVICES:
        raise ValueError("Zu viele Geräte. Bitte den Filter eingrenzen.")
    rows = store.measurements(clauses, parameters, limit)
    loaded = len(rows)
    filtered = (
        int(rows[0]["filtered_row_count"])
        if rows
        else store.measurement_count(clauses, parameters)
    )
    truncated = filtered > loaded
    expected = [(row["site_id"], row["device_sn"]) for row in latest]
    result = {
        "rows": [],
        "aggregated": [],
        "energy": [],
        "loaded_count": loaded,
        "filtered_count": filtered,
        "truncated": truncated,
        "energy_status": "truncated" if truncated else "empty",
        "unknown_source_age": False,
        "kpis": {
            key: None
            for key in ("pv_total_w", "home_load_w", "grid_power_w", "battery_soc_pct")
        },
        "poll_interval_seconds": config.poll_interval_seconds,
        "freshness": freshness_status(latest, config, now=current),
        "collector": collector_status(store.collector_state(), config, now=current),
    }
    if not rows:
        return result
    frame = pd.DataFrame(rows)
    for column in ("bucket_start", "collected_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame = source_frame(frame, config)
    # Preserve rejected readings in the raw table; never draw or integrate them.
    chart_frame = frame.copy()
    power_columns = [column for column in chart_frame if column.endswith("_w")]
    chart_frame.loc[
        chart_frame["source_stale"], [*power_columns, "battery_soc_pct"]
    ] = float("nan")
    aggregated = aggregate_measurements(chart_frame, expected_devices=expected)
    result["aggregated"] = records(aggregated)
    result["kpis"] = {key: result["aggregated"][-1].get(key) for key in result["kpis"]}
    frame["pv_total_w"] = frame["pv_reported_total_w"].combine_first(
        frame["pv_calculated_total_w"]
    )
    result["rows"] = records(
        frame.sort_values("bucket_start", ascending=False).head(TABLE_ROW_LIMIT)
    )
    result["unknown_source_age"] = bool(
        frame["source_status"].isin(["missing", "unknown_timezone"]).any()
    )
    if not truncated:
        result["energy_status"] = "available"
        result["energy"] = records(
            daily_energy(
                frame,
                start,
                end,
                config.timezone_name,
                config.poll_interval_seconds,
                now=current,
                expected_devices=expected,
            )
        )
    return result
