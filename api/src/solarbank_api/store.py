from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

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
        self._dummy_hash = generate_password_hash(secrets.token_urlsafe(24))

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
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
                if not self.settings.admin_username or not (
                    self.settings.admin_password or self.settings.admin_password_hash
                ):
                    raise ValueError(
                        "INIT_ADMIN_USERNAME und INIT_ADMIN_PASSWORD "
                        "oder INIT_ADMIN_PASSWORD_HASH fehlen"
                    )
                password_hash = (
                    generate_password_hash(self.settings.admin_password)
                    if self.settings.admin_password
                    else self.settings.admin_password_hash
                )
                connection.execute(
                    "INSERT INTO users (username, password_hash, auth_nonce) "
                    "VALUES (?, ?, ?)",
                    (
                        self.settings.admin_username,
                        password_hash,
                        secrets.token_hex(16),
                    ),
                )
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
            user = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            return version == SCHEMA_VERSION and state is not None and user is not None

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

    def latest(self, clauses: list[str], parameters: list[str]) -> list[dict[str, Any]]:
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
            """,
                parameters,
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

    def login(
        self, username: str, password: str, client_key: str
    ) -> tuple[dict[str, str] | None, int | None]:
        client_hash = hmac.new(
            self.settings.secret_key.encode(), client_key.encode(), hashlib.sha256
        ).hexdigest()
        attempt_id, retry_after = self._reserve_login(client_hash)
        if retry_after is not None:
            return None, retry_after
        with self.connection() as connection:
            row = connection.execute(
                "SELECT username, password_hash, auth_nonce "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        valid = check_password_hash(
            row["password_hash"] if row else self._dummy_hash, password
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if row is not None and valid:
                connection.execute(
                    "DELETE FROM login_attempts WHERE client_key = ? "
                    "AND (id = ? OR pending = 0)",
                    (client_hash, attempt_id),
                )
                return self._user_identity(row), None
            connection.execute(
                "UPDATE login_attempts SET pending = 0 WHERE id = ?", (attempt_id,)
            )
        return None, None

    def _user_identity(self, row: sqlite3.Row) -> dict[str, str]:
        material = (
            f"{row['username']}\0{row['password_hash']}\0{row['auth_nonce']}"
        ).encode()
        user_id = hmac.new(
            self.settings.secret_key.encode(), material, hashlib.sha256
        ).hexdigest()
        return {"id": user_id, "username": row["username"]}

    def session_user(self, user_id: str) -> dict[str, str] | None:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT username, password_hash, auth_nonce FROM users"
            ).fetchall()
        for row in rows:
            user = self._user_identity(row)
            if hmac.compare_digest(user["id"].encode(), user_id.encode()):
                return user
        return None

    def _reserve_login(self, client_hash: str) -> tuple[str | None, int | None]:
        now = time.time()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM login_attempts WHERE attempted_at <= ?", (now - 3600,)
            )
            timestamps = [
                row[0]
                for row in connection.execute(
                    "SELECT attempted_at FROM login_attempts WHERE client_key = ? "
                    "ORDER BY attempted_at",
                    (client_hash,),
                )
            ]
            recent = [stamp for stamp in timestamps if stamp > now - 300]
            waits = []
            if len(recent) >= 5:
                waits.append(recent[-5] + 300 - now)
            if len(timestamps) >= 20:
                waits.append(timestamps[-20] + 3600 - now)
            if waits:
                return None, max(1, math.ceil(max(waits)))
            attempt_id = secrets.token_hex(16)
            connection.execute(
                "INSERT INTO login_attempts (id, client_key, attempted_at) "
                "VALUES (?, ?, ?)",
                (attempt_id, client_hash, now),
            )
        return attempt_id, None
