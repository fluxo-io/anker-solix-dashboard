from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dash_ag_grid as dag
import pandas as pd
import plotly.graph_objects as go
import psycopg
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import jsonify

from solarbank_dashboard.auth import AuthSettings, configure_auth
from solarbank_dashboard.charts import aggregate_measurements, line_figure
from solarbank_dashboard.config import ConfigurationError, Settings
from solarbank_dashboard.details import freshness_view, snapshot_view, source_frame
from solarbank_dashboard.energy import daily_energy
from solarbank_dashboard.public_pages import register_public_pages
from solarbank_dashboard.repository import (
    Device,
    fetch_collector_state,
    fetch_devices,
    fetch_latest_measurements,
    fetch_measurements,
    fetch_snapshot,
    verify_schema,
)
from solarbank_dashboard.time_range import (
    PRESETS,
    TimeWindow,
    custom_window,
    preset_window,
)

LOGGER = logging.getLogger(__name__)

CUSTOM_RANGE = "Eigener Zeitraum"
TABLE_ROW_LIMIT = 5_000
AUTO_REFRESH_INTERVAL_MS = 60_000
AUTO_REFRESH_VALUE = "enabled"
DASHBOARD_TITLE = "Private Solardatenbank · fluxo.io"
ASSETS_FOLDER = Path(__file__).resolve().parents[2] / "assets"
GRAPH_CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "showSendToCloud": False,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}

TABLE_COLUMNS = {
    "bucket_start": "Zeit",
    "site_id": "Anlage",
    "device_sn": "Gerät",
    "data_valid": "Gültig",
    "pv_total_w": "PV gesamt (W)",
    "pv_1_w": "PV1 (W)",
    "pv_2_w": "PV2 (W)",
    "pv_3_w": "PV3 (W)",
    "pv_4_w": "PV4 (W)",
    "battery_soc_pct": "Akku (%)",
    "battery_power_w": "Akku (W)",
    "output_power_w": "Ausgang (W)",
    "home_load_w": "Haus (W)",
    "grid_power_w": "Netz (W)",
    "grid_import_w": "Netzbezug (W)",
    "grid_export_w": "Einspeisung (W)",
    "grid_to_home_w": "Netz → Haus (W)",
    "grid_to_battery_w": "Netz → Akku (W)",
}

NUMERIC_TABLE_COLUMNS = [
    column
    for column in TABLE_COLUMNS
    if column not in {"bucket_start", "site_id", "device_sn", "data_valid"}
]

GRID_COLUMNS = [
    {
        "field": "bucket_start",
        "headerName": "Zeit",
        "minWidth": 165,
        "sort": "desc",
        "comparator": {"function": "timestampComparator"},
    },
    {"field": "site_id", "headerName": "Anlage", "minWidth": 140},
    {"field": "device_sn", "headerName": "Gerät", "minWidth": 150},
    {"field": "data_valid", "headerName": "Gültig", "maxWidth": 105},
    {
        "field": "pv_total_w",
        "headerName": "PV gesamt",
        "type": "numericColumn",
    },
    {"field": "pv_1_w", "headerName": "PV1", "type": "numericColumn"},
    {"field": "pv_2_w", "headerName": "PV2", "type": "numericColumn"},
    {"field": "pv_3_w", "headerName": "PV3", "type": "numericColumn"},
    {"field": "pv_4_w", "headerName": "PV4", "type": "numericColumn"},
    {
        "field": "battery_soc_pct",
        "headerName": "Akku %",
        "type": "numericColumn",
    },
    {
        "field": "battery_power_w",
        "headerName": "Akku W",
        "type": "numericColumn",
    },
    {
        "field": "output_power_w",
        "headerName": "Ausgang",
        "type": "numericColumn",
    },
    {"field": "home_load_w", "headerName": "Haus", "type": "numericColumn"},
    {"field": "grid_power_w", "headerName": "Netz", "type": "numericColumn"},
    {"field": "grid_import_w", "headerName": "Netzbezug", "type": "numericColumn"},
    {"field": "grid_export_w", "headerName": "Einspeisung", "type": "numericColumn"},
    {"field": "grid_to_home_w", "headerName": "Netz → Haus", "type": "numericColumn"},
    {
        "field": "grid_to_battery_w",
        "headerName": "Netz → Akku",
        "type": "numericColumn",
    },
]


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_environment()


