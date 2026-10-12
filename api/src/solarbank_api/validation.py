from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_RANGE = timedelta(days=90, hours=2)
MAX_ROWS = 100_000
MAX_BATCH = 1000
NUMERIC_COLUMNS = (
    "pv_reported_total_w",
    "pv_calculated_total_w",
    "pv_1_w",
    "pv_2_w",
    "pv_3_w",
    "pv_4_w",
    "battery_soc_pct",
    "battery_power_w",
    "output_power_w",
    "home_load_w",
    "grid_power_w",
    "grid_import_w",
    "grid_export_w",
    "grid_to_home_w",
    "grid_to_battery_w",
)
MEASUREMENT_COLUMNS = (
    "bucket_start",
    "collected_at",
    "source_updated_at",
    "site_id",
    "device_sn",
    "device_name",
    "device_model",
    "data_valid",
    *NUMERIC_COLUMNS,
)


def timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} muss ein ISO-Zeitpunkt sein")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} muss ein ISO-Zeitpunkt sein") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} muss eine Zeitzone enthalten")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def text(value: Any, name: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} ist ungültig")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} ist ungültig")
    return value


def measurement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Messung ist ungültig")
    result: dict[str, Any] = {
        name: timestamp(value.get(name), name)
        for name in ("bucket_start", "collected_at")
    }
    result.update(
        {name: text(value.get(name), name) for name in ("site_id", "device_sn")}
    )
    for name in ("source_updated_at", "device_name", "device_model"):
        item = value.get(name)
        if item is not None and (not isinstance(item, str) or len(item) > 1024):
            raise ValueError(f"{name} ist ungültig")
        result[name] = item
    if not isinstance(value.get("data_valid"), bool):
        raise ValueError("data_valid muss true oder false sein")
    result["data_valid"] = int(value["data_valid"])
    for name in NUMERIC_COLUMNS:
        item = value.get(name)
        if item is not None:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"{name} muss eine endliche Zahl sein")
            try:
                item = float(item)
            except OverflowError as error:
                raise ValueError(f"{name} muss eine endliche Zahl sein") from error
            if not math.isfinite(item):
                raise ValueError(f"{name} muss eine endliche Zahl sein")
        result[name] = item
    if not isinstance(value.get("raw_data"), dict):
        raise ValueError("raw_data muss ein Objekt sein")
    try:
        result["raw_data"] = json.dumps(
            value["raw_data"],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (ValueError, TypeError) as error:
        raise ValueError("raw_data ist ungültig") from error
    return result


def filters(args: Any) -> tuple[list[str], list[str]]:
    clauses, parameters = [], []
    try:
        sites = json.loads(args.get("site_ids", "[]"))
        devices = json.loads(args.get("devices", "[]"))
    except (TypeError, ValueError) as error:
        raise ValueError("Gerätefilter ist ungültig") from error
    if not isinstance(sites, list) or not isinstance(devices, list):
        raise ValueError("Gerätefilter muss eine Liste sein")
    if len(sites) > 100 or len(devices) > 100:
        raise ValueError("Zu viele Gerätefilter")
    if sites:
        parameters.extend(text(site, "site_id") for site in sites)
        clauses.append("site_id IN (" + ",".join("?" for _ in sites) + ")")
    if devices:
        for device in devices:
            if not isinstance(device, list) or len(device) != 2:
                raise ValueError("Gerätefilter ist ungültig")
            parameters.extend(text(part, "device") for part in device)
        clauses.append(
            "(" + " OR ".join("(site_id = ? AND device_sn = ?)" for _ in devices) + ")"
        )
    return clauses, parameters


def measurement_filter(args: Any) -> tuple[list[str], list[Any], int]:
    start = timestamp(args.get("start"), "start")
    end = timestamp(args.get("end"), "end")
    duration = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    if not timedelta() < duration <= MAX_RANGE:
        raise ValueError("Zeitraum muss zwischen 0 und 90 Tagen liegen")
    try:
        limit = int(args.get("limit", MAX_ROWS))
    except (TypeError, ValueError) as error:
        raise ValueError("Limit ist ungültig") from error
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"Limit muss zwischen 1 und {MAX_ROWS} liegen")
    valid_only = args.get("valid_only", "true").lower()
    if valid_only not in {"true", "false"}:
        raise ValueError("valid_only muss true oder false sein")
    clauses, parameters = filters(args)
    clauses.extend(("bucket_start >= ?", "bucket_start < ?"))
    parameters.extend((start, end))
    if valid_only == "true":
        clauses.append("data_valid = 1")
    return clauses, parameters, limit
