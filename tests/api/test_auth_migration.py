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
    )


def legacy(settings, usernames=("Admin",)):
    password_hash = generate_password_hash(PASSWORD, method="pbkdf2:sha256:600000")
    with sqlite3.connect(settings.db_path) as db:
        db.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
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


def test_migration_preserves_hash_and_data_without_reimport(settings):
    password_hash = legacy(settings)
    store = Store(settings)
    store.initialize()
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
        Store(settings).initialize()
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
    store.initialize()
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
