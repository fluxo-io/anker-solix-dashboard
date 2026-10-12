from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from solarbank_api.app import create_app
from solarbank_api.config import Settings

TOKEN = "test-api-token-" + "t" * 32
PASSWORD = "test-password-12345"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
START = "2026-10-13T10:00:00+00:00"
END = "2026-10-13T11:00:00+00:00"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=tmp_path / "data" / "app.sqlite3",
        api_token=TOKEN,
        secret_key="test-secret-" + "s" * 32,
        admin_username="admin",
        admin_password=PASSWORD,
    )


@pytest.fixture
def app(settings):
    app = create_app(settings)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def row(**values):
    result = {
        "bucket_start": START,
        "collected_at": "2026-10-13T10:00:20+00:00",
        "site_id": "site-1",
        "device_sn": "device-1",
        "device_name": "Solarbank",
        "device_model": "A17C0",
        "data_valid": True,
        "pv_reported_total_w": 600,
        "battery_soc_pct": 50,
        "raw_data": {"device": {"power": "600"}},
    }
    result.update(values)
    return result


def save(client, *rows):
    return client.post("/v1/measurements", headers=HEADERS, json={"rows": rows})


def measurements(client, **query):
    return client.get(
        "/v1/measurements",
        headers=HEADERS,
        query_string={"start": START, "end": END, **query},
    )


def login(client, **values):
    return client.post(
        "/v1/auth/login",
        headers=HEADERS,
        json={
            "username": "admin",
            "password": PASSWORD,
            "client_key": "client-1",
            **values,
        },
    )


def test_health_is_public_and_data_requires_token(client):
    assert client.get("/healthz").json == {"status": "ok"}
    assert client.get("/v1/devices").status_code == 401
    assert (
        client.get("/v1/devices", headers={"Authorization": "Bearer bad"}).status_code
        == 401
    )
    assert client.get("/v1/devices", headers=HEADERS).json == {"rows": []}
    assert client.post("/v1/measurements", json={"rows": []}).status_code == 401


def test_roundtrip_utc_nulls_device_and_raw_snapshot(client):
    assert save(client, row(bucket_start="2026-10-13T12:00:00+02:00")).json == {
        "written": 1
    }
    result = measurements(client).json["rows"]
    assert len(result) == 1
    assert result[0]["bucket_start"] == "2026-10-13T10:00:00.000000+00:00"
    assert result[0]["data_valid"] is True
    assert result[0]["pv_1_w"] is None
    assert result[0]["pv_reported_total_w"] == 600
    assert "raw_data" not in result[0]
    assert client.get("/v1/devices", headers=HEADERS).json["rows"] == [
        {
            "site_id": "site-1",
            "device_sn": "device-1",
            "device_name": "Solarbank",
            "device_model": "A17C0",
        }
    ]
    snapshot = client.get(
        "/v1/snapshot",
        headers=HEADERS,
        query_string={
            "timestamp": START,
            "site_id": "site-1",
            "device_sn": "device-1",
        },
    ).json["snapshot"]
    assert snapshot["raw_data"] == row()["raw_data"]
    assert client.get(
        "/v1/snapshot",
        headers=HEADERS,
        query_string={
            "timestamp": START,
            "site_id": "missing",
            "device_sn": "device-1",
        },
    ).json == {"snapshot": None}


def test_valid_data_wins_and_older_equal_quality_cannot_replace_it(client):
    assert save(client, row(data_valid=False)).json == {"written": 1}
    assert save(client, row(collected_at="2026-10-13T10:00:10Z")).json == {"written": 1}
    assert save(client, row(data_valid=False, collected_at=END)).json == {"written": 0}
    assert save(client, row(collected_at="2026-10-13T10:00:05Z")).json == {"written": 0}
    result = measurements(client).json["rows"]
    assert len(result) == 1
    assert result[0]["collected_at"] == "2026-10-13T10:00:10.000000+00:00"


