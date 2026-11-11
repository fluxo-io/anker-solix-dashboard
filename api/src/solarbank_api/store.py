from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from fluxo_auth import AuthService, UserStore, hash_password, verify_password

from .config import Settings
from .validation import MEASUREMENT_COLUMNS, NUMERIC_COLUMNS

SCHEMA_VERSION = 1
_KEY_COLUMNS = {"bucket_start", "site_id", "device_sn"}
_WRITE_COLUMNS = (*MEASUREMENT_COLUMNS, "raw_data")
UPSERT_MEASUREMENT = f"""
    INSERT INTO solarbank_measurements ({", ".join(_WRITE_COLUMNS)})
    VALUES ({", ".join("?" for _ in _WRITE_COLUMNS)})
    ON CONFLICT (bucket_start, site_id, device_sn) DO UPDATE SET
        {
    ", ".join(
        f"{name} = excluded.{name}"
        for name in _WRITE_COLUMNS
        if name not in _KEY_COLUMNS
    )
}
    WHERE (excluded.data_valid = 1 AND solarbank_measurements.data_valid = 0)
       OR (excluded.data_valid = solarbank_measurements.data_valid
           AND excluded.collected_at >= solarbank_measurements.collected_at)
"""


def _serialize(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    if "data_valid" in result:
        result["data_valid"] = bool(result["data_valid"])
    if "raw_data" in result:
        result["raw_data"] = json.loads(result["raw_data"])
    return result


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @contextmanager
    def connection(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        mode = "rwc" if create else "rw"
        connection = sqlite3.connect(
            f"{self.settings.db_path.as_uri()}?mode={mode}", uri=True, timeout=10
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(create=True) as connection:
            # Keep the portable rollback journal on the bundled SQLite runtime.
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            if version is not None and version != SCHEMA_VERSION:
                raise RuntimeError("Nicht unterstützte Datenbankversion")
            if version is None:
                self._create_schema(connection)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
                )
            accounts = UserStore(connection)
            accounts.ensure_schema()
            if not accounts.migration_applied("anker-users-v1"):
                rows = connection.execute("SELECT * FROM users").fetchall()
                accounts.import_legacy(
                    "anker-users-v1",
                    [
                        dict(
                            id="usr_" + secrets.token_hex(12),
                            username=row["username"],
                            password_hash=row["password_hash"],
                            active=True,
                            authorization={},
                            metadata={"display_name": row["username"]},
                        )
                        for row in rows
                    ],
                )
                attempts = [
                    dict(row)
                    for row in connection.execute("SELECT * FROM login_attempts")
                ]
                accounts.import_attempts("anker-login-v1", attempts)
                connection.execute("DROP TABLE users")
                connection.execute("DROP TABLE login_attempts")
            if not accounts.count():
                if not self.settings.admin_username or not (
                    self.settings.admin_password or self.settings.admin_password_hash
                ):
                    raise ValueError(
                        "INIT_ADMIN_USERNAME und INIT_ADMIN_PASSWORD "
                        "oder INIT_ADMIN_PASSWORD_HASH fehlen"
                    )
                accounts.create(
                    id="usr_" + secrets.token_hex(12),
                    username=self.settings.admin_username,
                    password_hash=(
                        hash_password(self.settings.admin_password)
                        if self.settings.admin_password
                        else self.settings.admin_password_hash
                    ),
                    authorization={"permissions": ["users.manage"]},
                    metadata={"display_name": self.settings.admin_username},
                )
            if not accounts.migration_applied("anker-account-management-v1"):
                existing = accounts.all()
                owners = [
                    account
                    for account in existing
                    if account.username.casefold()
                    == self.settings.admin_username.casefold()
                ]
                # With no configured name, only the sole legacy account can be
                # identified unambiguously. Multiple users require an explicit name.
                if not owners and len(existing) == 1 and not existing[0].authorization:
                    owners = existing
                for owner in owners:
                    permissions = set(owner.authorization.get("permissions", []))
                    permissions.add("users.manage")
                    accounts.update(
                        owner.id,
                        authorization={
                            **owner.authorization,
                            "permissions": sorted(permissions),
                        },
                    )
                if owners or any(
                    "users.manage" in account.authorization.get("permissions", [])
                    for account in existing
                ):
                    accounts.import_legacy("anker-account-management-v1", [])
            self._auth(connection).constrain_lifetime()
        self.settings.db_path.chmod(0o600)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(f"""
            CREATE TABLE solarbank_measurements (
                id INTEGER PRIMARY KEY,
                bucket_start TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                source_updated_at TEXT,
                site_id TEXT NOT NULL,
                device_sn TEXT NOT NULL,
                device_name TEXT,
                device_model TEXT,
                data_valid INTEGER NOT NULL CHECK (data_valid IN (0, 1)),
                {", ".join(f"{name} REAL" for name in NUMERIC_COLUMNS)},
                raw_data TEXT NOT NULL,
                UNIQUE (bucket_start, site_id, device_sn)
            )
        """)
        connection.execute("""
            CREATE INDEX measurements_device_time
            ON solarbank_measurements (site_id, device_sn, bucket_start DESC)
        """)
        connection.execute("""
            CREATE TABLE collector_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                started_at TEXT NOT NULL,
                last_attempt_at TEXT,
                last_success_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                measurements_written INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute(
            "INSERT INTO collector_state (id, started_at) VALUES (1, ?)",
            (datetime.now(UTC).isoformat(timespec="microseconds"),),
        )
        connection.execute("""
            CREATE TABLE users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                auth_nonce TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE login_attempts (
                id TEXT PRIMARY KEY,
                client_key TEXT NOT NULL,
                attempted_at REAL NOT NULL,
                pending INTEGER NOT NULL DEFAULT 1
            )
        """)
        connection.execute("""
            CREATE INDEX login_attempts_client_time
            ON login_attempts (client_key, attempted_at)
        """)

    def ready(self) -> bool:
        with self.connection() as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            connection.execute("SELECT 1 FROM solarbank_measurements LIMIT 1")
            state = connection.execute(
                "SELECT 1 FROM collector_state WHERE id = 1"
            ).fetchone()
            accounts = UserStore(connection)
            return (
                version == SCHEMA_VERSION
                and state is not None
                and accounts.schema_ready()
                and accounts.count() > 0
            )

    def devices(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY site_id, device_sn
                        ORDER BY bucket_start DESC, collected_at DESC
                    ) AS row_number
                    FROM solarbank_measurements
                )
                SELECT site_id, device_sn, device_name, device_model FROM ranked
                WHERE row_number = 1 ORDER BY site_id, device_sn
            """).fetchall()
        return [_serialize(row) for row in rows]

    def measurements(
        self, clauses: list[str], parameters: list[Any], limit: int
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                WITH filtered AS (
                    SELECT {", ".join(MEASUREMENT_COLUMNS)},
                           COUNT(*) OVER () AS filtered_row_count
                    FROM solarbank_measurements WHERE {" AND ".join(clauses)}
                ), ranked_buckets AS (
                    SELECT bucket_start, SUM(COUNT(*)) OVER (
                        ORDER BY bucket_start DESC
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS cumulative_rows
                    FROM filtered GROUP BY bucket_start
                )
                SELECT filtered.* FROM filtered
                JOIN ranked_buckets USING (bucket_start)
                WHERE cumulative_rows <= ?
                ORDER BY bucket_start, site_id, device_sn
            """,
                [*parameters, limit],
            ).fetchall()
        return [_serialize(row) for row in rows]

    def measurement_count(self, clauses: list[str], parameters: list[Any]) -> int:
        with self.connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM solarbank_measurements "
                f"WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchone()[0]

    def latest(
        self, clauses: list[str], parameters: list[str], *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY site_id, device_sn
                        ORDER BY bucket_start DESC, collected_at DESC
                    ) AS row_number
                    FROM solarbank_measurements
                    WHERE {" AND ".join(clauses) or "1 = 1"}
                )
                SELECT site_id, device_sn, device_name, collected_at,
                       source_updated_at, data_valid
                FROM ranked WHERE row_number = 1 ORDER BY site_id, device_sn
                LIMIT ?
            """,
                [*parameters, limit if limit is not None else -1],
            ).fetchall()
        return [_serialize(row) for row in rows]

    def snapshot(
        self, timestamp: str, site_id: str, device_sn: str
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT bucket_start, collected_at, source_updated_at, raw_data
                FROM solarbank_measurements
                WHERE bucket_start = ? AND site_id = ? AND device_sn = ?
            """,
                (timestamp, site_id, device_sn),
            ).fetchone()
        return _serialize(row) if row else None

    def collector_state(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("""
                SELECT last_attempt_at, last_success_at, consecutive_failures,
                       measurements_written FROM collector_state WHERE id = 1
            """).fetchone()
        return dict(row)

    def mark_attempt(self, timestamp: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE collector_state SET last_attempt_at = ? WHERE id = 1",
                (timestamp,),
            )

    def mark_failure(self, error: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE collector_state
                SET consecutive_failures = consecutive_failures + 1, last_error = ?
                WHERE id = 1
            """,
                (error[:500],),
            )

    def save(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        written = 0
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                written += connection.execute(
                    UPSERT_MEASUREMENT, [row[name] for name in _WRITE_COLUMNS]
                ).rowcount
            valid = [row["collected_at"] for row in rows if row["data_valid"]]
            if valid:
                connection.execute(
                    """
                    UPDATE collector_state SET
                        last_success_at = CASE
                            WHEN last_success_at IS NULL OR last_success_at < ?
                            THEN ? ELSE last_success_at END,
                        consecutive_failures = 0, last_error = NULL,
                        measurements_written = measurements_written + ?
                    WHERE id = 1
                """,
                    (max(valid), max(valid), written),
                )
            else:
                connection.execute(
                    """
                    UPDATE collector_state
                    SET measurements_written = measurements_written + ?
                    WHERE id = 1
                """,
                    (written,),
                )
        return written

    def _auth(self, connection: sqlite3.Connection) -> AuthService:
        return AuthService(
            UserStore(connection),
            self.settings.secret_key,
            lifetime_seconds=self.settings.session_lifetime_seconds,
        )

    def login(
        self, username: str, password: str, client_key: str
    ) -> tuple[dict[str, str] | None, int | None]:
        client_hash = hmac.new(
            self.settings.secret_key.encode(), client_key.encode(), hashlib.sha256
        ).hexdigest()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = self._auth(connection).login(
                username,
                password,
                client_hash,
                legacy_attempt_keys=[("anker-login-v1", client_hash)],
            )
            if result.user is None:
                return None, result.retry_after
            return {**self._account(result.user), "id": result.token}, None

    def session_user(self, token: str) -> dict[str, str] | None:
        with self.connection() as connection:
            user = self._auth(connection).validate(token)
            return {**self._account(user), "id": token} if user else None

    def logout(self, token: str) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._auth(connection).logout(token)

    def reset_password(
        self, username: str, password: str, *, must_change: bool = False
    ) -> None:
        if type(must_change) is not bool:
            raise ValueError("Ungültige Passwortart.")
        password_hash = hash_password(password)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            accounts = UserStore(connection)
            user = accounts.find(username)
            if user is None:
                raise ValueError("Benutzer nicht gefunden.")
            accounts.update(
                user.id, password_hash=password_hash, must_change_password=must_change
            )

    @staticmethod
    def _account(user) -> dict[str, Any]:
        return {
            "username": user.username,
            "active": user.active,
            "must_change_password": user.must_change_password,
            "can_manage_users": "users.manage"
            in user.authorization.get("permissions", []),
            "last_login_at": user.last_login_at,
        }

    def manage_accounts(self, token: str, operation: str, data: dict) -> dict:
        # Verify the actor under the same lock as account writes.
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            accounts = UserStore(connection)
            actor = self._auth(connection).validate(token)
            if actor is None:
                raise PermissionError("Anmeldung erforderlich.")
            if (
                actor.must_change_password
                or "users.manage" not in actor.authorization.get("permissions", [])
            ):
                raise PermissionError("Zugriff nicht erlaubt.")
            if operation == "list":
                return {"users": [self._account(user) for user in accounts.all()]}
            must_change = data.get("must_change_password", True)
            if type(must_change) is not bool:
                raise ValueError("Ungültige Passwortart.")
            username = data.get("username")
            if not isinstance(username, str):
                raise ValueError("Benutzername fehlt.")
            password_hash = hash_password(data.get("password"))
            if operation == "create":
                if accounts.find(username):
                    raise ValueError("Dieser Benutzername ist bereits vergeben.")
                user = accounts.create(
                    id="usr_" + secrets.token_hex(12),
                    username=username,
                    password_hash=password_hash,
                    must_change_password=must_change,
                    authorization={},
                    metadata={"display_name": username},
                )
            elif operation == "reset":
                user = accounts.find(username)
                if user is None:
                    raise ValueError("Benutzer nicht gefunden.")
                user = accounts.update(
                    user.id,
                    password_hash=password_hash,
                    must_change_password=must_change,
                )
            else:
                raise ValueError("Unbekannte Aktion.")
            return {"user": self._account(user)}

    def change_password(self, token: str, old_password: str, password: str) -> dict:
        password_hash = hash_password(password)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            service = self._auth(connection)
            user = service.validate(token)
            if user is None:
                raise PermissionError("Anmeldung erforderlich.")
            if not isinstance(old_password, str) or not verify_password(
                user.password_hash, old_password
            ):
                raise ValueError("Das aktuelle Passwort ist nicht korrekt.")
            if verify_password(user.password_hash, password):
                raise ValueError("Bitte ein anderes neues Passwort wählen.")
            user = UserStore(connection).update(
                user.id, password_hash=password_hash, must_change_password=False
            )
            return {**self._account(user), "id": service.issue(user.id)}
