from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import solarbank_dashboard.app as app_module
from solarbank_dashboard.app import (
    AUTO_REFRESH_INTERVAL_MS,
    CUSTOM_RANGE,
    DASHBOARD_TITLE,
    GRID_COLUMNS,
    auto_refresh_disabled,
    collector_status,
    create_app,
    dashboard_today,
    decode_devices,
    encode_device,
    export_frame,
    filter_options,
    measurement_counts,
    resolve_window,
    table_frame,
    table_summary,
    toast_message,
)
from solarbank_dashboard.config import Settings
from solarbank_dashboard.repository import Device


def _find_component(component: object, component_id: str) -> object | None:
    if getattr(component, "id", None) == component_id:
        return component
    children = getattr(component, "children", None)
    if children is None:
        return None
    candidates = children if isinstance(children, list | tuple) else [children]
    for candidate in candidates:
        found = _find_component(candidate, component_id)
        if found is not None:
            return found
    return None


def _settings() -> Settings:
    return Settings(
        db_host="database",
        db_port=5432,
        db_name="solarbank",
        db_user="solarbank_dashboard",
        db_password="secret",
        timezone_name="Europe/Berlin",
    )


def test_device_filter_values_round_trip_and_follow_site_selection() -> None:
    devices = [
        Device("site-b", "B-1", "Garage", "A17C5"),
        Device("site-a", "A-1", None, "A17C5"),
    ]

    sites, options = filter_options(devices, ["site-a"])

    assert [option["value"] for option in sites] == ["site-a", "site-b"]
    assert len(options) == 1
    encoded = encode_device(devices[1])
    assert options[0]["value"] == encoded
    assert decode_devices([encoded]) == (("site-a", "A-1"),)


@pytest.mark.parametrize("value", ["not-json", "[]", '["site", 42]'])
def test_invalid_device_filter_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="Gerätefilter"):
        decode_devices([value])


def test_custom_window_uses_local_complete_days() -> None:
    result = resolve_window(
        CUSTOM_RANGE,
        "2026-09-20",
        "2026-09-21",
        _settings(),
    )

    assert result.start == datetime(2026, 9, 19, 22, tzinfo=UTC)
    assert result.end == datetime(2026, 9, 21, 22, tzinfo=UTC)


def test_dashboard_date_uses_configured_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHBOARD_TIMEZONE", "Europe/Berlin")

    result = dashboard_today(datetime(2026, 9, 21, 22, 30, tzinfo=UTC))

    assert result == datetime(2026, 9, 22, tzinfo=UTC).date()


def test_table_records_are_json_safe_and_exported_in_german() -> None:
    frame = pd.DataFrame(
        [
            {
                "bucket_start": datetime(2026, 9, 21, 12, tzinfo=UTC),
                "site_id": "site-a",
                "device_sn": "device-a",
                "data_valid": False,
                "pv_reported_total_w": float("nan"),
                "pv_calculated_total_w": 50.0,
                "pv_1_w": 50.0,
            }
        ]
    )

    records = table_frame(frame).to_dict("records")

    assert records[0]["bucket_start"] == "2026-09-21 12:00 +00:00"
    assert records[0]["data_valid"] == "Nein"
    assert records[0]["pv_total_w"] == 50.0
    json.dumps(records, allow_nan=False)
    exported = export_frame(frame)
    assert list(exported.columns)[0:4] == ["Zeit", "Anlage", "Gerät", "Gültig"]
    assert exported.loc[0, "Zeit"] == "21.09.2026 12:00 +00:00"
    assert exported.loc[0, "PV gesamt (W)"] == 50.0


def test_table_timestamps_remain_unique_across_daylight_saving_fallback() -> None:
    frame = pd.DataFrame(
        [
            {
                "bucket_start": datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
                "site_id": "site-a",
                "device_sn": "device-a",
            },
            {
                "bucket_start": datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
                "site_id": "site-a",
                "device_sn": "device-a",
            },
        ]
    )
    frame["bucket_start"] = pd.to_datetime(
        frame["bucket_start"], utc=True
    ).dt.tz_convert("Europe/Berlin")

    records = table_frame(frame).to_dict("records")

    assert {record["bucket_start"] for record in records} == {
        "2026-10-25 02:30 +02:00",
        "2026-10-25 02:30 +01:00",
    }
    assert GRID_COLUMNS[0]["comparator"] == {"function": "timestampComparator"}


def test_counts_use_total_match_count_for_truncated_results() -> None:
    rows = [{"filtered_row_count": 150_000}] * 100_000

    loaded_count, filtered_count = measurement_counts(rows)

    assert (loaded_count, filtered_count) == (100_000, 150_000)
    assert table_summary(loaded_count, filtered_count) == (
        "Neueste 5.000 von 150.000 Werten"
    )


def test_collector_status_warns_when_last_measurement_is_stale() -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    state = {
        "last_success_at": now - timedelta(minutes=16),
        "consecutive_failures": 0,
    }

    text, class_name, warning = collector_status(state, _settings(), now=now)

    assert text.startswith("Abgerufen:")
    assert class_name == "status warning"
    assert warning == "Collector: keine aktuellen Daten"