def test_filtering_end_exclusive_latest_includes_invalid(client):
    save(
        client,
        row(),
        row(site_id="site-2"),
        row(device_sn="device-2"),
        row(bucket_start="2026-10-13T10:05:00Z", data_valid=False),
        row(bucket_start=END),
    )
    assert len(measurements(client).json["rows"]) == 3
    assert len(measurements(client, valid_only="false").json["rows"]) == 4
    assert len(measurements(client, site_ids=json.dumps(["site-1"])).json["rows"]) == 2
    filtered = measurements(client, devices=json.dumps([["site-2", "device-1"]]))
    assert len(filtered.json["rows"]) == 1
    latest = client.get(
        "/v1/measurements/latest",
        headers=HEADERS,
        query_string={
            "site_ids": json.dumps(["site-1"]),
        },
    ).json["rows"]
    assert len(latest) == 2
    assert latest[0]["data_valid"] is True
    save(client, row(bucket_start="2026-10-13T11:05:00Z", data_valid=False))
    latest = client.get("/v1/measurements/latest", headers=HEADERS).json["rows"]
    assert latest[0]["data_valid"] is False


def test_limit_only_returns_whole_latest_buckets(client):
    save(
        client,
        row(),
        row(device_sn="device-2"),
        row(bucket_start="2026-10-13T10:05:00Z"),
        row(bucket_start="2026-10-13T10:05:00Z", device_sn="device-2"),
    )
    assert measurements(client, limit=1).json == {"rows": []}
    rows = measurements(client, limit=3).json["rows"]
    assert len(rows) == 2
    assert {item["filtered_row_count"] for item in rows} == {4}
    assert {item["bucket_start"] for item in rows} == {
        "2026-10-13T10:05:00.000000+00:00"
    }


@pytest.mark.parametrize(
    "query",
    [
        {"start": "2026-10-13T10:00:00"},
        {"end": START},
        {"end": "2027-10-13T10:00:00Z"},
        {"limit": 0},
        {"limit": 100001},
        {"limit": "invalid"},
        {"site_ids": "{}"},
        {"devices": '["device"]'},
        {"site_ids": "[null]"},
        {"valid_only": "yes"},
    ],
)
def test_bad_queries_are_rejected(client, query):
    assert measurements(client, **query).status_code == 400


def test_calendar_range_allows_dst_offset(client):
    end = datetime.fromisoformat(START) + timedelta(days=90, hours=2)
    assert measurements(client, end=end.isoformat()).status_code == 200
    assert (
        measurements(client, end=(end + timedelta(seconds=1)).isoformat()).status_code
        == 400
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"data_valid": 1},
        {"battery_soc_pct": True},
        {"battery_power_w": float("inf")},
        {"grid_power_w": 10**500},
        {"bucket_start": "2026-10-13"},
        {"device_sn": ""},
        {"raw_data": []},
        {"raw_data": {"x": float("nan")}},
    ],
)
def test_invalid_batch_does_not_save_any_rows(client, bad):
    assert (
        save(client, row(), row(**{"device_sn": "invalid", **bad})).status_code == 400
    )
    assert measurements(client).json == {"rows": []}


def test_collector_status_failure_invalid_and_valid_success(client):
    assert (
        client.post(
            "/v1/collector/attempt", headers=HEADERS, json={"attempted_at": START}
        ).status_code
        == 200
    )
    for _ in range(2):
        assert (
            client.post(
                "/v1/collector/failure",
                headers=HEADERS,
                json={"error": "source unavailable"},
            ).status_code
            == 200
        )
    save(client, row(data_valid=False))
    state = client.get("/v1/collector-state", headers=HEADERS).json["state"]
    assert state["consecutive_failures"] == 2
    assert state["last_success_at"] is None
    assert state["measurements_written"] == 1
    save(client, row())
    state = client.get("/v1/collector-state", headers=HEADERS).json["state"]
    assert state["consecutive_failures"] == 0
    assert state["last_success_at"] == "2026-10-13T10:00:20.000000+00:00"
    assert state["measurements_written"] == 2


