from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg
import pytest

from solarbank_dashboard.config import Settings
from solarbank_dashboard.repository import fetch_measurements, verify_schema

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="PostgreSQL integration test is disabled"
)
TEST_DEVICE_SNS = ["DASHBOARD-INTEGRATION", "DASHBOARD-A", "DASHBOARD-B"]


def test_dashboard_role_can_read_but_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "DB_HOST": os.getenv("DB_HOST", "database"),
        "DB_PORT": os.getenv("DB_PORT", "5432"),
        "DB_NAME": os.getenv("DB_NAME", "solarbank"),
        "DB_USER": os.getenv("DB_USER", "solarbank_dashboard"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD", "change-me-dashboard"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    admin_user = os.getenv("TEST_ADMIN_DB_USER", "solarbank")
    admin_password = os.getenv("TEST_ADMIN_DB_PASSWORD", "change-me-too")
    timestamp = datetime(2026, 9, 21, 12, tzinfo=UTC)
    settings = Settings.from_environment()

    admin_connection = psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=admin_user,
        password=admin_password,
        autocommit=True,
    )
    try:
        admin_connection.execute(
            "DELETE FROM solarbank_measurements WHERE device_sn = ANY(%s)",
            (TEST_DEVICE_SNS,),
        )
        admin_connection.execute(
            """
            INSERT INTO solarbank_measurements (
                bucket_start, collected_at, site_id, device_sn, device_name,
                device_model, data_valid, pv_reported_total_w, battery_soc_pct,
                raw_data
            ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s, '{}'::jsonb)
            """,
            (
                timestamp,
                timestamp,
                "dashboard-test-site",
                "DASHBOARD-INTEGRATION",
                "Testgerät",
                "A17C5",
                301.0,
                78.0,
            ),
        )

        verify_schema(settings)
        rows = fetch_measurements(
            settings,
            start=datetime(2026, 9, 21, 11, tzinfo=UTC),
            end=datetime(2026, 9, 21, 13, tzinfo=UTC),
            devices=[("dashboard-test-site", "DASHBOARD-INTEGRATION")],
        )
        assert len(rows) == 1
        assert rows[0]["pv_reported_total_w"] == 301.0

        older = datetime(2026, 9, 21, 12, 10, tzinfo=UTC)
        newer = datetime(2026, 9, 21, 12, 15, tzinfo=UTC)
        with admin_connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO solarbank_measurements (
                    bucket_start, collected_at, site_id, device_sn, data_valid,
                    pv_reported_total_w, raw_data
                ) VALUES (%s, %s, %s, %s, TRUE, %s, '{}'::jsonb)
                """,
                [
                    (older, older, "dashboard-limit-site", "DASHBOARD-A", 100.0),
                    (older, older, "dashboard-limit-site", "DASHBOARD-B", 110.0),
                    (newer, newer, "dashboard-limit-site", "DASHBOARD-A", 120.0),
                    (newer, newer, "dashboard-limit-site", "DASHBOARD-B", 130.0),
                ],
            )

        limited_rows = fetch_measurements(
            settings,
            start=datetime(2026, 9, 21, 12, tzinfo=UTC),
            end=datetime(2026, 9, 21, 13, tzinfo=UTC),
            site_ids=["dashboard-limit-site"],
            limit=3,
        )
        assert len(limited_rows) == 2
        assert {row["device_sn"] for row in limited_rows} == {
            "DASHBOARD-A",
            "DASHBOARD-B",
        }
        assert {row["bucket_start"] for row in limited_rows} == {newer}
        assert limited_rows[0]["filtered_row_count"] == 4

        denied_statements = [
            (
                """
                INSERT INTO solarbank_measurements (
                    bucket_start, collected_at, site_id, device_sn, data_valid,
                    raw_data
                ) VALUES (%s, %s, %s, %s, TRUE, '{}'::jsonb)
                """,
                (timestamp, timestamp, "denied", "DENIED"),
            ),
            (
                "UPDATE solarbank_measurements SET data_valid = FALSE "
                "WHERE device_sn = %s",
                ("DASHBOARD-INTEGRATION",),
            ),
            (
                "DELETE FROM solarbank_measurements WHERE device_sn = %s",
                ("DASHBOARD-INTEGRATION",),
            ),
            ("CREATE TABLE dashboard_write_probe (id INTEGER)", None),
        ]
        for statement, parameters in denied_statements:
            with psycopg.connect(
                host=settings.db_host,
                port=settings.db_port,
                dbname=settings.db_name,
                user=settings.db_user,
                password=settings.db_password,
                autocommit=True,
            ) as reader:
                default_read_only = reader.execute(
                    "SHOW default_transaction_read_only"
                ).fetchone()[0]
                assert default_read_only == "on"
                reader.execute("SET default_transaction_read_only = off")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    reader.execute(statement, parameters)
    finally:
        admin_connection.execute(
            "DELETE FROM solarbank_measurements WHERE device_sn = ANY(%s)",
            (TEST_DEVICE_SNS,),
        )
        admin_connection.close()