def german_number(value: float, decimals: int = 0) -> str:
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def format_power(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "–"
    return f"{german_number(value)} W"


def format_grid(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "–"
    if value > 0:
        return f"{german_number(value)} W Bezug"
    if value < 0:
        return f"{german_number(abs(value))} W Einspeisung"
    return "0 W"


def format_timestamp(value: datetime | None, config: Settings) -> str:
    if value is None:
        return "–"
    return value.astimezone(config.timezone).strftime("%d.%m.%Y %H:%M")


def toast_message(message: str, kind: str, event_id: int | None = None) -> Any:
    attributes: dict[str, str] = {
        "data-toast-text": "",
        "data-toast-type": kind,
    }
    if event_id is not None:
        attributes["data-toast-event"] = str(event_id)
    return html.Span(message, **attributes)


def latest_value(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame:
        return None
    value = frame.sort_values("bucket_start").iloc[-1][column]
    return None if pd.isna(value) else float(value)


def dashboard_today(now: datetime | None = None) -> date:
    timezone_name = os.getenv("DASHBOARD_TIMEZONE", "Europe/Berlin").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Zeitpunkt muss eine Zeitzone enthalten")
    return current.astimezone(timezone).date()


def resolve_window(
    period: str | None,
    start_value: str | None,
    end_value: str | None,
    config: Settings,
    *,
    now: datetime | None = None,
) -> TimeWindow:
    if period != CUSTOM_RANGE:
        return preset_window(period or "", now)
    if not start_value or not end_value:
        raise ValueError("Zeitraum ist unvollständig")
    try:
        start_date = date.fromisoformat(start_value[:10])
        end_date = date.fromisoformat(end_value[:10])
    except ValueError as error:
        raise ValueError("Zeitraum ist ungültig") from error
    return custom_window(start_date, end_date, config.timezone)


def encode_device(device: Device) -> str:
    return json.dumps([device.site_id, device.device_sn], separators=(",", ":"))


def decode_devices(values: list[str] | None) -> tuple[tuple[str, str], ...]:
    decoded: list[tuple[str, str]] = []
    for value in values or []:
        try:
            pair = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("Gerätefilter ist ungültig") from error
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) and item for item in pair)
        ):
            raise ValueError("Gerätefilter ist ungültig")
        decoded.append((pair[0], pair[1]))
    return tuple(decoded)


def filter_options(
    devices: list[Device], selected_sites: list[str] | None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    sites = sorted({device.site_id for device in devices})
    selected = set(selected_sites or [])
    eligible = [
        device for device in devices if not selected or device.site_id in selected
    ]
    site_options = [{"label": site, "value": site} for site in sites]
    device_options = [
        {
            "label": f"{device.label} · {device.site_id}",
            "value": encode_device(device),
        }
        for device in eligible
    ]
    return site_options, device_options


def measurement_frame(rows: list[dict[str, Any]], config: Settings) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["bucket_start"] = pd.to_datetime(
        frame["bucket_start"], utc=True
    ).dt.tz_convert(config.timezone_name)
    return frame


def _display_pv_total(frame: pd.DataFrame) -> pd.Series:
    reported = frame.get("pv_reported_total_w")
    calculated = frame.get("pv_calculated_total_w")
    if reported is None:
        return (
            calculated
            if calculated is not None
            else pd.Series(index=frame.index, dtype="float64")
        )
    if calculated is None:
        return reported
    return reported.combine_first(calculated)


def _timestamp_with_offset(value: Any, pattern: str) -> str | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.Timestamp(value)
    offset = timestamp.strftime("%z")
    if len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    return f"{timestamp.strftime(pattern)} {offset}".rstrip()


def table_frame(frame: pd.DataFrame, *, limit: int | None = None) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=TABLE_COLUMNS)
    display_frame = frame.copy()
    display_frame["pv_total_w"] = _display_pv_total(display_frame)
    table = display_frame.reindex(columns=TABLE_COLUMNS).sort_values(
        "bucket_start", ascending=False
    )
    if limit is not None:
        table = table.head(limit)
    table = table.copy()
    table["bucket_start"] = table["bucket_start"].map(
        lambda value: _timestamp_with_offset(value, "%Y-%m-%d %H:%M")
    )
    table["data_valid"] = table["data_valid"].map(format_validity)
    table[NUMERIC_TABLE_COLUMNS] = table[NUMERIC_TABLE_COLUMNS].round(1)
    return table.astype(object).where(pd.notna(table), None)


