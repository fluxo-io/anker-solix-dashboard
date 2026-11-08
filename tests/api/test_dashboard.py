from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from solarbank_api.app import create_app
from solarbank_api.config import Settings
from solarbank_api.dashboard import (
    collector_status,
    dashboard,
    freshness_status,
    source_frame,
)

START = datetime(2026, 9, 22, 12, tzinfo=UTC)
TOKEN = "test-read-" + "r" * 32
WRITE_TOKEN = "test-write-" + "w" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}
WRITE_HEADERS = {"Authorization": "Bearer " + WRITE_TOKEN}


@pytest.fixture
def app(tmp_path):
    return create_app(
        Settings(
            tmp_path / "app.sqlite3",
            TOKEN,
            "s" * 32,
            admin_username="admin",
            admin_password="synthetic-password",
            collector_api_token=WRITE_TOKEN,
            timezone_name="UTC",
            source_timezone_name="Europe/Berlin",
        )
    )


def sample(**updates):
    return {
        "bucket_start": START.isoformat(),
        "collected_at": START.isoformat(),
        "source_updated_at": START.isoformat(),
        "site_id": "site",
        "device_sn": "a",
        "data_valid": True,
        "pv_reported_total_w": 600,
        "battery_soc_pct": 50,
        "home_load_w": 120,
        "grid_power_w": -30,
        "battery_power_w": -60,
        "raw_data": {"synthetic": True},
        **updates,
    }


def save(app, *rows):
    response = app.test_client().post(
        "/v1/measurements", headers=WRITE_HEADERS, json={"rows": rows}
    )
    assert response.status_code == 200


def query(**updates):
    return {
        "start": START.isoformat(),
        "end": (START + timedelta(hours=1)).isoformat(),
        **updates,
    }


def calculate(app, **updates):
    store = app.extensions["solarbank_store"]
    return dashboard(
        store, store.settings, query(**updates), now=START + timedelta(hours=1)
    )


def test_dashboard_endpoint_calculates_stored_data_and_service_permissions(app):
    save(
        app,
        sample(),
        sample(device_sn="b", pv_reported_total_w=300, battery_soc_pct=80),
    )
    client = app.test_client()
    assert client.get("/v1/dashboard", query_string=query()).status_code == 401
    assert (
        client.get(
            "/v1/dashboard", headers=WRITE_HEADERS, query_string=query()
        ).status_code
        == 403
    )
    result = client.get("/v1/dashboard", headers=HEADERS, query_string=query())
    assert result.status_code == 200
    data = result.json
    assert data["kpis"] == {
        "pv_total_w": 900,
        "home_load_w": 120,
        "grid_power_w": -30,
        "battery_soc_pct": 65,
    }
    assert data["energy"][0]["pv_kwh"] == pytest.approx(0.075)
    assert data["energy"][0]["home_kwh"] == pytest.approx(0.01)
    assert data["energy"][0]["pv_coverage_pct"] == pytest.approx(100 / 12)
    assert data["rows"][0]["pv_total_w"] in {300, 600}
    assert data["loaded_count"] == data["filtered_count"] == 2
    assert data["energy_status"] == "available"
    assert "raw_data" not in data["rows"][0]
    assert "NaN" not in result.text and "Infinity" not in result.text
    assert result.headers["Cache-Control"] == "no-store"


def test_historical_eligibility_compares_source_to_collection_not_today(app):
    store = app.extensions["solarbank_store"]
    settings = store.settings
    frame = pd.DataFrame(
        [
            {
                "collected_at": START,
                "source_updated_at": "2026-09-22 14:00:00",
                "data_valid": True,
            },
            {
                "collected_at": START + timedelta(hours=1),
                "source_updated_at": "2026-09-22 14:00:00",
                "data_valid": True,
            },
        ]
    )
    assert source_frame(frame, settings)["source_stale"].tolist() == [False, True]
    save(app, sample())
    data = calculate(app)
    assert data["energy"][0]["pv_kwh"] == pytest.approx(0.05)
    assert data["freshness"]["rows"][0]["status"] == "collection_stale"
    assert data["collector"]["status"] == "stale"


def test_fresh_collection_does_not_hide_stale_source(app):
    settings = app.extensions["solarbank_store"].settings
    data = freshness_status(
        [sample(source_updated_at=(START - timedelta(hours=1)).isoformat())],
        settings,
        now=START,
    )
    assert data["rows"][0]["status"] == "stale"
    assert data["warning"]


def test_invalid_and_known_stale_values_never_supply_chart_kpi_or_energy(app):
    save(
        app,
        sample(data_valid=False),
        sample(
            device_sn="b", source_updated_at=(START - timedelta(hours=2)).isoformat()
        ),
    )
    data = calculate(app, valid_only="false")
    assert all(value is None for value in data["kpis"].values())
    assert data["energy"][0]["pv_kwh"] is None
    assert data["energy"][0]["pv_coverage_pct"] == 0
    assert {row["source_status"] for row in data["rows"]} == {"invalid_data", "stale"}
    assert all(row["pv_total_w"] == 600 for row in data["rows"])


def test_missing_selected_device_and_site_scopes_keep_previous_coverage(app):
    save(
        app,
        sample(),
        sample(device_sn="b", bucket_start=(START - timedelta(days=1)).isoformat()),
    )
    data = calculate(app)
    assert data["kpis"]["pv_total_w"] is None
    assert data["kpis"]["home_load_w"] == 120
    assert data["energy"][0]["pv_coverage_pct"] == pytest.approx(100 / 24)
    assert data["energy"][0]["home_coverage_pct"] == pytest.approx(100 / 12)


