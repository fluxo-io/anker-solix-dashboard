"""Synthetic usage/audit contract, migration and transaction regressions."""

import shutil
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fluxo_auth import UserStore
from solarbank_api.analytics import build
from solarbank_api.app import create_app
from solarbank_api.config import Settings
from solarbank_api.store import Store

PASSWORD = "Synthetic-password-123"


@pytest.fixture
def store(tmp_path):
    value = Store(
        Settings(
            db_path=tmp_path / "app.sqlite3",
            api_token="w" * 40,
            collector_api_token="c" * 40,
            secret_key="s" * 40,
            admin_username="admin",
            admin_password=PASSWORD,
            admin_require_password_change=False,
        )
    )
    value.initialize()
    return value


def token(store, username="admin"):
    return store.login(username, PASSWORD, username)[0]["id"]


def records(store):
    with store.connection() as connection:
        return [
            dict(row) for row in connection.execute("SELECT * FROM activity_events")
        ]


def test_exclusion_applies_to_all_statistics_and_existing_events_but_not_audit(store):
    admin = token(store)
    for name in ("Alice", "Straße", "system"):
        store.manage_accounts(
            admin,
            "create",
            {"username": name, "password": PASSWORD, "must_change_password": False},
        )
        actor = token(store, name)
        store.record_usage(actor, "page_view", "/")
        store.record_usage(actor, "access_denied", "/users")
    store.login("unverified", "bad", "attempt")
    before = records(store)
    excluded = Store(
        replace(store.settings, analytics_excluded_users=" ADMIN, , strASSE , ")
    )
    report = excluded.activity_report(admin, {"range": "all"}, usage=True)
    assert report["users"] == ["Alice", "system"]
    assert report["kpi"]["users"] == 2
    assert report["kpi"]["events"] == 8  # two people x 3, setup + failed login
    assert report["kpi"]["events"] == sum(sum(s["counts"]) for s in report["series"])
    assert report["kpi"]["events"] == sum(row["total"] for row in report["table"])
    assert {row["user"] for row in report["table"]} == {"Alice", "system", "-"}
    assert not any(
        s["user"].casefold() in {"admin", "strasse"} for s in report["series"]
    )
    assert records(store) == before
    audit = excluded.activity_report(admin, {"range": "all", "user": "Straße"})
    assert audit["total"] == 3
    assert "Straße" in audit["users"]
    assert any(
        row["event"] == "login_failed" and not row["actor_verified"] for row in before
    )
    filtered = excluded.activity_report(
        admin, {"range": "all", "user": "Alice", "event": "page_view"}, usage=True
    )
    assert filtered["kpi"]["events"] == 1
    assert filtered["table"][0]["page_view"] == 1
    assert (
        "CET (UTC+0100)" in filtered["table"][0]["last_seen"]
        or "CEST (UTC+0200)" in filtered["table"][0]["last_seen"]
    )


def test_ranges_partial_hour_dst_and_anonymous_users():
    now = datetime(2026, 10, 25, 3, 17, tzinfo=UTC)

    def entry(moment, user="alice", verified=True, event="page_view"):
        return {
            "ts": moment.isoformat(),
            "user": user,
            "event": event,
            "actor_verified": verified,
        }

    entries = [
        entry(now - timedelta(hours=23, minutes=59)),
        entry(now - timedelta(days=2)),
        entry(now - timedelta(hours=1), "failed", False, "login_failed"),
        entry(now + timedelta(seconds=1)),
    ]
    report = build(entries, days="1", now=now)
    assert report["kpi"]["events"] == 2
    assert report["kpi"]["users"] == 1
    assert report["users"] == ["alice"]
    assert "25.10. 02:00 CEST" in report["labels"]
    assert "25.10. 02:00 CET" in report["labels"]
    assert build(entries, days="7", now=now)["kpi"]["events"] == 3
    assert (
        build([entry(datetime(1200, 1, 1, tzinfo=UTC))], days="all", now=now)["kpi"][
            "events"
        ]
        == 1
    )
    assert len(build(entries, days="all", now=now)["labels"]) <= 400


def test_activity_filters_pagination_timezone_and_config_are_api_owned(store):
    admin = token(store)
    with store.connection() as connection:
        for _ in range(60):
            store._record(connection, "page_view", "admin", "/users")
        store._record(connection, "page_view", "alice", "/password")
        connection.execute(
            "UPDATE activity_events SET ts=? WHERE user='alice'",
            ((datetime.now(UTC) - timedelta(days=10)).isoformat(),),
        )
    report = store.activity_report(
        admin,
        {"range": "7", "user": "admin", "event": "page_view", "q": "USERS", "page": 2},
    )
    assert report["total"] == 60 and len(report["rows"]) == 10 and report["pages"] == 2
    assert all(row["target"] == "/users" for row in report["rows"])
    assert store.activity_report(admin, {"range": "7", "user": "alice"})["total"] == 0
    assert store.activity_report(admin, {"range": "30", "user": "alice"})["total"] == 1
    api = create_app(
        replace(
            store.settings, analytics_excluded_users="admin", analytics_timezone="UTC"
        )
    )
    response = api.test_client().post(
        "/v1/analytics",
        headers={"Authorization": "Bearer " + store.settings.api_token},
        json={
            "id": admin,
            "range": "all",
            "excluded_users": [],
            "timezone": "Europe/Berlin",
        },
    )
    assert response.status_code == 200
    assert response.json["timezone"] == "UTC"
    assert "admin" not in response.json["users"]


