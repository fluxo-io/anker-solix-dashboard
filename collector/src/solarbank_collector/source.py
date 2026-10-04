from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientSession, ClientTimeout
from anker_solix_api.api import AnkerSolixApi

from .config import Settings
from .mapping import build_measurements, utc_now
from .models import Measurement


class AnkerCloudSource:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger
        self._session: ClientSession | None = None
        self._api: AnkerSolixApi | None = None

    async def __aenter__(self) -> AnkerCloudSource:
        timeout = ClientTimeout(total=self._settings.api_request_timeout_seconds)
        self._session = ClientSession(timeout=timeout)
        self._api = AnkerSolixApi(
            email=self._settings.anker_username,
            password=self._settings.anker_password,
            countryId=self._settings.anker_country,
            websession=self._session,
            logger=self._logger,
        )
        self._api.payloadEncryption(self._settings.anker_payload_encryption)
        self._api.apisession.requestTimeout(
            min(self._settings.api_request_timeout_seconds, 60)
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session is not None:
            await self._session.close()

    async def fetch(self) -> list[Measurement]:
        if self._api is None:
            raise RuntimeError("Anker-Client ist nicht gestartet")

        async with asyncio.timeout(self._settings.api_request_timeout_seconds):
            await self._api.update_sites()
        collected_at = utc_now()
        return build_measurements(
            self._api.devices,
            self._api.sites,
            target_device_sn=self._settings.anker_device_sn,
            collected_at=collected_at,
            interval_seconds=self._settings.poll_interval_seconds,
        )
