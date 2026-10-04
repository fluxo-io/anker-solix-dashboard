import asyncio
import logging
from types import SimpleNamespace

from solarbank_collector import app
from solarbank_collector.config import Settings


def _settings(max_retries: int) -> Settings:
    return Settings(
        anker_username="owner@example.com",
        anker_password="api-secret",
        anker_country="DE",
        anker_device_sn=None,
        anker_payload_encryption=False,
        poll_interval_seconds=300,
        api_request_timeout_seconds=90,
        max_retries=max_retries,
        log_level="INFO",
        db_host="database",
        db_port=5432,
        db_name="solarbank",
        db_user="solarbank",
        db_password="db-secret",
    )


class FakeRepository:
    def __init__(self) -> None:
        self.attempts = 0
        self.saved = []
        self.failures = []

    async def mark_attempt(self, _attempted_at) -> None:
        self.attempts += 1

    async def save(self, rows) -> None:
        self.saved.append(rows)

    async def mark_failure(self, error: str) -> None:
        self.failures.append(error)


def test_failed_fetch_is_retried(monkeypatch) -> None:
    class Source:
        calls = 0

        async def fetch(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            return [SimpleNamespace(data_valid=True)]

    source = Source()
    repository = FakeRepository()
    monkeypatch.setattr(app, "RETRY_DELAYS_SECONDS", (0,))

    result = asyncio.run(
        app._collect_once(
            source,
            repository,
            _settings(max_retries=1),
            asyncio.Event(),
        )
    )

    assert result is True
    assert source.calls == 2
    assert repository.attempts == 2
    assert len(repository.saved) == 1
    assert repository.failures == []


def test_final_error_is_redacted() -> None:
    class Source:
        async def fetch(self):
            raise RuntimeError("api-secret db-secret owner@example.com")

    repository = FakeRepository()
    result = asyncio.run(
        app._collect_once(
            Source(),
            repository,
            _settings(max_retries=0),
            asyncio.Event(),
        )
    )

    assert result is False
    assert repository.failures == ["RuntimeError: *** *** ***"]


def test_log_filter_redacts_upstream_arguments() -> None:
    settings = _settings(max_retries=0)
    record = logging.LogRecord(
        name="solarbank_collector",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Login failed for user %s with %s",
        args=(settings.anker_username, settings.anker_password),
        exc_info=None,
    )

    app.SecretRedactionFilter(settings.anker_username, settings.anker_password).filter(
        record
    )

    assert record.getMessage() == "Login failed for user *** with ***"