def test_sql_failure_rolls_back_entire_batch(client, app):
    with app.extensions["solarbank_store"].connection() as connection:
        connection.execute("""
            CREATE TRIGGER reject_test_device BEFORE INSERT ON solarbank_measurements
            WHEN NEW.device_sn = 'reject'
            BEGIN SELECT RAISE(ABORT, 'test failure'); END
        """)
    assert save(client, row(), row(device_sn="reject")).status_code == 503
    assert measurements(client).json == {"rows": []}
    assert (
        client.get("/v1/collector-state", headers=HEADERS).json["state"][
            "measurements_written"
        ]
        == 0
    )


def test_restart_persists_data_password_and_rate_limit(client, settings):
    save(client, row())
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    restarted = create_app(replace(settings, admin_username="", admin_password=""))
    restarted_client = restarted.test_client()
    assert len(measurements(restarted_client).json["rows"]) == 1
    limited = login(restarted_client)
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 300
    identity = login(restarted_client, client_key="another-client").json
    assert identity["username"] == "admin"
    assert len(identity["id"]) == 64
    with sqlite3.connect(settings.db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        stored = connection.execute("SELECT password_hash FROM users").fetchone()[0]
        assert stored.startswith("scrypt:") and PASSWORD not in stored


def test_login_success_clears_failures(client):
    for _ in range(4):
        assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 200
    assert login(client, password="wrong").status_code == 401
    assert login(client, username="missing", client_key="another").status_code == 401


def test_concurrent_login_reservations_share_limit(app):
    store = app.extensions["solarbank_store"]
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(
            pool.map(lambda _: store._reserve_login("same-client"), range(10))
        )
    assert sum(attempt is not None for attempt, _ in results) == 5
    assert sum(retry is not None for _, retry in results) == 5


def test_new_database_requires_seed_and_existing_user_is_unchanged(settings):
    with pytest.raises(ValueError, match="INIT_ADMIN"):
        create_app(replace(settings, admin_username="", admin_password=""))
    create_app(settings)
    changed_seed = create_app(replace(settings, admin_password="another-password-123"))
    assert login(changed_seed.test_client()).status_code == 200
    assert (
        login(changed_seed.test_client(), password="another-password-123").status_code
        == 401
    )


def test_health_detects_missing_state(client, app):
    with app.extensions["solarbank_store"].connection() as connection:
        connection.execute("DELETE FROM collector_state")
    assert client.get("/healthz").status_code == 503


def test_settings_validate_and_factory_accepts_overrides(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "API_TOKEN": TOKEN,
            "SECRET_KEY": "s" * 32,
            "INIT_ADMIN_USERNAME": "admin",
            "INIT_ADMIN_PASSWORD": PASSWORD,
        }
    )
    assert app.test_client().get("/healthz").status_code == 200
    assert (
        app.extensions["solarbank_store"].settings.db_path == tmp_path / "app.sqlite3"
    )
    with pytest.raises(ValueError, match="API_TOKEN"):
        Settings(tmp_path / "x.db", "short", "s" * 32)
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(tmp_path / "x.db", TOKEN, "short")


def test_environment_uses_data_dir_and_fixed_database_filename(monkeypatch, tmp_path):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("SECRET_KEY", "s" * 32)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert Settings.from_environment().db_path == tmp_path / "app.sqlite3"
    monkeypatch.delenv("DATA_DIR")
    assert Settings.from_environment().db_path == Path("/data/app.sqlite3")
    monkeypatch.setenv("DATA_DIR", "relative")
    with pytest.raises(ValueError, match="DATA_DIR"):
        Settings.from_environment()


@pytest.mark.parametrize("whitespace", [" ", "\t", "\n", "\r", "\u00a0"])
def test_api_token_rejects_whitespace(settings, whitespace):
    with pytest.raises(ValueError, match="API_TOKEN"):
        replace(settings, api_token=TOKEN + whitespace)


