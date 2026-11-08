from __future__ import annotations

import json
import re
from typing import Any

from dash import html

from .config import Settings

STATUS_LABELS = {
    "fresh": "Aktuell",
    "collection_stale": "Abruf veraltet",
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


def freshness_view(
    assessment: dict[str, Any], config: Settings
) -> tuple[str, str, Any]:
    """Render the API's freshness verdict, without a second time assessment."""
    rows = assessment["rows"]
    records = []
    warnings = assessment["warning"]
    for row in rows:
        label = STATUS_LABELS[row["status"]]
        collected = row["collected_at"]
        source_text = row.get("source_updated_at") or "–"
        if row.get("source_timestamp") is not None:
            source_text = (
                row["source_timestamp"]
                .astimezone(config.timezone)
                .strftime("%d.%m.%Y %H:%M:%S %Z")
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
    elif assessment["oldest_timestamp"] is not None:
        prefix = "Messwert von" if len(rows) == 1 else "Ältester Messwert"
        oldest = assessment["oldest_timestamp"].astimezone(config.timezone)
        text = f"{prefix}: {oldest:%d.%m. %H:%M}"
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
