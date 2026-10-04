from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .models import Measurement


class NoSolarbankFound(RuntimeError):
    """Raised when the account response contains no selected solarbank."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def bucket_start(moment: datetime, interval_seconds: int) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("moment needs timezone information")
    timestamp = int(moment.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % interval_seconds), tz=UTC)


def seconds_until_next_bucket(moment: datetime, interval_seconds: int) -> float:
    if moment.tzinfo is None:
        raise ValueError("moment needs timezone information")
    remainder = moment.timestamp() % interval_seconds
    return float(interval_seconds - remainder)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _boolean(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_measurements(
    devices: Mapping[str, Mapping[str, Any]],
    sites: Mapping[str, Mapping[str, Any]],
    *,
    target_device_sn: str | None,
    collected_at: datetime,
    interval_seconds: int,
) -> list[Measurement]:
    selected: list[Mapping[str, Any]] = []

    for cache_key, device in devices.items():
        device_sn = _text(device.get("device_sn")) or str(cache_key)
        if target_device_sn and device_sn != target_device_sn:
            continue
        if device.get("type") != "solarbank":
            continue
        selected.append(device)

    if not selected:
        suffix = f" mit Seriennummer {target_device_sn}" if target_device_sn else ""
        raise NoSolarbankFound(f"Keine Solarbank{suffix} gefunden")

    slot = bucket_start(collected_at, interval_seconds)
    rows: list[Measurement] = []

    for device in selected:
        device_sn = _text(device.get("device_sn"))
        site_id = _text(device.get("site_id"))
        if not device_sn or not site_id:
            continue

        site = sites.get(site_id) or {}
        solarbank_info = site.get("solarbank_info") or {}
        grid_info = site.get("grid_info") or {}

        pv_values = (
            _number(device.get("solar_power_1")),
            _number(device.get("solar_power_2")),
            _number(device.get("solar_power_3")),
            _number(device.get("solar_power_4")),
        )
        calculated_total = (
            sum(value for value in pv_values if value is not None)
            if all(value is not None for value in pv_values)
            else None
        )

        grid_to_home = _number(grid_info.get("grid_to_home_power"))
        grid_to_battery = _number(solarbank_info.get("grid_to_battery_power"))
        grid_import = (
            (grid_to_home or 0.0) + (grid_to_battery or 0.0)
            if grid_to_home is not None or grid_to_battery is not None
            else None
        )
        grid_export = _number(grid_info.get("photovoltaic_to_grid_power"))
        grid_power = (
            grid_import - grid_export
            if grid_import is not None and grid_export is not None
            else None
        )

        source_updated_at = _text(solarbank_info.get("updated_time")) or _text(
            site.get("updated_time")
        )
        data_valid = _boolean(
            device.get("data_valid"), default=_boolean(site.get("data_valid"), True)
        )

        rows.append(
            Measurement(
                bucket_start=slot,
                collected_at=collected_at.astimezone(UTC),
                source_updated_at=source_updated_at,
                site_id=site_id,
                device_sn=device_sn,
                device_name=_text(device.get("alias") or device.get("name")),
                device_model=_text(device.get("device_pn")),
                data_valid=data_valid,
                pv_reported_total_w=_number(device.get("input_power")),
                pv_calculated_total_w=calculated_total,
                pv_1_w=pv_values[0],
                pv_2_w=pv_values[1],
                pv_3_w=pv_values[2],
                pv_4_w=pv_values[3],
                battery_soc_pct=_number(device.get("battery_soc")),
                battery_power_w=_number(device.get("charging_power")),
                output_power_w=_number(device.get("output_power")),
                home_load_w=_number(site.get("home_load_power")),
                grid_power_w=grid_power,
                grid_import_w=grid_import,
                grid_export_w=grid_export,
                grid_to_home_w=grid_to_home,
                grid_to_battery_w=grid_to_battery,
                raw_data={
                    "device": copy.deepcopy(dict(device)),
                    "site": copy.deepcopy(dict(site)),
                },
            )
        )

    if not rows:
        raise NoSolarbankFound("Solarbank ohne Site- oder Gerätekennung gefunden")
    return rows
