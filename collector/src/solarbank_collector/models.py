from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Measurement:
    bucket_start: datetime
    collected_at: datetime
    source_updated_at: str | None
    site_id: str
    device_sn: str
    device_name: str | None
    device_model: str | None
    data_valid: bool
    pv_reported_total_w: float | None
    pv_calculated_total_w: float | None
    pv_1_w: float | None
    pv_2_w: float | None
    pv_3_w: float | None
    pv_4_w: float | None
    battery_soc_pct: float | None
    battery_power_w: float | None
    output_power_w: float | None
    home_load_w: float | None
    grid_power_w: float | None
    grid_import_w: float | None
    grid_export_w: float | None
    grid_to_home_w: float | None
    grid_to_battery_w: float | None
    raw_data: dict[str, Any]
