import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from solarbank_collector.config import Settings
from solarbank_collector.models import Measurement
from solarbank_collector.repository import MeasurementRepository

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="PostgreSQL integration test is disabled"
)


def test_same_slot_is_updated_instead_of_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = {
        "ANKER_USERNAME": "test@example.com",
        "ANKER_PASSWORD": "not-used",
        "DB_HOST": os.getenv("DB_HOST", "database"),
        "DB_PORT": os.getenv("DB_PORT", "5432"),
        "DB_NAME": os.getenv("DB_NAME", "solarbank"),
        "DB_USER": os.getenv("DB_USER", "solarbank"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD", "change-me-too"),
    }
    for name, value in required.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_environment()
    slot = datetime(2026, 9, 21, 12, 5, tzinfo=UTC)
    first = Measurement(
        bucket_start=slot,
        collected_at=datetime(2026, 9, 21, 12, 7, tzinfo=UTC),
        source_updated_at="2026-09-21 14:07:00",
        site_id="integration-site",
        device_sn="INTEGRATION-TEST",
        device_name="Testgerät",
        device_model="A17C5",
        data_valid=True,
        pv_reported_total_w=301,
        pv_calculated_total_w=301,
        pv_1_w=69,
        pv_2_w=66,
        pv_3_w=85,
        pv_4_w=81,
        battery_soc_pct=78,
        battery_power_w=-42,
        output_power_w=343,
        home_load_w=424,
        grid_power_w=125,
        grid_import_w=130,
        grid_export_w=5,
        grid_to_home_w=123,
        grid_to_battery_w=7,
        raw_data={"revision": 1},
    )
    corrected = replace(
        first,
        collected_at=datetime(2026, 9, 21, 12, 8, tzinfo=UTC),
        pv_reported_total_w=302,
        raw_data={"revision": 2},
    )
    invalid = replace(
        corrected,
        collected_at=datetime(2026, 9, 21, 12, 9, tzinfo=UTC),
        data_valid=False,
        pv_reported_total_w=0,
        raw_data={"revision": 3},
    )

    async def scenario() -> None:
        async with MeasurementRepository(settings) as repository:
            await repository.save([first])
            await repository.save([corrected])
            await repository.save([invalid])
            result = await repository._pool.fetchrow(
                """
                SELECT COUNT(*) OVER () AS row_count,
                       pv_reported_total_w,
                       data_valid,
                       raw_data ->> 'revision' AS revision
                FROM solarbank_measurements
                WHERE site_id = $1 AND device_sn = $2 AND bucket_start = $3
                """,
                first.site_id,
                first.device_sn,
                first.bucket_start,
            )
            assert result["row_count"] == 1
            assert result["pv_reported_total_w"] == 302
            assert result["data_valid"] is True
            assert result["revision"] == "2"
            await repository._pool.execute(
                "DELETE FROM solarbank_measurements WHERE device_sn = $1",
                first.device_sn,
            )

    asyncio.run(scenario())