def test_new_endpoints_require_web_service_and_current_authorized_session(store):
    admin = token(store)
    store.manage_accounts(
        admin,
        "create",
        {"username": "reader", "password": PASSWORD, "must_change_password": False},
    )
    reader = token(store, "reader")
    client = create_app(store.settings).test_client()
    headers = {"Authorization": "Bearer " + store.settings.api_token}
    collector = {"Authorization": "Bearer " + store.settings.collector_api_token}
    for path in ("/v1/analytics", "/v1/activity", "/v1/activity/events"):
        payload = {"id": admin, "event": "page_view", "target": "/"}
        assert client.post(path, json=payload).status_code == 401
        assert client.post(path, headers=collector, json=payload).status_code == 403
        assert client.get(path, headers=headers).status_code == 403
        assert client.post(
            path, headers=headers, json={**payload, "id": "invalid"}
        ).status_code in {401, 403}
        assert (
            client.post(
                path, headers=headers, json={**payload, "target": []}
            ).status_code
            == 400
        )
    for path in ("/v1/analytics", "/v1/activity"):
        assert (
            client.post(path, headers=headers, json={"id": reader}).status_code == 403
        )
        assert (
            client.post(
                path, headers=headers, json={"id": admin, "user": []}
            ).status_code
            == 400
        )
    assert (
        client.post(
            "/v1/activity/events",
            headers=headers,
            json={"id": reader, "user": "admin", "event": "page_view", "target": "/"},
        ).status_code
        == 200
    )
    assert records(store)[-1]["user"] == "reader"
    assert (
        client.post(
            "/v1/activity/events",
            headers=headers,
            json={"id": reader, "event": "user_create", "target": "/"},
        ).status_code
        == 400
    )
    store.logout(admin)
    assert (
        client.post("/v1/analytics", headers=headers, json={"id": admin}).status_code
        == 403
    )


def test_auth_and_account_changes_rollback_if_mandatory_audit_fails(store):
    admin = token(store)
    with store.connection() as connection:
        before = UserStore(connection).find("admin")
        session_count = connection.execute(
            "SELECT count(*) FROM auth_sessions"
        ).fetchone()[0]
        connection.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON activity_events BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END"
        )
    operations = [
        lambda: store.login("admin", PASSWORD, "new-client"),
        lambda: store.login("admin", "wrong", "bad-client"),
        lambda: store.logout(admin),
        lambda: store.manage_accounts(
            admin, "create", {"username": "new-user", "password": PASSWORD}
        ),
        lambda: store.manage_accounts(
            admin,
            "reset",
            {"username": "admin", "password": "Replacement-password-123"},
        ),
        lambda: store.change_password(admin, PASSWORD, "Replacement-password-123"),
        lambda: store.reset_password("admin", "Replacement-password-123"),
    ]
    for operation in operations:
        with pytest.raises(sqlite3.Error):
            operation()
        assert store.session_user(admin)
        with store.connection() as connection:
            after = UserStore(connection).find("admin")
            assert after.password_hash == before.password_hash
            assert after.last_login_at == before.last_login_at
            assert UserStore(connection).find("new-user") is None
            assert (
                connection.execute("SELECT count(*) FROM auth_sessions").fetchone()[0]
                == session_count
            )
    client = create_app(store.settings).test_client()
    response = client.post(
        "/v1/auth/login",
        headers={"Authorization": "Bearer " + store.settings.api_token},
        json={"username": "admin", "password": PASSWORD, "client_key": "client"},
    )
    assert response.status_code == 503


def test_optional_record_is_bounded_and_reports_storage_failure(store):
    admin = token(store)
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.record_usage(admin, "page_view", "/")
        assert time.monotonic() - started < 1
    with store.connection() as connection:
        connection.execute("DROP TABLE activity_events")
    with pytest.raises(sqlite3.Error):
        store.activity_report(admin, {}, usage=True)


def test_v1_migration_is_explicit_idempotent_and_preserves_sessions_after_copy(store):
    admin = token(store)
    with store.connection() as connection:
        account = UserStore(connection).find("admin")
        state = dict(connection.execute("SELECT * FROM collector_state").fetchone())
        connection.execute("DROP TABLE activity_events")
        connection.execute("DELETE FROM schema_migrations WHERE version=2")
    before = store.settings.db_path.read_bytes()
    with pytest.raises(RuntimeError, match="migration erforderlich"):
        Store(store.settings).initialize()
    assert store.settings.db_path.read_bytes() == before
    store.migrate()
    store.migrate()
    restarted = Store(store.settings)
    restarted.initialize()
    assert restarted.ready() and restarted.session_user(admin)
    with restarted.connection() as connection:
        assert UserStore(connection).find("admin").id == account.id
        assert (
            UserStore(connection).find("admin").password_hash == account.password_hash
        )
        assert (
            dict(connection.execute("SELECT * FROM collector_state").fetchone())
            == state
        )
        assert (
            connection.execute("SELECT count(*) FROM activity_events").fetchone()[0]
            == 0
        )
    restarted.record_usage(admin, "page_view", "/")
    copied = store.settings.db_path.with_name("restored.sqlite3")
    shutil.copy2(store.settings.db_path, copied)
    restored = Store(replace(store.settings, db_path=copied))
    restored.initialize()
    assert restored.session_user(admin)
    assert records(restored) == records(restarted)