def format_validity(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return "Ja" if bool(value) else "Nein"


def export_frame(frame: pd.DataFrame) -> pd.DataFrame:
    table = table_frame(frame)
    table["bucket_start"] = table["bucket_start"].map(
        lambda value: (
            _timestamp_with_offset(datetime.fromisoformat(value), "%d.%m.%Y %H:%M")
            if value
            else None
        )
    )
    return table.rename(columns=TABLE_COLUMNS)


def measurement_counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    loaded_count = len(rows)
    if not rows:
        return 0, 0
    filtered_count = int(rows[0].get("filtered_row_count") or loaded_count)
    return loaded_count, filtered_count


def table_summary(loaded_count: int, filtered_count: int) -> str:
    visible_count = min(loaded_count, TABLE_ROW_LIMIT)
    if filtered_count > visible_count:
        return (
            f"Neueste {german_number(visible_count)} von "
            f"{german_number(filtered_count)} Werten"
        )
    return f"{german_number(filtered_count)} Werte"


def empty_figure(message: str = "Keine Daten") -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(
        text=message,
        showarrow=False,
        font={"color": "#586c7b", "family": "Open Sans, sans-serif", "size": 14},
    )
    figure.update_layout(
        height=340,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return figure


def graph_figures(
    frame: pd.DataFrame, *, revision: str = "default", interval_seconds: int = 300
) -> tuple[go.Figure, go.Figure, go.Figure]:
    if frame.empty:
        return empty_figure(), empty_figure(), empty_figure()
    return (
        line_figure(
            frame,
            ["pv_total_w", "pv_1_w", "pv_2_w", "pv_3_w", "pv_4_w"],
            y_title="W",
            revision=revision,
            interval_seconds=interval_seconds,
        ),
        line_figure(
            frame,
            ["home_load_w", "output_power_w", "battery_power_w", "grid_power_w"],
            y_title="W",
            revision=revision,
            interval_seconds=interval_seconds,
        ),
        line_figure(
            frame,
            ["battery_soc_pct"],
            y_title="%",
            height=300,
            revision=revision,
            interval_seconds=interval_seconds,
        ),
    )


def collector_status(
    state: dict[str, Any] | None,
    config: Settings,
    *,
    now: datetime | None = None,
) -> tuple[str, str, str | None]:
    if state is None or state.get("last_success_at") is None:
        return "Noch kein gültiger Abruf", "status warning", None
    timestamp = format_timestamp(state["last_success_at"], config)
    failures = int(state.get("consecutive_failures") or 0)
    if failures:
        return (
            f"Abgerufen: {timestamp}",
            "status warning",
            f"Collector: {failures} Fehler",
        )
    current = now or datetime.now(UTC)
    last_success = state["last_success_at"]
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=UTC)
    stale_after = timedelta(seconds=config.poll_interval_seconds * 3)
    if current - last_success.astimezone(UTC) > stale_after:
        return (
            f"Abgerufen: {timestamp}",
            "status warning",
            "Collector: keine aktuellen Daten",
        )
    return f"Abgerufen: {timestamp}", "status", None


ENERGY_METRICS = {
    "pv_kwh": ("PV", "#d79b22"),
    "home_kwh": ("Haus", "#007aaf"),
    "grid_import_kwh": ("Netzbezug", "#b05252"),
    "grid_export_kwh": ("Einspeisung", "#8da33b"),
    "battery_charge_kwh": ("Akku geladen", "#3e7cb1"),
    "battery_discharge_kwh": ("Akku entladen", "#6c63a8"),
}


def energy_view(frame: pd.DataFrame, revision: str) -> tuple[go.Figure, Any]:
    figure = go.Figure()
    rows = []
    for key, (label, color) in ENERGY_METRICS.items():
        if key in frame and frame[key].notna().any():
            figure.add_bar(
                x=frame["day"],
                y=frame[key],
                name=label,
                uid=key,
                marker_color=color,
                customdata=frame[key.replace("_kwh", "_coverage_pct")],
                hovertemplate="%{y:.2f} kWh · %{customdata:.0f} % Abdeckung"
                "<extra>%{fullData.name}</extra>",
            )
    for row in reversed(frame.to_dict("records")):
        cells = [
            html.Th(
                datetime.fromisoformat(row["day"]).strftime("%d.%m.%Y"),
                scope="row",
            )
        ]
        for key in ENERGY_METRICS:
            value = row[key]
            coverage = row[key.replace("_kwh", "_coverage_pct")]
            cells.append(
                html.Td(
                    [
                        html.Span("–" if pd.isna(value) else german_number(value, 2)),
                        html.Small(
                            f"{german_number(coverage)} %", className="coverage"
                        ),
                    ]
                )
            )
        rows.append(html.Tr(cells))
    figure.update_layout(
        barmode="group",
        height=320,
        uirevision=revision,
        margin={"l": 10, "r": 10, "t": 28, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#243746", "family": "Open Sans, sans-serif"},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
        yaxis_title="kWh",
        xaxis={"type": "category"},
    )
    table = html.Div(
        html.Table(
            [
                html.Caption("Tagesenergie", className="visually-hidden"),
                html.Thead(
                    html.Tr(
                        [
                            html.Th("Tag", scope="col"),
                            *[
                                html.Th(label, scope="col")
                                for label, _color in ENERGY_METRICS.values()
                            ],
                        ]
                    )
                ),
                html.Tbody(rows),
            ],
            className="detail-table energy-table",
        ),
        className="table-scroll energy-scroll",
    )
    return figure, table


def auto_refresh_disabled(values: list[str] | None) -> bool:
    return AUTO_REFRESH_VALUE not in (values or [])


def _kpi_card(label: str, component_id: str, *, solar: bool = False) -> html.Div:
    class_name = "kpi-card solar" if solar else "kpi-card"
    return html.Div(
        [
            html.P(label, className="kpi-label"),
            html.P("–", id=component_id, className="kpi-value"),
        ],
        className=class_name,
    )


def _table_alternative_link() -> html.A:
    return html.A(
        "Zur Messwerttabelle",
        href="#measurement-grid",
        className="chart-table-link",
    )


def build_layout(*, auth_enabled: bool = False) -> html.Div:
    today = dashboard_today()
    period_options = [{"label": label, "value": label} for label in PRESETS]
    period_options.append({"label": CUSTOM_RANGE, "value": CUSTOM_RANGE})

    sidebar = html.Aside(
        [
            html.A(
                [
                    html.Img(
                        id="sidebar-logo",
                        src="/static/img/logo-fluxo_io.svg",
                        alt="fluxo.io",
                        className="sidebar-logo",
                    ),
                    html.Span("Private Solardatenbank"),
                ],
                id="sidebar-brand",
                href="/",
                className="sidebar-brand",
            ),
            html.Fieldset(
                [
                    html.Legend("Filter", className="filter-title"),
                    html.Div(
                        [
                            html.Span(
                                "Zeitraum",
                                id="period-filter-label",
                                className="field-label",
                            ),
                            dcc.Dropdown(
                                id="period-filter",
                                options=period_options,
                                value="24 Stunden",
                                clearable=False,
                                searchable=False,
                            ),
                        ],
                        id="period-filter-field",
                        className="filter-field",
                        role="group",
                        **{"aria-labelledby": "period-filter-label"},
                    ),
                    html.Div(
                        [
                            html.Span(
                                "Von / bis",
                                id="custom-range-label",
                                className="field-label",
                            ),
                            html.Label(
                                "Von",
                                htmlFor="custom-range-start",
                                className="visually-hidden",
                            ),
                            html.Label(
                                "Bis",
                                htmlFor="custom-range-end",
                                className="visually-hidden",
                            ),
                            dcc.DatePickerRange(
                                id="custom-range",
                                start_date_id="custom-range-start",
                                end_date_id="custom-range-end",
                                start_date=today - timedelta(days=6),
                                end_date=today,
                                max_date_allowed=today,
                                display_format="DD.MM.YYYY",
                                first_day_of_week=1,
                                minimum_nights=0,
                                clearable=False,
                                updatemode="bothdates",
                            ),
                        ],
                        id="custom-range-wrapper",
                        className="filter-field is-hidden",
                        role="group",
                        **{"aria-labelledby": "custom-range-label"},
                    ),
                    html.Div(
                        [
                            html.Span(
                                "Anlage",
                                id="site-filter-label",
                                className="field-label",
                            ),
                            dcc.Dropdown(
                                id="site-filter",
                                options=[],
                                value=[],
                                multi=True,
                                placeholder="Alle",
                            ),
                        ],
                        id="site-filter-field",
                        className="filter-field",
                        role="group",
                        **{"aria-labelledby": "site-filter-label"},
                    ),
                    html.Div(
                        [
                            html.Span(
                                "Gerät",
                                id="device-filter-label",
                                className="field-label",
                            ),
                            dcc.Dropdown(
                                id="device-filter",
                                options=[],
                                value=[],
                                multi=True,
                                placeholder="Alle",
                            ),
                        ],
                        id="device-filter-field",
                        className="filter-field",
                        role="group",
                        **{"aria-labelledby": "device-filter-label"},
                    ),
                    html.Div(
                        dcc.Checklist(
                            id="valid-filter",
                            options=[{"label": "Nur gültige Werte", "value": "valid"}],
                            value=["valid"],
                            className="dash-checklist",
                        ),
                        className="filter-field",
                    ),
                    html.Button(
                        "Neu laden",
                        id="reload-button",
                        n_clicks=0,
                        className="secondary-button",
                    ),
                    html.P(
                        id="filter-message",
                        className="sidebar-message",
                        role="status",
                        **{"aria-live": "polite"},
                    ),
                ],
                className="filter-card",
            ),
        ],
        className="sidebar",
    )

    main = html.Main(
        [
            html.Header(
                [
                    html.Div(
                        [
                            html.H1("Energieübersicht"),
                            html.P("PV, Speicher, Verbrauch und Netz"),
                        ],
                        className="header-copy",
                    ),
                    html.Div(
                        [
                            html.Div(
                                dcc.Checklist(
                                    id="auto-refresh-toggle",
                                    options=[
                                        {
                                            "label": html.Span(
                                                [
                                                    html.Span("Auto-Refresh"),
                                                    html.Span(
                                                        "An · 60 s",
                                                        className=(
                                                            "refresh-state refresh-on"
                                                        ),
                                                    ),
                                                    html.Span(
                                                        "Aus",
                                                        className=(
                                                            "refresh-state refresh-off"
                                                        ),
                                                    ),
                                                ],
                                                className="auto-refresh-label",
                                            ),
                                            "value": AUTO_REFRESH_VALUE,
                                        }
                                    ],
                                    value=[AUTO_REFRESH_VALUE],
                                    className="auto-refresh-toggle",
                                    persistence=True,
                                    persistence_type="local",
                                ),
                                className="auto-refresh-control",
                                title="Automatisch jede Minute aktualisieren",
                            ),
                            html.Span(
                                "Wird geladen …",
                                id="collector-status",
                                className="status loading",
                                role="status",
                                **{"aria-live": "polite", "aria-atomic": "true"},
                            ),
                            html.Span(
                                "Messzeit: unbekannt",
                                id="source-status",
                                className="status warning",
                                role="status",
                                **{"aria-live": "polite", "aria-atomic": "true"},
                            ),
                            *(
                                [
                                    html.A(
                                        "Abmelden",
                                        id="logout-link",
                                        href="/logout",
                                        className="logout-link",
                                    )
                                ]
                                if auth_enabled
                                else []
                            ),
                        ],
                        className="header-actions",
                    ),
                ],
                className="header",
            ),
            html.Div(
                id="data-message",
                className="message is-hidden",
                role="status",
                **{"aria-live": "polite", "aria-atomic": "true"},
            ),
            html.Div(
                [
                    _kpi_card("PV", "kpi-pv", solar=True),
                    _kpi_card("Akku Ø", "kpi-soc"),
                    _kpi_card("Haus", "kpi-home"),
                    _kpi_card("Netz", "kpi-grid"),
                ],
                className="kpi-grid",
            ),
            html.Div(
                [
                    html.Section(
                        [
                            html.H2("PV-Leistung"),
                            html.P("Gesamt und PV1–PV4", className="card-subtitle"),
                            _table_alternative_link(),
                            dcc.Graph(
                                id="pv-chart",
                                figure=empty_figure("Wird geladen …"),
                                config=GRAPH_CONFIG,
                                responsive=True,
                                style={"height": "340px"},
                            ),
                        ],
                        className="chart-card wide",
                    ),
                    html.Section(
                        [
                            html.H2("Energiefluss"),
                            html.P(
                                "Akku: + Laden / − Entladen · "
                                "Netz: + Bezug / − Einspeisung",
                                className="card-subtitle",
                            ),
                            _table_alternative_link(),
                            dcc.Graph(
                                id="flow-chart",
                                figure=empty_figure("Wird geladen …"),
                                config=GRAPH_CONFIG,
                                responsive=True,
                                style={"height": "340px"},
                            ),
                        ],
                        className="chart-card",
                    ),
                    html.Section(
                        [
                            html.H2("Ladezustand"),
                            html.P(
                                "Durchschnitt aller Geräte",
                                className="card-subtitle",
                            ),
                            _table_alternative_link(),
                            dcc.Graph(
                                id="soc-chart",
                                figure=empty_figure("Wird geladen …"),
                                config=GRAPH_CONFIG,
                                responsive=True,
                                style={"height": "300px"},
                            ),
                        ],
                        className="chart-card",
                    ),
                ],
                className="chart-grid",
            ),
            html.Section(
                [
                    html.H2("Tagesenergie"),
                    html.P(
                        "Geschätzte kWh im gewählten Zeitraum · "
                        "Prozentwerte zeigen die Datenabdeckung",
                        className="card-subtitle",
                    ),
                    html.P(id="energy-summary", className="card-subtitle"),
                    dcc.Graph(
                        id="energy-chart",
                        figure=empty_figure(),
                        config=GRAPH_CONFIG,
                        responsive=True,
                    ),
                    html.Div(id="energy-table"),
                ],
                className="table-card",
            ),
            html.Details(
                [
                    html.Summary(
                        [
                            html.Span("Details"),
                            html.Span(
                                "⌄",
                                className="summary-icon",
                                **{"aria-hidden": "true"},
                            ),
                        ]
                    ),
                    html.H3("Gerätestatus"),
                    html.Div(id="device-freshness"),
                    html.H3("Netzverteilung"),
                    _table_alternative_link(),
                    dcc.Graph(
                        id="grid-paths-chart",
                        figure=empty_figure(),
                        config=GRAPH_CONFIG,
                        responsive=True,
                    ),
                ],
                className="table-card details-card",
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H2("Messwerte"),
                                    html.P(
                                        "0 Werte",
                                        id="table-summary",
                                        className="card-subtitle",
                                    ),
                                ]
                            ),
                            html.Div(
                                [
                                    html.Span(
                                        id="download-message",
                                        className="download-message",
                                        **{"data-toast-message": ""},
                                    ),
                                    html.Button(
                                        "CSV laden",
                                        id="download-button",
                                        n_clicks=0,
                                        disabled=True,
                                        className="primary-button",
                                    ),
                                ],
                                className="table-actions",
                            ),
                        ],
                        className="table-header",
                    ),
                    dag.AgGrid(
                        id="measurement-grid",
                        className="ag-theme-quartz solarbank-grid",
                        columnDefs=GRID_COLUMNS,
                        defaultColDef={
                            "sortable": True,
                            "filter": True,
                            "resizable": True,
                            "minWidth": 105,
                        },
                        dashGridOptions={
                            "ariaLabel": "Messwerte",
                            "pagination": True,
                            "paginationPageSize": 25,
                            "paginationPageSizeSelector": [25, 50, 100],
                            "animateRows": False,
                            "rowSelection": {
                                "mode": "singleRow",
                                "enableClickSelection": True,
                                "checkboxes": False,
                            },
                        },
                        getRowId="params.data._row_id",
                        rowData=[],
                        style={"height": "580px", "width": "100%"},
                    ),
                    dcc.Download(id="csv-download"),
                    html.Details(
                        [
                            html.Summary(
                                [
                                    html.Span("Messwertdetails"),
                                    html.Span(
                                        "⌄",
                                        className="summary-icon",
                                        **{"aria-hidden": "true"},
                                    ),
                                ]
                            ),
                            html.Div(
                                "Eine Zeile auswählen, um die Rohdaten einzusehen.",
                                id="snapshot-details",
                                className="snapshot-details",
                                role="status",
                                **{"aria-live": "polite"},
                            ),
                        ],
                        className="snapshot-panel",
                    ),
                ],
                className="table-card",
            ),
            dcc.Interval(
                id="auto-refresh-interval",
                interval=AUTO_REFRESH_INTERVAL_MS,
                n_intervals=0,
                disabled=False,
            ),
            html.Footer(
                [
                    html.Div(
                        [
                            html.Img(
                                id="footer-logo",
                                src="/static/img/logo-fluxo_io.svg",
                                alt="",
                            ),
                            html.Span(
                                f"© {today.year} fluxo.io · Private Solardatenbank"
                            ),
                        ],
                        className="footer-brand",
                    ),
                    html.Nav(
                        [
                            html.A("Impressum", href="/impressum"),
                            html.A("Datenschutz", href="/datenschutz"),
                        ],
                        className="footer-links",
                        **{"aria-label": "Rechtliches"},
                    ),
                ],
                id="app-footer",
                className="app-footer",
            ),
        ],
        id="main-content",
        className="main-content",
        tabIndex=-1,
    )
    skip_link = html.A(
        "Zum Inhalt",
        href="#main-content",
        className="skip-link",
    )
    return html.Div([skip_link, sidebar, main], className="app-shell")


