from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from dash import html

from .config import Settings
from .freshness import assess_source_freshness

STATUS_LABELS = {
    "fresh": "Aktuell",
    "stale": "Veraltet",
    "missing": "Messzeit fehlt",
    "unknown_timezone": "Zeitzone unbekannt",
    "ambiguous_time": "Messzeit nicht eindeutig",
    "invalid": "Messzeit ungültig",
    "future": "Messzeit liegt in der Zukunft",
    "invalid_data": "Ungültige Daten",
}
SECRET_KEY = re.compile(r"password|passwd|secret|token|authorization|credential", re.I)
TEMPERATURE_KEY = re.compile(r"temperature|(?:^|_)temp(?:_|$)", re.I)


def source_frame(frame: pd.DataFrame, config: Settings) -> pd.DataFrame:
    """Mark stale source readings against their acquisition time, not today."""
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


def freshness_view(
    rows: list[dict[str, Any]], config: Settings, *, now: datetime | None = None
) -> tuple[str, str, Any]:
    current = now or datetime.now(UTC)
    maximum_age = timedelta(seconds=config.poll_interval_seconds * 3)
    records = []
    timestamps = []
    warnings = False
    for row in rows:
        source = assess_source_freshness(
            row.get("source_updated_at"),
            observed_at=current,
            max_age=maximum_age,
            source_timezone=config.source_timezone,
            data_valid=row.get("data_valid") is not False,
        )
        label = STATUS_LABELS[source.status]
        collected = row["collected_at"]
        if current - collected > maximum_age:
            label = "Abruf veraltet"
        warnings |= label != "Aktuell"
        source_text = row.get("source_updated_at") or "–"
        if source.timestamp is not None:
            timestamps.append(source.timestamp)
            source_text = source.timestamp.astimezone(config.timezone).strftime(
                "%d.%m.%Y %H:%M:%S %Z"
            )
        records.append(
            html.Tr(
                [
                    html.Th(row.get("device_name") or row["device_sn"], scope="row"),
                    html.Td(
                        collected.astimezone(config.timezone).strftime(
                            "%d.%m. %H:%M:%S"
                        )
                    ),
                    html.Td(source_text),
                    html.Td(label),
                ]
            )
        )
    if not rows:
        return "Messzeit: unbekannt", "status warning", html.P("Keine Gerätedaten.")
    if warnings:
        text = "Messzeit prüfen" if len(rows) > 1 else label
    elif timestamps:
        prefix = "Messwert von" if len(rows) == 1 else "Ältester Messwert"
        text = f"{prefix}: {min(timestamps).astimezone(config.timezone):%d.%m. %H:%M}"
    else:
        text = "Messzeit: unbekannt"
    table = html.Div(
        html.Table(
            [
                html.Caption("Gerätestatus", className="visually-hidden"),
                html.Thead(
                    html.Tr(
                        [
                            html.Th(value, scope="col")
                            for value in [
                                "Gerät",
                                "Abgerufen",
                                "Messwert von",
                                "Status",
                            ]
                        ]
                    )
                ),
                html.Tbody(records),
            ],
            className="detail-table",
        ),
        className="table-scroll",
    )
    return text, "status warning" if warnings else "status", table


def redact_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if SECRET_KEY.search(str(key)) else redact_snapshot(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_snapshot(child) for child in value]
    return value


def temperature_fields(value: Any, path: str = "") -> list[tuple[str, Any]]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if TEMPERATURE_KEY.search(str(key)) and not isinstance(child, (dict, list)):
                found.append((child_path, child))
            found.extend(temperature_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(temperature_fields(child, f"{path}[{index}]"))
    return found


def snapshot_view(snapshot: dict[str, Any], config: Settings) -> list[Any]:
    raw = redact_snapshot(snapshot["raw_data"])
    temperatures = temperature_fields(raw)
    timestamp = snapshot["collected_at"].astimezone(config.timezone)
    children = [
        html.P(
            f"Abgerufen: {timestamp:%d.%m.%Y %H:%M:%S %Z}", className="card-subtitle"
        )
    ]
    if temperatures:
        children.extend(
            [
                html.H3("Temperaturfelder"),
                html.Div(
                    html.Table(
                        [
                            html.Caption(
                                "Temperaturfelder", className="visually-hidden"
                            ),
                            html.Thead(
                                html.Tr(
                                    [
                                        html.Th("Feld", scope="col"),
                                        html.Th("Rohwert", scope="col"),
                                    ]
                                )
                            ),
                            html.Tbody(
                                [
                                    html.Tr(
                                        [
                                            html.Th(key, scope="row"),
                                            html.Td(str(value)),
                                        ]
                                    )
                                    for key, value in temperatures
                                ]
                            ),
                        ],
                        className="detail-table",
                    ),
                    className="table-scroll",
                ),
            ]
        )
    else:
        children.append(
            html.P(
                "Keine Temperaturfelder in diesem Snapshot.", className="card-subtitle"
            )
        )
    children.append(
        html.Pre(
            json.dumps(raw, ensure_ascii=False, indent=2), className="raw-snapshot"
        )
    )
    return children
