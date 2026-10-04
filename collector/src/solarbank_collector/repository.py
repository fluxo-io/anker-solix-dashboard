from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

from .config import Settings
from .models import Measurement

UPSERT_MEASUREMENT = """
INSERT INTO solarbank_measurements (
    bucket_start,
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
    raw_data
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
    $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
    $23, $24::jsonb
)
ON CONFLICT (bucket_start, site_id, device_sn) DO UPDATE SET
    collected_at = EXCLUDED.collected_at,
    source_updated_at = EXCLUDED.source_updated_at,
    device_name = EXCLUDED.device_name,
    device_model = EXCLUDED.device_model,
    data_valid = EXCLUDED.data_valid,
    pv_reported_total_w = EXCLUDED.pv_reported_total_w,
    pv_calculated_total_w = EXCLUDED.pv_calculated_total_w,
    pv_1_w = EXCLUDED.pv_1_w,
    pv_2_w = EXCLUDED.pv_2_w,
    pv_3_w = EXCLUDED.pv_3_w,
    pv_4_w = EXCLUDED.pv_4_w,
    battery_soc_pct = EXCLUDED.battery_soc_pct,
    battery_power_w = EXCLUDED.battery_power_w,
    output_power_w = EXCLUDED.output_power_w,
    home_load_w = EXCLUDED.home_load_w,
    grid_power_w = EXCLUDED.grid_power_w,
    grid_import_w = EXCLUDED.grid_import_w,
    grid_export_w = EXCLUDED.grid_export_w,
    grid_to_home_w = EXCLUDED.grid_to_home_w,
    grid_to_battery_w = EXCLUDED.grid_to_battery_w,
    raw_data = EXCLUDED.raw_data
WHERE
    (EXCLUDED.data_valid AND NOT solarbank_measurements.data_valid)
    OR (
        EXCLUDED.data_valid = solarbank_measurements.data_valid
        AND EXCLUDED.collected_at >= solarbank_measurements.collected_at
    )
"""


class MeasurementRepository:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: Any = None

    async def __aenter__(self) -> MeasurementRepository:
        self._pool = await asyncpg.create_pool(
            host=self._settings.db_host,
            port=self._settings.db_port,
            database=self._settings.db_name,
            user=self._settings.db_user,
            password=self._settings.db_password,
            min_size=1,
            max_size=2,
            command_timeout=30,
            server_settings={"application_name": "anker-solix-collector"},
        )
        await self.verify_schema()
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def verify_schema(self) -> None:
        version = await self._pool.fetchval(
            "SELECT MAX(version) FROM schema_migrations"
        )
        if version != 3:
            raise RuntimeError(f"Nicht unterstützte Datenbankversion: {version}")

    async def mark_attempt(self, attempted_at: datetime) -> None:
        await self._pool.execute(
            "UPDATE collector_state SET last_attempt_at = $1 WHERE id = 1",
            attempted_at,
        )

    async def mark_failure(self, error: str) -> None:
        await self._pool.execute(
            """
            UPDATE collector_state
            SET consecutive_failures = consecutive_failures + 1,
                last_error = $1
            WHERE id = 1
            """,
            error[:500],
        )

    async def save(self, rows: list[Measurement]) -> None:
        if not rows:
            return

        async with self._pool.acquire() as connection, connection.transaction():
            for row in rows:
                raw_json = json.dumps(
                    row.raw_data,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                await connection.execute(
                    UPSERT_MEASUREMENT,
                    row.bucket_start,
                    row.collected_at,
                    row.source_updated_at,
                    row.site_id,
                    row.device_sn,
                    row.device_name,
                    row.device_model,
                    row.data_valid,
                    row.pv_reported_total_w,
                    row.pv_calculated_total_w,
                    row.pv_1_w,
                    row.pv_2_w,
                    row.pv_3_w,
                    row.pv_4_w,
                    row.battery_soc_pct,
                    row.battery_power_w,
                    row.output_power_w,
                    row.home_load_w,
                    row.grid_power_w,
                    row.grid_import_w,
                    row.grid_export_w,
                    row.grid_to_home_w,
                    row.grid_to_battery_w,
                    raw_json,
                )

            valid_rows = [row for row in rows if row.data_valid]
            if valid_rows:
                await connection.execute(
                    """
                        UPDATE collector_state
                        SET last_success_at = $1,
                            consecutive_failures = 0,
                            last_error = NULL,
                            measurements_written = measurements_written + $2
                        WHERE id = 1
                        """,
                    max(row.collected_at for row in valid_rows),
                    len(rows),
                )
            else:
                await connection.execute(
                    """
                        UPDATE collector_state
                        SET measurements_written = measurements_written + $1
                        WHERE id = 1
                        """,
                    len(rows),
                )
