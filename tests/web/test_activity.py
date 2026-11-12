"""Usage pages through a real API, using only temporary synthetic data."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_login_roundtrip import PASSWORD, csrf, login, web
from test_login_roundtrip import api as api_fixture


@pytest.fixture
def api(tmp_path):
    yield from api_fixture.__wrapped__(tmp_path)


def test_admin_views_filters_navigation_and_only_document_visits_are_recorded(api):
    app = web(api)
    client = app.test_client()
    assert client.get("/admin/analytics").status_code == 302
    assert login(client).status_code == 302
    response = client.get("/admin/analytics?range=all&event=login_success&user=admin")
    assert response.status_code == 200
    assert b"Gesamt-Ereignisse" in response.data
    assert b"CET (UTC+0100)" in response.data or b"CEST (UTC+0200)" in response.data
    layout = client.get("/_dash-layout")
    assert "/admin/analytics" in json.dumps(layout.json)
    assert "/admin/activity" in json.dumps(layout.json)
    client.get("/healthz")
    with sqlite3.connect(api.settings.db_path) as connection:
        pages = connection.execute(
            "SELECT target FROM activity_events WHERE event='page_view'"
        ).fetchall()
        assert pages == [("/admin/analytics",)]
    response = client.get(
        "/admin/activity?range=all&event=page_view&user=admin&q=analytics"
    )
    assert response.status_code == 200
    assert b"1 Ereignisse" in response.data
    assert b"/admin/analytics" in response.data
    assert client.get("/admin/activity?page=invalid").status_code == 400


def test_excluded_admin_still_visible_in_activity_after_api_restart(api):
    client = web(api).test_client()
    login(client)
    client.get("/admin/activity")
    api.stop()
    api.settings = replace(api.settings, analytics_excluded_users=" ADMIN, ")
    api.start()
    response = client.get("/admin/analytics?range=all&user=admin")
    assert response.status_code == 200
    assert b"Keine Ereignisse" in response.data
    assert b"<option selected>admin</option>" not in response.data
    response = client.get("/admin/activity?range=all&user=admin&event=login_success")
    assert response.status_code == 200 and b"1 Ereignisse" in response.data


def test_readers_cannot_view_usage_and_denial_uses_verified_identity(api):
    store = api.app.extensions["solarbank_store"]
    admin = store.login("admin", PASSWORD, "setup")[0]["id"]
    store.manage_accounts(
        admin,
        "create",
        {"username": "reader", "password": PASSWORD, "must_change_password": False},
    )
    client = web(api).test_client()
    client.post(
        "/login",
        data={
            "csrf_token": csrf(client.get("/login")),
            "username": "reader",
            "password": PASSWORD,
        },
    )
    for path in ("/admin/analytics", "/admin/activity", "/users"):
        assert client.get(path).status_code == 403
    layout = client.get("/_dash-layout")
    assert "/admin/analytics" not in json.dumps(layout.json)
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT user, actor_verified FROM activity_events WHERE event='access_denied'"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("reader", 1)] * 3


def test_optional_telemetry_failure_does_not_hide_report_failure(api, monkeypatch):
    store = api.app.extensions["solarbank_store"]
    client = web(api).test_client()
    login(client)

    def failed(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic storage error")

    monkeypatch.setattr(store, "record_usage", failed)
    assert client.get("/admin/analytics").status_code == 200
    monkeypatch.setattr(store, "activity_report", failed)
    response = client.get("/admin/analytics")
    assert response.status_code == 503
    assert b"Daten nicht verf" in response.data
    assert b"Gesamt-Ereignisse" not in response.data


@pytest.mark.parametrize("bucket,peak", [("hour", 2), ("day", 3), ("week", 4)])
def test_usage_grouping_and_peak_follow_the_selected_time_bucket(api, bucket, peak):
    store = api.app.extensions["solarbank_store"]
    base = (datetime.now(UTC) - timedelta(days=7)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    base -= timedelta(days=base.weekday())
    with store.connection() as connection:
        for offset in (
            timedelta(),
            timedelta(minutes=10),
            timedelta(hours=2),
            timedelta(days=1),
        ):
            connection.execute(
                "INSERT INTO activity_events (ts, user, event, target, actor_verified) VALUES (?, 'Alice', 'page_view', '/', 1)",
                ((base + offset).isoformat(),),
            )
    client = web(api).test_client()
    login(client)
    response = client.get(
        f"/admin/analytics?range=all&bucket={bucket}&user=Alice&event=page_view"
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'<option value="{bucket}" selected>' in body
    assert f"<span>Spitzenwert</span><strong>{peak}</strong><small>" in body
    assert "<span>Gesamt-Ereignisse</span><strong>4</strong>" in body
    if bucket == "hour":
        # A bounded chart announces its actual grouping while retaining the choice.
        response = client.get(
            "/admin/analytics?range=90&bucket=hour&user=Alice&event=page_view"
        )
        assert b'<option value="hour" selected>' in response.data
        assert b"Zusammengefasst: Tag." in response.data
        assert b"<span>Spitzenwert</span><strong>3</strong>" in response.data
