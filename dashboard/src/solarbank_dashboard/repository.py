from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import Settings

SCHEMA_VERSION = 3
# Calendar ranges can cross a daylight-saving transition.
MAX_RANGE = timedelta(days=90, hours=2)
MAX_ROWS = 100_000


@dataclass(frozen=True, slots=True)
class Device:
    site_id: str
    device_sn: str
    device_name: str | None
    device_model: str | None

    @property
    def key(self) -> tuple[str, str]:
        return self.site_id, self.device_sn

    @property
    def label(self) -> str:
        name = self.device_name or self.device_model or "Solarbank"
        return f"{name} · {self.device_sn}"


def connect(settings: Settings, *, autocommit: bool = False) -> Any:
    return psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        connect_timeout=5,
        options="-c default_transaction_read_only=on -c statement_timeout=10000",
        application_name="anker-solix-dashboard",
        row_factory=dict_row,
        autocommit=autocommit,
    )


def verify_schema(settings: Settings) -> None:
    with connect(settings) as connection:
        version = connection.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
        if version != SCHEMA_VERSION:
            raise RuntimeError(f"Nicht unterstützte Datenbankversion: {version}")
        connection.execute("SELECT 1 FROM solarbank_measurements LIMIT 1").fetchone()
        connection.execute("SELECT 1 FROM collector_state LIMIT 1").fetchone()


def fetch_devices(settings: Settings) -> list[Device]:
    query = """
        SELECT DISTINCT ON (site_id, device_sn)
               site_id, device_sn, device_name, device_model
        FROM solarbank_measurements
        ORDER BY site_id, device_sn, bucket_start DESC
    """
    with connect(settings) as connection:
        rows = connection.execute(query).fetchall()
    return [Device(**row) for row in rows]


def measurement_query(
    *,
    start: datetime,
    end: datetime,
    site_ids: Sequence[str] = (),
    devices: Sequence[tuple[str, str]] = (),
    valid_only: bool = True,
    limit: int = MAX_ROWS,
) -> tuple[str, list[Any]]:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Zeitraum muss eine Zeitzone enthalten")
    if end <= start:
        raise ValueError("Zeitraum ist ungültig")
    if end - start > MAX_RANGE:
        raise ValueError("Zeitraum darf höchstens 90 Tage umfassen")
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"Limit muss zwischen 1 und {MAX_ROWS} liegen")

    clauses = ["bucket_start >= %s", "bucket_start < %s"]
    parameters: list[Any] = [start, end]

    if site_ids:
        clauses.append("site_id = ANY(%s)")
        parameters.append(list(site_ids))

    if devices:
        device_clauses: list[str] = []
        for site_id, device_sn in devices:
            device_clauses.append("(site_id = %s AND device_sn = %s)")
            parameters.extend((site_id, device_sn))
        clauses.append(f"({' OR '.join(device_clauses)})")

    if valid_only:
        clauses.append("data_valid IS TRUE")

    parameters.append(limit)
    query = f"""
        WITH filtered_measurements AS (
            SELECT bucket_start,
                   collected_at,
                   source_updated_at,
                   site_id,
                   device_sn,
                   device_name,
                   device_model,
                   data_valid,
                   pv_reported_total_w,
                   pv_calculated_total_w,
                   pv_1_w,
                   pv_2_w,
                   pv_3_w,
                   pv_4_w,
                   battery_soc_pct,
                   battery_power_w,
                   output_power_w,
                   home_load_w,
                   grid_power_w,
                   grid_import_w,
                   grid_export_w,
                   grid_to_home_w,
                   grid_to_battery_w,
                   COUNT(*) OVER () AS filtered_row_count
            FROM solarbank_measurements
            WHERE {" AND ".join(clauses)}
        ),
        ranked_buckets AS (
            SELECT bucket_start,
                   SUM(COUNT(*)) OVER (
                       ORDER BY bucket_start DESC
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS cumulative_rows
            FROM filtered_measurements
            GROUP BY bucket_start
        ),
        selected_measurements AS (
            SELECT measurements.*
            FROM filtered_measurements AS measurements
            JOIN ranked_buckets USING (bucket_start)
            WHERE ranked_buckets.cumulative_rows <= %s
        )
        SELECT *
        FROM selected_measurements
        ORDER BY bucket_start ASC, site_id ASC, device_sn ASC
    """
    return query, parameters


def fetch_measurements(
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
    site_ids: Sequence[str] = (),
    devices: Sequence[tuple[str, str]] = (),
    valid_only: bool = True,
    limit: int = MAX_ROWS,
) -> list[dict[str, Any]]:
    query, parameters = measurement_query(
        start=start,
        end=end,
        site_ids=site_ids,
        devices=devices,
        valid_only=valid_only,
        limit=limit,
    )
    with connect(settings) as connection:
        return connection.execute(query, parameters).fetchall()


def fetch_collector_state(settings: Settings) -> dict[str, Any] | None:
    query = """
        SELECT last_attempt_at,
               last_success_at,
               consecutive_failures,
               measurements_written
        FROM collector_state
        WHERE id = 1
    """
    with connect(settings) as connection:
        return connection.execute(query).fetchone()


def fetch_latest_measurements(
    settings: Settings,
    *,
    site_ids: Sequence[str] = (),
    devices: Sequence[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    """Read each selected device's latest snapshot, including invalid readings."""
    clauses = ["TRUE"]
    parameters: list[Any] = []
    if site_ids:
        clauses.append("site_id = ANY(%s)")
        parameters.append(list(site_ids))
    if devices:
        clauses.append(
            "("
            + " OR ".join("(site_id = %s AND device_sn = %s)" for _ in devices)
            + ")"
        )
        for site_id, device_sn in devices:
            parameters.extend((site_id, device_sn))
    query = f"""
        SELECT DISTINCT ON (site_id, device_sn)
               site_id, device_sn, device_name, collected_at,
               source_updated_at, data_valid
        FROM solarbank_measurements
        WHERE {" AND ".join(clauses)}
        ORDER BY site_id, device_sn, bucket_start DESC, collected_at DESC
    """
    with connect(settings) as connection:
        return connection.execute(query, parameters).fetchall()


def fetch_snapshot(
    settings: Settings, *, timestamp: datetime, site_id: str, device_sn: str
) -> dict[str, Any] | None:
    if timestamp.tzinfo is None:
        raise ValueError("Messzeit muss eine Zeitzone enthalten")
    with connect(settings) as connection:
        return connection.execute(
            """
            SELECT bucket_start, collected_at, source_updated_at, raw_data
            FROM solarbank_measurements
            WHERE bucket_start = %s AND site_id = %s AND device_sn = %s
            """,
            (timestamp, site_id, device_sn),
        ).fetchone()
