from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Literal

SourceStatus = Literal[
    "fresh",
    "stale",
    "missing",
    "invalid",
    "unknown_timezone",
    "ambiguous_time",
    "future",
    "invalid_data",
]


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    timestamp: datetime | None
    status: SourceStatus

    @property
    def is_usable(self) -> bool:
        """Allow unknown freshness for estimates, but never known bad samples."""
        return self.status in {"fresh", "missing", "unknown_timezone"}


def _parse_source_timestamp(
    value: Any, source_timezone: tzinfo | None
) -> SourceFreshness:
    if value is None or (isinstance(value, str) and not value.strip()):
        return SourceFreshness(None, "missing")
    if isinstance(value, bool):
        return SourceFreshness(None, "invalid")

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (str, int, float)):
        text = str(value).strip()
        try:
            epoch = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return SourceFreshness(None, "invalid")
        else:
            if not math.isfinite(epoch):
                return SourceFreshness(None, "invalid")
            # The API may expose Unix timestamps in seconds or milliseconds.
            if abs(epoch) >= 100_000_000_000:
                epoch /= 1_000
            try:
                return SourceFreshness(datetime.fromtimestamp(epoch, UTC), "fresh")
            except (OverflowError, OSError, ValueError):
                return SourceFreshness(None, "invalid")
    else:
        return SourceFreshness(None, "invalid")

    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        try:
            return SourceFreshness(parsed.astimezone(UTC), "fresh")
        except (OverflowError, ValueError):
            return SourceFreshness(None, "invalid")
    if source_timezone is None:
        return SourceFreshness(None, "unknown_timezone")

    # A local wall time can be missing or occur twice at a daylight-saving
    # transition. A configured timezone must not silently invent an instant.
    candidates: set[datetime] = set()
    try:
        for fold in (0, 1):
            local_time = parsed.replace(tzinfo=source_timezone, fold=fold)
            candidate = local_time.astimezone(UTC)
            if candidate.astimezone(source_timezone).replace(tzinfo=None) == parsed:
                candidates.add(candidate)
    except (OverflowError, ValueError):
        return SourceFreshness(None, "invalid")
    if not candidates:
        return SourceFreshness(None, "invalid")
    if len(candidates) > 1:
        return SourceFreshness(None, "ambiguous_time")
    return SourceFreshness(candidates.pop(), "fresh")


def parse_source_timestamp(
    value: Any, *, source_timezone: tzinfo | None = None
) -> datetime | None:
    """Return a UTC instant only when the source timezone is unambiguous."""
    return _parse_source_timestamp(value, source_timezone).timestamp


def assess_source_freshness(
    value: Any,
    *,
    observed_at: datetime,
    max_age: timedelta,
    source_timezone: tzinfo | None = None,
    data_valid: bool = True,
    future_tolerance: timedelta = timedelta(seconds=60),
) -> SourceFreshness:
    """Compare cloud time with now, or with collection time for past samples.

    Naive source timestamps stay unknown unless their timezone was configured.
    An unavailable source time is distinct from a known stale/invalid value;
    callers may still make clearly labelled estimates from collection times.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at needs timezone information")
    if max_age < timedelta(0) or future_tolerance < timedelta(0):
        raise ValueError("freshness tolerances must not be negative")

    result = _parse_source_timestamp(value, source_timezone)
    if not data_valid:
        return SourceFreshness(result.timestamp, "invalid_data")
    if result.timestamp is None:
        return result
    age = observed_at.astimezone(UTC) - result.timestamp
    if age < -future_tolerance:
        return SourceFreshness(result.timestamp, "future")
    if age > max_age:
        return SourceFreshness(result.timestamp, "stale")
    return result