def test_auto_refresh_can_be_disabled() -> None:
    assert AUTO_REFRESH_INTERVAL_MS == 60_000
    assert auto_refresh_disabled(["enabled"]) is False
    assert auto_refresh_disabled([]) is True
    assert auto_refresh_disabled(None) is True


def test_toast_message_uses_text_and_event_metadata() -> None:
    message = toast_message("CSV erstellt.", "success", 3)
    props = message.to_plotly_json()["props"]

    assert props["children"] == "CSV erstellt."
    assert props["data-toast-text"] == ""
    assert props["data-toast-type"] == "success"
    assert props["data-toast-event"] == "3"


def test_layout_contains_enabled_auto_refresh() -> None:
    layout = app_module.build_layout()

    toggle = _find_component(layout, "auto-refresh-toggle")
    interval = _find_component(layout, "auto-refresh-interval")

    assert toggle is not None
    assert toggle.value == ["enabled"]
    assert toggle.persistence is True
    assert toggle.persistence_type == "local"
    assert interval is not None
    assert interval.interval == AUTO_REFRESH_INTERVAL_MS
    assert interval.disabled is False


def test_layout_shows_logout_only_when_authentication_is_enabled() -> None:
    protected_layout = app_module.build_layout(auth_enabled=True)
    local_layout = app_module.build_layout()

    logout = _find_component(protected_layout, "logout-link")
    assert logout is not None
    assert logout.href == "/logout"
    assert _find_component(local_layout, "logout-link") is None


def test_layout_contains_fluxo_branding_and_legal_footer() -> None:
    layout = app_module.build_layout()

    brand = _find_component(layout, "sidebar-brand")
    sidebar_logo = _find_component(layout, "sidebar-logo")
    footer = _find_component(layout, "app-footer")
    footer_logo = _find_component(layout, "footer-logo")
    main = _find_component(layout, "main-content")

    assert brand is not None
    assert brand.href == "/"
    assert brand.children[1].children == "Private Solardatenbank"
    assert sidebar_logo is not None
    assert sidebar_logo.src == "/static/img/logo-fluxo_io.svg"
    assert sidebar_logo.alt == "fluxo.io"
    assert footer is not None
    assert footer.children[0].children[1].children == (
        f"© {date.today().year} fluxo.io · Private Solardatenbank"
    )
    assert [link.href for link in footer.children[1].children] == [
        "/impressum",
        "/datenschutz",
    ]
    assert footer_logo is not None
    assert footer_logo.src == "/static/img/logo-fluxo_io.svg"
    assert footer_logo.alt == ""
    assert main is not None
    assert main.tabIndex == -1


def test_layout_exposes_accessible_navigation_and_statuses() -> None:
    layout = app_module.build_layout()

    assert layout.children[0].href == "#main-content"
    assert layout.children[0].children == "Zum Inhalt"
    assert _find_component(layout, "period-filter") is not None
    assert _find_component(layout, "period-filter-field").role == "group"
    assert _find_component(layout, "site-filter-field").role == "group"
    assert _find_component(layout, "device-filter-field").role == "group"
    assert _find_component(layout, "measurement-grid").dashGridOptions["ariaLabel"] == (
        "Messwerte"
    )
    assert _find_component(layout, "collector-status").role == "status"
    assert _find_component(layout, "data-message").role == "status"
    assert _find_component(layout, "snapshot-details").role == "status"
    download_message = _find_component(layout, "download-message")
    assert download_message.to_plotly_json()["props"]["data-toast-message"] == ""

    table_links = []

    def collect(component: object) -> None:
        if getattr(component, "className", None) == "chart-table-link":
            table_links.append(component)
        children = getattr(component, "children", None)
        if children is None:
            return
        for child in children if isinstance(children, list | tuple) else [children]:
            if hasattr(child, "children"):
                collect(child)

    collect(layout)
    assert len(table_links) == 4
    assert all(link.href == "#measurement-grid" for link in table_links)


def test_app_shell_has_no_database_dependency() -> None:
    dash_app = create_app()

    response = dash_app.server.test_client().get("/")

    assert response.status_code == 200
    assert dash_app.title == DASHBOARD_TITLE
    assert b"<title>Private Solardatenbank \xc2\xb7 fluxo.io</title>" in response.data
    assert b"session-redirect.js" in response.data
    assert b"/static/toasts.css" in response.data
    assert b"/static/toasts.js" in response.data
    assert b'content="#007aaf"' in response.data
    assert len(dash_app.callback_map) == 6
    assert "auto-refresh-interval.disabled" in dash_app.callback_map

    dashboard_callback = next(
        callback
        for output, callback in dash_app.callback_map.items()
        if "collector-status.children" in output
    )
    assert {
        "id": "auto-refresh-interval",
        "property": "n_intervals",
    } in dashboard_callback["inputs"]

    csv_callback = next(
        callback
        for output, callback in dash_app.callback_map.items()
        if "csv-download.data" in output
    )
    assert {
        "id": "auto-refresh-interval",
        "property": "n_intervals",
    } not in csv_callback["inputs"]