@pytest.mark.parametrize("method", ["scrypt", "pbkdf2:sha256:600000"])
def test_existing_password_hash_bootstraps_fresh_database(settings, method):
    password_hash = generate_password_hash(PASSWORD, method=method)
    app = create_app(
        replace(settings, admin_password="", admin_password_hash=password_hash)
    )
    assert login(app.test_client()).status_code == 200
    with sqlite3.connect(settings.db_path) as connection:
        assert (
            connection.execute(
                "SELECT password_hash FROM users WHERE username = 'admin'"
            ).fetchone()[0]
            == password_hash
        )


def test_plaintext_seed_takes_precedence_over_optional_hash(settings):
    app = create_app(replace(settings, admin_password_hash="ignored-invalid-hash"))
    assert login(app.test_client()).status_code == 200


@pytest.mark.parametrize(
    "password_hash",
    [
        "invalid",
        "scrypt:16:8:1$salt12345$" + "a" * 128,
        "pbkdf2:sha256:100$salt12345$" + "a" * 64,
        "scrypt:32768:8:1$salt12345$not-a-digest",
    ],
)
def test_invalid_seed_hash_fails_startup(settings, password_hash):
    with pytest.raises(ValueError, match="INIT_ADMIN_PASSWORD_HASH"):
        replace(settings, admin_password="", admin_password_hash=password_hash)


def test_login_returns_opaque_identity_and_session_validates_persisted_user(client):
    identity = login(client).json
    assert identity["username"] == "admin"
    assert len(identity["id"]) == 64
    assert identity["id"] != "admin"
    assert (
        client.post(
            "/v1/auth/session", headers=HEADERS, json={"id": identity["id"]}
        ).json
        == identity
    )
    assert (
        client.post("/v1/auth/session", json={"id": identity["id"]}).status_code == 401
    )
    assert (
        client.post(
            "/v1/auth/session", headers=HEADERS, json={"id": "0" * 64}
        ).status_code
        == 401
    )


def test_password_change_invalidates_existing_identity(client, app):
    old_identity = login(client).json
    new_password = "new-password-12345"
    with app.extensions["solarbank_store"].connection() as connection:
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(new_password), "admin"),
        )
    assert (
        client.post(
            "/v1/auth/session", headers=HEADERS, json={"id": old_identity["id"]}
        ).status_code
        == 401
    )
    new_identity = login(client, password=new_password).json
    assert new_identity["id"] != old_identity["id"]
    assert (
        client.post(
            "/v1/auth/session", headers=HEADERS, json={"id": new_identity["id"]}
        ).json
        == new_identity
    )


def test_deleted_user_invalidates_existing_identity(client, app):
    identity = login(client).json
    with app.extensions["solarbank_store"].connection() as connection:
        connection.execute("DELETE FROM users WHERE username = ?", ("admin",))
    assert (
        client.post(
            "/v1/auth/session", headers=HEADERS, json={"id": identity["id"]}
        ).status_code
        == 401
    )


def test_session_survives_restart_but_not_database_reinitialization(client, settings):
    identity = login(client).json
    restarted = create_app(settings).test_client()
    assert (
        restarted.post(
            "/v1/auth/session", headers=HEADERS, json={"id": identity["id"]}
        ).json
        == identity
    )
    with sqlite3.connect(settings.db_path) as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM users WHERE username = 'admin'"
        ).fetchone()[0]
    settings.db_path.unlink()
    reinitialized = create_app(
        replace(settings, admin_password="", admin_password_hash=password_hash)
    ).test_client()
    assert (
        reinitialized.post(
            "/v1/auth/session", headers=HEADERS, json={"id": identity["id"]}
        ).status_code
        == 401
    )


def test_missing_database_is_not_recreated_by_requests(client, settings):
    settings.db_path.unlink()
    assert client.get("/healthz").status_code == 503
    assert not settings.db_path.exists()
    assert client.get("/v1/devices", headers=HEADERS).status_code == 503
    assert not settings.db_path.exists()
    assert save(client, row()).status_code == 503
    assert not settings.db_path.exists()
