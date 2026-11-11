"""Exercise shared authentication against the real legacy schema and copied files."""

import hashlib
import hmac
import shutil
import sqlite3
import time
from dataclasses import replace

import pytest
from fluxo_auth import UserStore
from solarbank_api.app import create_app
from solarbank_api.config import Settings
from solarbank_api.store import Store
from werkzeug.security import generate_password_hash

PASSWORD = "Legacy-password-123"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=tmp_path / "app.sqlite3",
        api_token="t" * 40,
        secret_key="s" * 40,
        collector_api_token="c" * 40,
        admin_username="admin",
        admin_password=PASSWORD,
        admin_require_password_change=False,
    )


def legacy(settings, usernames=("Admin",)):
    password_hash = generate_password_hash(PASSWORD, method="pbkdf2:sha256:600000")
    with sqlite3.connect(settings.db_path) as db:
        db.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL)"
        )
        Store._create_schema(db)
        db.execute(
            "INSERT INTO schema_migrations VALUES (1, '2020-01-01T00:00:00+00:00')"
        )
        for username in usernames:
            db.execute(
                "INSERT INTO users VALUES (?, ?, ?)",
                (username, password_hash, "old-nonce"),
            )
    return password_hash


def test_start_refuses_legacy_schema_without_changing_database(settings):
    legacy(settings)
    before = settings.db_path.read_bytes()
    with pytest.raises(RuntimeError, match="migration erforderlich"):
        Store(settings).initialize()
    assert before == settings.db_path.read_bytes()
    Store(settings).migrate()
    Store(settings).initialize()
    assert Store(settings).ready()


def test_start_does_not_replace_existing_empty_file(settings):
    settings.db_path.touch()
    with pytest.raises(RuntimeError, match="migration erforderlich"):
        Store(settings).initialize()
    assert settings.db_path.read_bytes() == b""


def test_migration_preserves_hash_and_data_without_reimport(settings):
    password_hash = legacy(settings)
    store = Store(settings)
    store.migrate()
    identity, _ = store.login(" ADMIN ", PASSWORD, "client")
    assert identity["username"] == "Admin"
    with store.connection() as db:
        account = UserStore(db).find("admin")
        assert account.password_hash == password_hash
        assert account.metadata["display_name"] == "Admin"
        identifier = account.id
        assert db.execute("SELECT count(*) FROM collector_state").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone()
            is None
        )
    Store(
        replace(
            settings,
            admin_username="replacement",
            admin_password="Replacement-password",
        )
    ).initialize()
    with store.connection() as db:
        assert UserStore(db).count() == 1
        assert UserStore(db).find("admin").id == identifier
    copied = settings.db_path.with_name("restored.sqlite3")
    shutil.copy2(settings.db_path, copied)
    restored = Store(replace(settings, db_path=copied))
    assert restored.session_user(identity["id"]) == identity
    assert restored.ready()


def test_colliding_logins_abort_atomic_migration(settings):
    legacy(settings, ("Admin", "admin"))
    with pytest.raises(ValueError):
        Store(settings).migrate()
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 2
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='auth_users'").fetchone()
            is None
        )


def test_existing_attempts_preserve_throttle(settings):
    legacy(settings)
    key = hmac.new(settings.secret_key.encode(), b"client", hashlib.sha256).hexdigest()
    with sqlite3.connect(settings.db_path) as db:
        for index in range(5):
            db.execute(
                "INSERT INTO login_attempts VALUES (?, ?, ?, 0)",
                (str(index), key, time.time()),
            )
    store = Store(settings)
    store.migrate()
    user, retry = store.login("admin", PASSWORD, "client")
    assert user is None
    assert retry > 0


