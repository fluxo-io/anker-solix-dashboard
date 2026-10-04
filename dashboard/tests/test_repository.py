from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import solarbank_dashboard.repository as repository
from solarbank_dashboard.repository import MAX_ROWS, measurement_query, verify_schema


class _SchemaConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def __enter__(self) -> _SchemaConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str) -> _SchemaConnection:
        self.queries.append(query)
        return self

    def fetchone(self) -> dict[str, int] | None:
        if "schema_migrations" in self.queries[-1]:
            return {"version": repository.SCHEMA_VERSION}
        return None


def test_query_uses_parameters_for_filters() -> None:
    start = datetime(2026, 9, 20, tzinfo=UTC)
    end = datetime(2026, 9, 21, tzinfo=UTC)
    injection = "'; DROP TABLE solarbank_measurements; --"

    query, parameters = measurement_query(
        start=start,
        end=end,
        site_ids=[injection],
        devices=[("site", injection)],
        valid_only=True,
        limit=500,
    )

    assert injection not in query
    assert parameters == [start, end, [injection], "site", injection, 500]
    assert "data_valid IS TRUE" in query
    assert "cumulative_rows <= %s" in query
    assert query.count("%s") == len(parameters)


def test_query_rejects_more_than_90_days() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="90 Tage"):
        measurement_query(start=start, end=start + timedelta(days=91))


@pytest.mark.parametrize("limit", [0, MAX_ROWS + 1])
def test_query_rejects_invalid_limit(limit: int) -> None:
    start = datetime(2026, 9, 20, tzinfo=UTC)

    with pytest.raises(ValueError, match="Limit"):
        measurement_query(start=start, end=start + timedelta(hours=1), limit=limit)


def test_schema_readiness_checks_every_dashboard_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _SchemaConnection()
    monkeypatch.setattr(repository, "connect", lambda _settings: connection)

    verify_schema(object())  # type: ignore[arg-type]

    assert any("schema_migrations" in query for query in connection.queries)
    assert any("solarbank_measurements" in query for query in connection.queries)
    assert any("collector_state" in query for query in connection.queries)
