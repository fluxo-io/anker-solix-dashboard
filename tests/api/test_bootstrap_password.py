"""The initial-account password policy never modifies existing accounts."""

from dataclasses import asdict, replace

import pytest
from fluxo_auth import UserStore
from solarbank_api import accounts, gunicorn_config, manage
from solarbank_api.app import create_app
from solarbank_api.config import Settings
from solarbank_api.store import Store
from werkzeug.security import generate_password_hash

FLAG = "INIT_ADMIN_REQUIRE_PASSWORD_CHANGE"
PASSWORD = "Synthetic-bootstrap-password-123!"


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


def account(store):
    with store.connection() as db:
        return asdict(UserStore(db).find("admin"))


def environment(monkeypatch, settings):
    for key, value in {
        "DATA_DIR": str(settings.db_path.parent),
        "API_TOKEN": settings.api_token,
        "SECRET_KEY": settings.secret_key,
        "COLLECTOR_API_TOKEN": settings.collector_api_token,
        "INIT_ADMIN_USERNAME": settings.admin_username,
        "INIT_ADMIN_PASSWORD": PASSWORD,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv("INIT_ADMIN_PASSWORD_HASH", raising=False)


@pytest.mark.parametrize(
    "value,expected", [(None, True), ("true", True), ("false", False)]
)
@pytest.mark.parametrize("hashed", [False, True])
def test_factory_plaintext_and_hash_policy(settings, value, expected, hashed):
    config = {
        "DATA_DIR": str(settings.db_path.parent),
        "API_TOKEN": settings.api_token,
        "SECRET_KEY": settings.secret_key,
        "INIT_ADMIN_USERNAME": "admin",
        "INIT_ADMIN_PASSWORD": PASSWORD,
    }
    if value is not None:
        config[FLAG] = value
    if hashed:
        config.update(
            INIT_ADMIN_PASSWORD="",
            INIT_ADMIN_PASSWORD_HASH=generate_password_hash(PASSWORD),
        )
    store = create_app(config).extensions["solarbank_store"]
    user = store.login("admin", PASSWORD, "synthetic-client")[0]
    assert user["must_change_password"] is expected
    if hashed:
        assert account(store)["password_hash"] == config["INIT_ADMIN_PASSWORD_HASH"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        (" TRUE ", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        (" FALSE ", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_environment_boolean_spellings(settings, monkeypatch, value, expected):
    environment(monkeypatch, settings)
    assert Settings.from_environment().admin_require_password_change is True
    monkeypatch.setenv(FLAG, value)
    assert Settings.from_environment().admin_require_password_change is expected


@pytest.mark.parametrize("value", ["", "maybe", "2"])
def test_invalid_environment_fails_before_creating_database(
    settings, monkeypatch, value
):
    environment(monkeypatch, settings)
    monkeypatch.setenv(FLAG, value)
    with pytest.raises(ValueError, match=FLAG):
        create_app()
    assert not settings.db_path.exists()


@pytest.mark.parametrize("value", ["", "maybe", None, 0, 1, [], {}])
def test_invalid_factory_and_dataclass_policy(settings, value):
    with pytest.raises(ValueError, match=FLAG):
        replace(settings, admin_require_password_change=value)
    with pytest.raises(ValueError, match=FLAG):
        create_app(
            {
                "DATA_DIR": str(settings.db_path.parent),
                "API_TOKEN": settings.api_token,
                "SECRET_KEY": settings.secret_key,
                "INIT_ADMIN_USERNAME": "admin",
                "INIT_ADMIN_PASSWORD": PASSWORD,
                FLAG: value,
            }
        )
    assert not settings.db_path.exists()


@pytest.mark.parametrize("required", [True, False])
def test_production_startup_uses_environment_once(settings, monkeypatch, required):
    environment(monkeypatch, settings)
    monkeypatch.setenv(FLAG, str(required).lower())
    gunicorn_config.on_starting(None)
    store = Store(Settings.from_environment())
    assert account(store)["must_change_password"] is required
    before = account(store)
    token = store.login("admin", PASSWORD, "synthetic-client")[0]["id"]
    before = account(store)
    monkeypatch.setenv(FLAG, str(not required).lower())
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "Changed-environment-password!")
    gunicorn_config.on_starting(None)
    create_app()
    assert account(store) == before
    assert store.session_user(token) is not None


@pytest.mark.parametrize("required", [True, False])
def test_existing_password_state_survives_migration_and_cli_reset_is_independent(
    settings, monkeypatch, required
):
    environment(monkeypatch, settings)
    store = Store(settings)
    store.initialize()
    store.reset_password("admin", "Changed-by-administrator-123!", must_change=required)
    token = store.login("admin", "Changed-by-administrator-123!", "synthetic-client")[
        0
    ]["id"]
    before = account(store)
    monkeypatch.setenv(FLAG, str(not required).lower())
    monkeypatch.setattr("sys.argv", ["manage", "migrate"])
    manage.main()
    Store(Settings.from_environment()).initialize()
    assert account(store) == before
    assert store.session_user(token) is not None
    monkeypatch.setattr(accounts, "getpass", lambda _prompt: "CLI-reset-password-123!")
    monkeypatch.setattr(
        "sys.argv", ["accounts", "admin"] + (["--initial"] if required else [])
    )
    accounts.main()
    assert account(store)["must_change_password"] is required
    assert store.session_user(token) is None