def test_server_logout_is_selective_and_rejects_replay(settings):
    app = create_app(settings)
    client = app.test_client()
    headers = {"Authorization": "Bearer " + settings.api_token}
    payload = {"username": "admin", "password": PASSWORD, "client_key": "client"}
    one = client.post("/v1/auth/login", headers=headers, json=payload).json
    two = client.post("/v1/auth/login", headers=headers, json=payload).json
    assert one["id"] != two["id"]
    assert client.post(
        "/v1/auth/logout", headers=headers, json={"id": one["id"]}
    ).json == {"ok": True}
    assert (
        client.post(
            "/v1/auth/session", headers=headers, json={"id": one["id"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/auth/session", headers=headers, json={"id": two["id"]}
        ).status_code
        == 200
    )
    collector = {"Authorization": "Bearer " + settings.collector_api_token}
    assert (
        client.post(
            "/v1/auth/logout", headers=collector, json={"id": two["id"]}
        ).status_code
        == 403
    )


def test_password_reset_disable_and_absolute_expiry(settings):
    store = Store(replace(settings, session_lifetime_seconds=10))
    store.initialize()
    first = store.login("admin", PASSWORD, "client")[0]
    store.reset_password("admin", "Changed-password-123")
    assert store.session_user(first["id"]) is None
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        accounts = UserStore(db)
        user = accounts.find("admin")
        token = store._auth(db).issue(user.id, now=100)
        assert store._auth(db).validate(token, now=109) is not None
        assert store._auth(db).validate(token, now=110) is None
        token = store._auth(db).issue(user.id)
        accounts.update(user.id, active=False)
        accounts.update(user.id, active=True)
        assert store._auth(db).validate(token) is None


def test_shorter_policy_permanently_caps_existing_session(settings):
    store = Store(settings)
    store.initialize()
    token = store.login("admin", PASSWORD, "client")[0]["id"]
    with store.connection() as db:
        issued = store._auth(db).session_info(token)["issued_at"]
    Store(replace(settings, session_lifetime_seconds=3600)).initialize()
    longer = Store(replace(settings, session_lifetime_seconds=86400))
    longer.initialize()
    with longer.connection() as db:
        assert longer._auth(db).validate(token, now=issued + 3599) is not None
        assert longer._auth(db).validate(token, now=issued + 3600) is None


def test_account_modes_last_login_and_permission_boundaries(settings):
    store = Store(settings)
    store.initialize()
    admin = store.login("admin", PASSWORD, "admin-client")[0]
    for name, required in [("fixed", False), ("initial", True)]:
        created = store.manage_accounts(
            admin["id"],
            "create",
            {
                "username": name,
                "password": PASSWORD,
                "must_change_password": required,
            },
        )["user"]
        assert created["must_change_password"] is required
        assert created["last_login_at"] is None
        assert created["can_manage_users"] is False
    fixed = store.login("fixed", PASSWORD, "fixed-client")[0]
    stamp = fixed["last_login_at"]
    assert stamp and stamp.endswith("+00:00")
    store.session_user(fixed["id"])
    store.login("fixed", "wrong", "fixed-client")
    assert store.session_user(fixed["id"])["last_login_at"] == stamp
    for token in ["invalid", fixed["id"]]:
        with pytest.raises(PermissionError):
            store.manage_accounts(token, "list", {})
    required = store.login("initial", PASSWORD, "initial-client")[0]
    with pytest.raises(ValueError):
        store.change_password(required["id"], PASSWORD, PASSWORD)
    changed = store.change_password(required["id"], PASSWORD, "Changed-password-456")
    assert changed["must_change_password"] is False
    assert changed["last_login_at"] == required["last_login_at"]
    assert store.session_user(required["id"]) is None
    reset = store.manage_accounts(
        admin["id"],
        "reset",
        {
            "username": "fixed",
            "password": "Reset-password-123",
            "must_change_password": True,
        },
    )["user"]
    assert reset["must_change_password"] is True
    assert reset["last_login_at"] == stamp
    assert store.session_user(fixed["id"]) is None
    store.reset_password("fixed", PASSWORD, must_change=False)
    assert (
        store.login("fixed", PASSWORD, "fixed-client")[0]["must_change_password"]
        is False
    )
    reopened = Store(settings)
    reopened.initialize()
    assert reopened.session_user(admin["id"])["can_manage_users"] is True
    users = reopened.manage_accounts(admin["id"], "list", {})["users"]
    assert all(
        user["can_manage_users"] is False
        for user in users
        if user["username"] != "admin"
    )


def test_new_account_endpoints_require_web_service_and_authorized_actor(settings):
    app = create_app(settings)
    client = app.test_client()
    store = app.extensions["solarbank_store"]
    admin = store.login("admin", PASSWORD, "client")[0]
    for route in ["/v1/auth/accounts", "/v1/auth/password"]:
        assert client.post(route, json={"id": admin["id"]}).status_code == 401
        assert (
            client.post(
                route,
                headers={"Authorization": "Bearer " + settings.collector_api_token},
                json={"id": admin["id"]},
            ).status_code
            == 403
        )
    headers = {"Authorization": "Bearer " + settings.api_token}
    assert (
        client.post(
            "/v1/auth/accounts", headers=headers, json={"id": "bad"}
        ).status_code
        == 403
    )
    result = client.post("/v1/auth/accounts", headers=headers, json={"id": admin["id"]})
    assert result.status_code == 200
    assert admin["id"] not in result.text
    assert "password_hash" not in result.text
    assert (
        client.post(
            "/v1/auth/accounts",
            headers=headers,
            json={
                "id": admin["id"],
                "operation": "create",
                "username": "bad",
                "password": PASSWORD,
                "must_change_password": "false",
            },
        ).status_code
        == 400
    )


def test_management_migration_preserves_authorization_and_can_select_owner_later(
    settings,
):
    store = Store(settings)
    store.initialize()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        accounts = UserStore(db)
        admin = accounts.find("admin")
        accounts.update(
            admin.id,
            authorization={"permissions": ["custom.read"], "tenant": "preserved"},
        )
        db.execute(
            "DELETE FROM auth_meta WHERE key='migration:anker-account-management-v1'"
        )
    store.migrate()
    with store.connection() as db:
        authorization = UserStore(db).find("admin").authorization
        assert authorization == {
            "permissions": ["custom.read", "users.manage"],
            "tenant": "preserved",
        }
    token = store.login("admin", PASSWORD, "admin-client")[0]["id"]
    store.manage_accounts(
        token,
        "create",
        {"username": "reader", "password": PASSWORD, "must_change_password": False},
    )
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        accounts = UserStore(db)
        accounts.update(accounts.find("admin").id, authorization={})
        db.execute(
            "DELETE FROM auth_meta WHERE key='migration:anker-account-management-v1'"
        )
    Store(replace(settings, admin_username="missing")).migrate()
    with store.connection() as db:
        accounts = UserStore(db)
        assert not accounts.migration_applied("anker-account-management-v1")
        assert all(not account.authorization for account in accounts.all())
    store.migrate()
    with store.connection() as db:
        assert UserStore(db).find("admin").authorization["permissions"] == [
            "users.manage"
        ]
        assert UserStore(db).find("reader").authorization == {}
