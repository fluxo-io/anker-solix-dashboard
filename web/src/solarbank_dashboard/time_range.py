from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MAX_DAYS = 90
PRESETS: dict[str, timedelta] = {
    "24 Stunden": timedelta(hours=24),
    "7 Tage": timedelta(days=7),
    "30 Tage": timedelta(days=30),
    "90 Tage": timedelta(days=90),
}


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: datetime
    end: datetime


def preset_window(label: str, now: datetime | None = None) -> TimeWindow:
    if label not in PRESETS:
        raise ValueError("Zeitraum ist unbekannt")
    end = now or datetime.now(UTC)
    if end.tzinfo is None:
        raise ValueError("Zeitpunkt muss eine Zeitzone enthalten")
    return TimeWindow(start=end - PRESETS[label], end=end)


def custom_window(
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
) -> TimeWindow:
    days = (end_date - start_date).days + 1
    if days < 1:
        raise ValueError("Ende liegt vor dem Anfang")
    if days > MAX_DAYS:
        raise ValueError("Zeitraum darf höchstens 90 Tage umfassen")

    local_start = datetime.combine(start_date, time.min, timezone)
    local_end = datetime.combine(end_date + timedelta(days=1), time.min, timezone)
    return TimeWindow(
        start=local_start.astimezone(UTC),
        end=local_end.astimezone(UTC),
    )
