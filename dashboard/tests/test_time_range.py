from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from solarbank_dashboard.time_range import custom_window, preset_window


def test_preset_window_uses_exact_duration() -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    window = preset_window("24 Stunden", now)

    assert window.start == datetime(2026, 9, 20, 12, tzinfo=UTC)
    assert window.end == now


def test_custom_window_includes_complete_local_days() -> None:
    window = custom_window(
        date(2026, 9, 20),
        date(2026, 9, 21),
        ZoneInfo("Europe/Berlin"),
    )

    assert window.start == datetime(2026, 9, 19, 22, tzinfo=UTC)
    assert window.end == datetime(2026, 9, 21, 22, tzinfo=UTC)


def test_custom_window_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="Ende"):
        custom_window(
            date(2026, 9, 21),
            date(2026, 9, 20),
            ZoneInfo("Europe/Berlin"),
        )