def register_callbacks(dash_app: Dash) -> None:
    @dash_app.callback(
        Output("auto-refresh-interval", "disabled"),
        Input("auto-refresh-toggle", "value"),
    )
    def toggle_auto_refresh(values: list[str] | None) -> bool:
        return auto_refresh_disabled(values)

    @dash_app.callback(
        Output("custom-range-wrapper", "className"),
        Input("period-filter", "value"),
    )
    def toggle_custom_range(period: str | None) -> str:
        return "filter-field" if period == CUSTOM_RANGE else "filter-field is-hidden"

    @dash_app.callback(
        Output("site-filter", "options"),
        Output("device-filter", "options"),
        Output("device-filter", "value"),
        Output("filter-message", "children"),
        Input("reload-button", "n_clicks"),
        Input("site-filter", "value"),
        State("device-filter", "value"),
    )
    def update_filter_options(
        _reloads: int,
        selected_sites: list[str] | None,
        selected_devices: list[str] | None,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str], str]:
        try:
            devices = fetch_devices(settings())
            site_options, device_options = filter_options(devices, selected_sites)
        except (ConfigurationError, psycopg.Error, RuntimeError):
            LOGGER.exception("Filter konnten nicht geladen werden")
            return [], [], [], "Filter nicht verfügbar."
        eligible_values = {option["value"] for option in device_options}
        retained = [
            value for value in selected_devices or [] if value in eligible_values
        ]
        return site_options, device_options, retained, ""

    @dash_app.callback(
        Output("collector-status", "children"),
        Output("collector-status", "className"),
        Output("data-message", "children"),
        Output("data-message", "className"),
        Output("kpi-pv", "children"),
        Output("kpi-soc", "children"),
        Output("kpi-home", "children"),
        Output("kpi-grid", "children"),
        Output("pv-chart", "figure"),
        Output("flow-chart", "figure"),
        Output("soc-chart", "figure"),
        Output("measurement-grid", "rowData"),
        Output("table-summary", "children"),
        Output("download-button", "disabled"),
        Output("source-status", "children"),
        Output("source-status", "className"),
        Output("device-freshness", "children"),
        Output("energy-chart", "figure"),
        Output("energy-table", "children"),
        Output("energy-summary", "children"),
        Output("grid-paths-chart", "figure"),
        Input("period-filter", "value"),
        Input("custom-range", "start_date"),
        Input("custom-range", "end_date"),
        Input("site-filter", "value"),
        Input("device-filter", "value"),
        Input("valid-filter", "value"),
        Input("reload-button", "n_clicks"),
        Input("auto-refresh-interval", "n_intervals"),
        running=[
            (Output("reload-button", "disabled"), True, False),
            (Output("reload-button", "children"), "Lädt …", "Neu laden"),
        ],
    )
    def update_dashboard(
        period: str | None,
        start_value: str | None,
        end_value: str | None,
        sites: list[str] | None,
        device_values: list[str] | None,
        valid_values: list[str] | None,
        _reloads: int,
        _auto_refreshes: int,
    ) -> tuple[Any, ...]:
        blank_figures = graph_figures(pd.DataFrame())
        extra = (
            "Messzeit: unbekannt",
            "status warning",
            [],
            empty_figure(),
            [],
            "Keine Energiedaten.",
            empty_figure(),
        )
        try:
            config = settings()
            window = resolve_window(period, start_value, end_value, config)
            rows = fetch_measurements(
                config,
                start=window.start,
                end=window.end,
                site_ids=tuple(sites or []),
                devices=decode_devices(device_values),
                valid_only="valid" in (valid_values or []),
            )
            state = fetch_collector_state(config)
            latest_rows = fetch_latest_measurements(
                config,
                site_ids=tuple(sites or []),
                devices=decode_devices(device_values),
            )
        except ValueError as error:
            return (
                "Filter prüfen",
                "status error",
                str(error),
                "message error",
                "–",
                "–",
                "–",
                "–",
                *blank_figures,
                [],
                "0 Werte",
                True,
                *extra,
            )
        except (ConfigurationError, psycopg.Error, RuntimeError):
            LOGGER.exception("Dashboard-Daten konnten nicht geladen werden")
            return (
                "Datenbank nicht erreichbar",
                "status error",
                "Messwerte konnten nicht geladen werden.",
                "message error",
                "–",
                "–",
                "–",
                "–",
                *blank_figures,
                [],
                "0 Werte",
                True,
                *extra,
            )

        status_text, status_class, collector_warning = collector_status(state, config)
        source_text, source_class, source_details = freshness_view(latest_rows, config)
        if not rows:
            message = collector_warning or "Keine Daten für diesen Filter."
            return (
                status_text,
                status_class,
                message,
                "message warning" if collector_warning else "message info",
                "–",
                "–",
                "–",
                "–",
                *blank_figures,
                [],
                "0 Werte",
                True,
                source_text,
                source_class,
                source_details,
                empty_figure(),
                [],
                "Keine Energiedaten.",
                empty_figure(),
            )

        frame = source_frame(measurement_frame(rows, config), config)
        chart_frame = frame.copy()
        # Keep rejected readings visible in the raw table, but never draw them
        # as a valid power curve or count them toward an energy estimate.
        power_columns = [column for column in chart_frame if column.endswith("_w")]
        chart_frame.loc[
            chart_frame["source_stale"], [*power_columns, "battery_soc_pct"]
        ] = float("nan")
        expected_devices = [(row["site_id"], row["device_sn"]) for row in latest_rows]
        aggregated = aggregate_measurements(
            chart_frame, expected_devices=expected_devices
        )
        revision = json.dumps(
            [
                period,
                start_value if period == CUSTOM_RANGE else None,
                end_value if period == CUSTOM_RANGE else None,
                sorted(sites or []),
                sorted(device_values or []),
                sorted(valid_values or []),
            ]
        )
        figures = graph_figures(
            aggregated, revision=revision, interval_seconds=config.poll_interval_seconds
        )
        grid_data = table_frame(frame, limit=TABLE_ROW_LIMIT).to_dict("records")
        selected_rows = frame.sort_values("bucket_start", ascending=False).head(
            TABLE_ROW_LIMIT
        )
        for record, source in zip(
            grid_data, selected_rows.to_dict("records"), strict=True
        ):
            record["_timestamp"] = source["bucket_start"].isoformat()
            record["_row_id"] = json.dumps(
                [record["_timestamp"], record["site_id"], record["device_sn"]]
            )
        loaded_count, filtered_count = measurement_counts(rows)
        warnings = [collector_warning] if collector_warning else []
        if filtered_count > len(rows):
            warnings.append("Zu viele Werte. Bitte Zeitraum verkürzen.")
        message = " · ".join(warnings)
        message_class = "message warning" if warnings else "message is-hidden"
        summary = table_summary(loaded_count, filtered_count)
        if filtered_count > loaded_count:
            energy_figure, energy_table = empty_figure("Zeitraum verkürzen"), []
            energy_summary = "Für die Energiebilanz bitte den Zeitraum verkürzen."
        else:
            energy = daily_energy(
                frame,
                window.start,
                window.end,
                config.timezone_name,
                config.poll_interval_seconds,
                expected_devices=expected_devices,
            )
            energy_figure, energy_table = energy_view(energy, revision)
            energy_summary = (
                "Fehlende oder veraltete Werte werden nicht hochgerechnet. "
                "Randtage beziehen sich nur auf den gewählten Zeitraum."
            )
            if frame["source_status"].isin(["missing", "unknown_timezone"]).any():
                energy_summary += " Das Alter einzelner Cloud-Messwerte ist unbekannt."
        grid_paths = line_figure(
            aggregated,
            ["grid_import_w", "grid_export_w", "grid_to_home_w", "grid_to_battery_w"],
            y_title="W",
            revision=revision,
            interval_seconds=config.poll_interval_seconds,
        )

        state_of_charge = latest_value(aggregated, "battery_soc_pct")
        return (
            status_text,
            status_class,
            message,
            message_class,
            format_power(latest_value(aggregated, "pv_total_w")),
            ("–" if state_of_charge is None else f"{german_number(state_of_charge)} %"),
            format_power(latest_value(aggregated, "home_load_w")),
            format_grid(latest_value(aggregated, "grid_power_w")),
            *figures,
            grid_data,
            summary,
            False,
            source_text,
            source_class,
            source_details,
            energy_figure,
            energy_table,
            energy_summary,
            grid_paths,
        )

    @dash_app.callback(
        Output("snapshot-details", "children"),
        Input("measurement-grid", "selectedRows"),
        prevent_initial_call=True,
    )
    def show_snapshot(selected: list[dict[str, Any]] | None) -> Any:
        if not selected:
            return "Eine Zeile auswählen, um die Rohdaten einzusehen."
        try:
            row = selected[0]
            config = settings()
            snapshot = fetch_snapshot(
                config,
                timestamp=datetime.fromisoformat(row["_timestamp"]),
                site_id=row["site_id"],
                device_sn=row["device_sn"],
            )
            if snapshot is None:
                return "Messwert nicht mehr verfügbar."
            return snapshot_view(snapshot, config)
        except (KeyError, TypeError, ValueError, ConfigurationError, psycopg.Error):
            LOGGER.exception("Messwertdetails konnten nicht geladen werden")
            return "Details nicht verfügbar."

    @dash_app.callback(
        Output("csv-download", "data"),
        Output("download-message", "children"),
        Input("download-button", "n_clicks"),
        Input("period-filter", "value"),
        Input("custom-range", "start_date"),
        Input("custom-range", "end_date"),
        Input("site-filter", "value"),
        Input("device-filter", "value"),
        Input("valid-filter", "value"),
        Input("reload-button", "n_clicks"),
        prevent_initial_call=True,
        running=[
            (Output("download-button", "children"), "Erstellt …", "CSV laden"),
        ],
    )
    def download_csv(
        clicks: int | None,
        period: str | None,
        start_value: str | None,
        end_value: str | None,
        sites: list[str] | None,
        device_values: list[str] | None,
        valid_values: list[str] | None,
        _reloads: int,
    ) -> tuple[Any, Any]:
        if ctx.triggered_id != "download-button":
            return no_update, ""
        if not clicks:
            raise PreventUpdate
        try:
            config = settings()
            window = resolve_window(period, start_value, end_value, config)
            rows = fetch_measurements(
                config,
                start=window.start,
                end=window.end,
                site_ids=tuple(sites or []),
                devices=decode_devices(device_values),
                valid_only="valid" in (valid_values or []),
            )
            if not rows:
                return no_update, toast_message("Keine Daten.", "warning", clicks)
            loaded_count, filtered_count = measurement_counts(rows)
            if filtered_count > loaded_count:
                return no_update, toast_message(
                    "Zu viele Werte. Zeitraum verkürzen.", "warning", clicks
                )
            frame = measurement_frame(rows, config)
            content = export_frame(frame).to_csv(index=False, sep=";", decimal=",")
            return dcc.send_string(
                content, "solarbank-messwerte.csv"
            ), toast_message("CSV erstellt.", "success", clicks)
        except (ConfigurationError, ValueError, psycopg.Error, RuntimeError):
            LOGGER.exception("CSV konnte nicht erstellt werden")
            return no_update, toast_message(
                "CSV nicht verfügbar.", "danger", clicks
            )


def create_app() -> Dash:
    auth_config = AuthSettings.from_environment()
    dash_app = Dash(
        __name__,
        title=DASHBOARD_TITLE,
        update_title="Wird geladen …",
        assets_folder=str(ASSETS_FOLDER),
        external_scripts=["/static/toasts.js"],
        external_stylesheets=["/static/toasts.css"],
        meta_tags=[
            {
                "name": "description",
                "content": "Fluxo-Demoprojekt für private Anker-SOLIX-Daten.",
            },
            {"name": "theme-color", "content": "#007aaf"},
        ],
    )
    configure_auth(dash_app.server, auth_config)
    register_public_pages(dash_app.server)

    def serve_layout() -> html.Div:
        return build_layout(auth_enabled=auth_config.enabled)

    dash_app.layout = serve_layout
    register_callbacks(dash_app)

    @dash_app.server.get("/health")
    def health() -> tuple[Any, int]:
        try:
            verify_schema(settings())
        except (ConfigurationError, psycopg.Error, RuntimeError):
            LOGGER.warning("Dashboard nicht bereit", exc_info=True)
            return jsonify(status="unhealthy"), 503
        return jsonify(status="ok"), 200

    return dash_app


app = create_app()
server = app.server


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8501, debug=False)