def test_unknown_source_time_is_labelled_but_not_discarded(app):
    save(app, sample(source_updated_at=None))
    data = calculate(app)
    assert data["unknown_source_age"] is True
    assert data["energy"][0]["pv_kwh"] == pytest.approx(0.05)


def test_incomplete_buckets_disable_energy_even_when_no_bucket_fits(app):
    save(
        app,
        sample(),
        sample(device_sn="b"),
        sample(bucket_start=(START + timedelta(minutes=5)).isoformat()),
    )
    data = calculate(app, limit="2")
    assert data["loaded_count"] == 1 and data["filtered_count"] == 3
    assert data["truncated"] and data["energy_status"] == "truncated"
    assert data["energy"] == []
    save(
        app,
        sample(device_sn="b", bucket_start=(START + timedelta(minutes=5)).isoformat()),
    )
    empty = calculate(app, limit="1")
    assert empty["loaded_count"] == 0 and empty["filtered_count"] == 4
    assert empty["energy_status"] == "truncated"


def test_table_cap_does_not_reduce_curve_or_energy(app):
    rows = [
        sample(
            bucket_start=(START + timedelta(minutes=5 * index)).isoformat(),
            collected_at=(START + timedelta(minutes=5 * index)).isoformat(),
            source_updated_at=None,
        )
        for index in range(5001)
    ]
    for start in range(0, len(rows), 1000):
        save(app, *rows[start : start + 1000])
    data = calculate(app, end=(START + timedelta(days=20)).isoformat())
    assert data["loaded_count"] == data["filtered_count"] == 5001
    assert len(data["rows"]) == 5000 and len(data["aggregated"]) == 5001
    assert data["energy_status"] == "available"
    assert not data["truncated"]


@pytest.mark.parametrize(
    "bad",
    [
        {"end": START.isoformat()},
        {"limit": "100001"},
        {"valid_only": "yes"},
        {"devices": "[null]"},
    ],
)
def test_dashboard_validates_browser_filters_on_api(app, bad):
    assert (
        app.test_client()
        .get("/v1/dashboard", headers=HEADERS, query_string=query(**bad))
        .status_code
        == 400
    )


def test_dashboard_response_and_selected_devices_are_bounded(app, monkeypatch):
    import solarbank_api.app as module
    import solarbank_api.dashboard as calculations

    save(app, sample(), sample(device_sn="b"))
    monkeypatch.setattr(calculations, "MAX_DEVICES", 1)
    assert (
        app.test_client()
        .get("/v1/dashboard", headers=HEADERS, query_string=query())
        .status_code
        == 400
    )
    monkeypatch.setattr(calculations, "MAX_DEVICES", 1000)
    monkeypatch.setattr(module, "MAX_DASHBOARD_BYTES", 100)
    assert (
        app.test_client()
        .get("/v1/dashboard", headers=HEADERS, query_string=query())
        .status_code
        == 400
    )


def test_missing_store_has_no_dashboard_fallback(app):
    path = app.extensions["solarbank_store"].settings.db_path
    path.unlink()
    assert (
        app.test_client()
        .get("/v1/dashboard", headers=HEADERS, query_string=query())
        .status_code
        == 503
    )
    assert not path.exists()


def test_api_validates_authoritative_time_configuration(app, monkeypatch):
    config = app.extensions["solarbank_store"].settings
    for changes in (
        {"poll_interval_seconds": 59},
        {"poll_interval_seconds": 86401},
        {"timezone_name": "bad"},
        {"source_timezone_name": "bad"},
    ):
        with pytest.raises(ValueError):
            replace(config, **changes)
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("SECRET_KEY", "s" * 32)
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "invalid")
    with pytest.raises(ValueError, match="POLL_INTERVAL_SECONDS"):
        Settings.from_environment()


def test_collector_verdict_priority_remains_missing_failed_stale_fresh(app):
    config = app.extensions["solarbank_store"].settings
    assert collector_status(None, config, now=START)["status"] == "missing"
    state = {"last_success_at": START.isoformat(), "consecutive_failures": 1}
    assert collector_status(state, config, now=START)["status"] == "failed"
    state["consecutive_failures"] = 0
    assert collector_status(state, config, now=START)["status"] == "fresh"
    assert (
        collector_status(state, config, now=START + timedelta(minutes=16))["status"]
        == "stale"
    )


def test_dashboard_rejects_competing_calculations_without_blocking_health(
    app, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import solarbank_api.app as module

    entered, release = Event(), Event()

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"synthetic": True}

    monkeypatch.setattr(module, "dashboard", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            lambda: app.test_client().get(
                "/v1/dashboard", headers=HEADERS, query_string=query()
            )
        )
        try:
            assert entered.wait(2)
            assert app.test_client().get("/healthz").status_code == 200
            second = app.test_client().get(
                "/v1/dashboard", headers=HEADERS, query_string=query()
            )
            assert second.status_code == 503
            assert second.headers["Retry-After"] == "1"
        finally:
            release.set()
        assert first.result().status_code == 200
    # Capacity is returned after completing the calculation.
    assert (
        app.test_client()
        .get("/v1/dashboard", headers=HEADERS, query_string=query())
        .status_code
        == 200
    )