def test_csv_callback_returns_typed_toast_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "settings", _settings)
    monkeypatch.setattr(
        app_module, "ctx", SimpleNamespace(triggered_id="download-button")
    )
    dash_app = create_app()
    callback = next(
        value
        for key, value in dash_app.callback_map.items()
        if "csv-download.data" in key
    )["callback"].__wrapped__

    def download(clicks: int) -> tuple[object, object]:
        return callback(
            clicks,
            "24 Stunden",
            None,
            None,
            [],
            [],
            ["valid"],
            0,
        )

    def feedback_props(feedback: object) -> dict[str, object]:
        return feedback.to_plotly_json()["props"]

    monkeypatch.setattr(app_module, "fetch_measurements", lambda *args, **kwargs: [])
    payload, feedback = download(1)
    assert payload is app_module.no_update
    assert feedback_props(feedback)["data-toast-type"] == "warning"
    assert feedback_props(feedback)["children"] == "Keine Daten."

    timestamp = datetime.now(UTC).replace(second=0, microsecond=0)
    rows = [
        {
            "bucket_start": timestamp,
            "site_id": "site-a",
            "device_sn": "device-a",
            "data_valid": True,
            "pv_reported_total_w": 100.0,
            "filtered_row_count": 1,
        }
    ]
    monkeypatch.setattr(
        app_module, "fetch_measurements", lambda *args, **kwargs: rows
    )
    payload, feedback = download(2)
    assert payload["filename"] == "solarbank-messwerte.csv"
    assert feedback_props(feedback)["data-toast-type"] == "success"
    assert feedback_props(feedback)["children"] == "CSV erstellt."

    rows[0]["filtered_row_count"] = 2
    payload, feedback = download(3)
    assert payload is app_module.no_update
    assert feedback_props(feedback)["data-toast-type"] == "warning"
    assert feedback_props(feedback)["children"] == (
        "Zu viele Werte. Zeitraum verkürzen."
    )

    def fail(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(app_module, "fetch_measurements", fail)
    payload, feedback = download(4)
    assert payload is app_module.no_update
    assert feedback_props(feedback)["data-toast-type"] == "danger"
    assert feedback_props(feedback)["children"] == "CSV nicht verfügbar."


def test_health_reports_database_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "settings", _settings)
    monkeypatch.setattr(app_module, "verify_schema", lambda _config: None)
    dash_app = create_app()

    response = dash_app.server.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_health_fails_when_schema_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "settings", _settings)

    def fail(_config: Settings) -> None:
        raise RuntimeError("missing schema")

    monkeypatch.setattr(app_module, "verify_schema", fail)
    dash_app = create_app()

    response = dash_app.server.test_client().get("/health")

    assert response.status_code == 503
    assert response.get_json() == {"status": "unhealthy"}


@pytest.mark.parametrize("has_rows", [True, False])
def test_dashboard_callback_renders_energy_and_keeps_refresh_revision(
    monkeypatch: pytest.MonkeyPatch,
    has_rows: bool,
) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    rows = (
        [
            {
                "bucket_start": now - timedelta(minutes=5),
                "collected_at": now,
                "source_updated_at": now.isoformat(),
                "site_id": "site-a",
                "device_sn": "test-device",
                "data_valid": True,
                "pv_reported_total_w": 600.0,
                "pv_calculated_total_w": None,
                "pv_1_w": 150.0,
                "battery_soc_pct": 60.0,
                "battery_power_w": 100.0,
                "home_load_w": 200.0,
                "grid_power_w": 0.0,
                "grid_import_w": 0.0,
                "grid_export_w": 0.0,
                "grid_to_home_w": 0.0,
                "grid_to_battery_w": 0.0,
            }
        ]
        if has_rows
        else []
    )
    monkeypatch.setattr(app_module, "settings", _settings)
    monkeypatch.setattr(app_module, "fetch_measurements", lambda *args, **kwargs: rows)
    monkeypatch.setattr(
        app_module, "fetch_latest_measurements", lambda *args, **kwargs: rows
    )
    monkeypatch.setattr(
        app_module,
        "fetch_collector_state",
        lambda *args: {
            "last_success_at": now,
            "consecutive_failures": 0,
        },
    )
    dash_app = create_app()
    callback = next(
        value
        for key, value in dash_app.callback_map.items()
        if "collector-status.children" in key
    )
    update = callback["callback"].__wrapped__
    first = update("24 Stunden", None, None, [], [], ["valid"], 0, 0)
    second = update("24 Stunden", None, None, [], [], ["valid"], 0, 1)
    assert len(first) == len(callback["output"])
    assert first[0].startswith("Abgerufen:")
    if has_rows:
        assert first[8].layout.uirevision == second[8].layout.uirevision
        assert first[11][0]["_timestamp"]
        assert len(first[17].data) == 6
        changed = update("7 Tage", None, None, [], [], ["valid"], 0, 1)
        assert first[8].layout.uirevision != changed[8].layout.uirevision
