from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from solarbank_dashboard.freshness import (
    assess_source_freshness,
    parse_source_timestamp,
)

OBSERVED_AT = datetime(2026, 9, 22, 21, 50, tzinfo=UTC)
MAX_AGE = timedelta(minutes=15)


def assess(value, **kwargs):
    return assess_source_freshness(
        value, observed_at=OBSERVED_AT, max_age=MAX_AGE, **kwargs
    )


def test_naive_cloud_time_needs_explicit_source_timezone() -> None:
    raw = "2026-09-22 23:46:36"
    unknown = assess(raw)
    known = assess(raw, source_timezone=ZoneInfo("Europe/Berlin"))

    assert unknown.status == "unknown_timezone"
    assert unknown.timestamp is None
    assert unknown.is_usable  # Estimates can fall back to collection times.
    assert known.status == "fresh"
    assert known.timestamp == datetime(2026, 9, 22, 21, 46, 36, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-22T23:46:36+02:00",
        "2026-09-22T21:46:36Z",
        datetime(2026, 9, 22, 21, 46, 36, tzinfo=UTC),
        1790113596,
        "1790113596000",
    ],
)
def test_explicit_timestamps_parse_to_same_utc_instant(raw) -> None:
    assert parse_source_timestamp(raw) == datetime(
        2026, 9, 22, 21, 46, 36, tzinfo=UTC
    )


def test_recent_fetch_does_not_make_old_cloud_values_fresh() -> None:
    result = assess("2026-09-22T20:00:00Z")

    assert result.status == "stale"
    assert not result.is_usable
    assert result.timestamp == datetime(2026, 9, 22, 20, tzinfo=UTC)


def test_historical_sample_compares_source_to_collection_time() -> None:
    result = assess_source_freshness(
        "2025-09-22T20:47:00Z",
        observed_at=datetime(2025, 9, 22, 20, 50, tzinfo=UTC),
        max_age=MAX_AGE,
    )

    assert result.status == "fresh"


@pytest.mark.parametrize(
    ("raw", "status", "usable"),
    [
        (None, "missing", True),
        ("  ", "missing", True),
        ("broken date", "invalid", False),
        (True, "invalid", False),
        (float("nan"), "invalid", False),
        (float("inf"), "invalid", False),
        ("1e99", "invalid", False),
        ("2026-09-22T22:00:00Z", "future", False),
        ("2026-09-22T21:50:30Z", "fresh", True),
        ("2026-09-22T21:35:00Z", "fresh", True),
        ("2026-09-22T21:34:59Z", "stale", False),
    ],
)
def test_missing_invalid_and_boundary_values(raw, status, usable) -> None:
    result = assess(raw)

    assert result.status == status
    assert result.is_usable is usable


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ("2026-03-29 02:30:00", "invalid"),
        ("2026-10-25 02:30:00", "ambiguous_time"),
    ],
)
def test_dst_gap_and_ambiguous_clock_time_do_not_invent_instant(raw, status) -> None:
    result = assess(raw, source_timezone=ZoneInfo("Europe/Berlin"))

    assert result.status == status
    assert result.timestamp is None
    assert not result.is_usable


def test_invalid_cloud_flag_overrides_recent_timestamp() -> None:
    result = assess("2026-09-22T21:46:36Z", data_valid=False)

    assert result.status == "invalid_data"
    assert not result.is_usable
    assert result.timestamp is not None


def test_freshness_check_requires_aware_observation_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        assess_source_freshness(
            "2026-09-22T21:46:36Z",
            observed_at=datetime(2026, 9, 22, 21, 50),
            max_age=MAX_AGE,
        )
