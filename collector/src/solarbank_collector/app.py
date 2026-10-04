from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from .config import ConfigurationError, Settings
from .mapping import seconds_until_next_bucket, utc_now
from .repository import MeasurementRepository
from .source import AnkerCloudSource

LOGGER = logging.getLogger("solarbank_collector")
RETRY_DELAYS_SECONDS = (5, 15, 30, 60)


class SecretRedactionFilter(logging.Filter):
    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "***")
        record.msg = message
        record.args = ()
        return True


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOGGER.addFilter(
        SecretRedactionFilter(
            settings.anker_username,
            settings.anker_password,
            settings.db_password,
        )
    )


def _safe_error(error: Exception, settings: Settings) -> str:
    message = f"{type(error).__name__}: {error}"
    for secret in (
        settings.anker_username,
        settings.anker_password,
        settings.db_password,
    ):
        if secret:
            message = message.replace(secret, "***")
    return message[:500]


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, seconds))
    except TimeoutError:
        return False
    return True


async def _collect_once(
    source: AnkerCloudSource,
    repository: MeasurementRepository,
    settings: Settings,
    stop_event: asyncio.Event,
) -> bool:
    final_error: Exception | None = None

    for attempt in range(settings.max_retries + 1):
        attempted_at = utc_now()
        try:
            await repository.mark_attempt(attempted_at)
            rows = await source.fetch()
            await repository.save(rows)
            invalid = sum(not row.data_valid for row in rows)
            if invalid == len(rows):
                raise RuntimeError("Cloud-Daten sind als ungültig markiert")
            LOGGER.info(
                "%d Messung(en) gespeichert%s",
                len(rows),
                f", davon {invalid} als ungültig markiert" if invalid else "",
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:  # The upstream library uses several exception types.
            final_error = error
            safe_error = _safe_error(error, settings)
            if attempt >= settings.max_retries:
                break
            delay = RETRY_DELAYS_SECONDS[min(attempt, len(RETRY_DELAYS_SECONDS) - 1)]
            LOGGER.warning(
                "Abruf fehlgeschlagen (%d/%d): %s; neuer Versuch in %d s",
                attempt + 1,
                settings.max_retries + 1,
                safe_error,
                delay,
            )
            if await _wait_or_stop(stop_event, delay):
                return False

    if final_error is not None:
        safe_error = _safe_error(final_error, settings)
        with contextlib.suppress(Exception):
            await repository.mark_failure(safe_error)
        LOGGER.error("Messzyklus fehlgeschlagen: %s", safe_error)
    return False


async def run(settings: Settings) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    LOGGER.info(
        "Collector gestartet: Intervall %d s, Land %s",
        settings.poll_interval_seconds,
        settings.anker_country,
    )

    async with (
        MeasurementRepository(settings) as repository,
        AnkerCloudSource(settings, LOGGER) as source,
    ):
        while not stop_event.is_set():
            await _collect_once(source, repository, settings, stop_event)
            if stop_event.is_set():
                break
            delay = seconds_until_next_bucket(utc_now(), settings.poll_interval_seconds)
            await _wait_or_stop(stop_event, delay)

    LOGGER.info("Collector beendet")


def main() -> None:
    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        logging.basicConfig(level="ERROR", format="%(levelname)s: %(message)s")
        LOGGER.error("Konfiguration ungültig: %s", error)
        raise SystemExit(2) from error

    _configure_logging(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        LOGGER.error("Collector beendet: %s", _safe_error(error, settings))
        raise SystemExit(1) from error
