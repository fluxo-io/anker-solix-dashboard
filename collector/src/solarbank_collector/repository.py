from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from http.client import HTTPException
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .config import Settings
from .models import Measurement


class MeasurementRepository:
    """The collector sends measurements to the API and never opens the database."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session: ClientSession | None = None

    async def __aenter__(self) -> MeasurementRepository:
        self._session = ClientSession(
            timeout=ClientTimeout(total=self._settings.api_timeout_seconds),
            json_serialize=lambda value: json.dumps(
                value, default=str, allow_nan=False
            ),
            headers={"Authorization": f"Bearer {self._settings.api_token}"},
        )
        try:
            await self.verify_schema()
        except Exception:
            await self._session.close()
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session is not None:
            await self._session.close()

    async def _request(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("API-Client ist nicht gestartet")
        method = "GET" if payload is None else "POST"
        try:
            async with self._session.request(
                method,
                self._settings.api_url + path,
                json=payload,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"API nicht verfügbar (HTTP {response.status})")
                return await response.json()
        except (HTTPException, ClientError, TimeoutError, ValueError):
            raise RuntimeError("API nicht verfügbar") from None

    async def verify_schema(self) -> None:
        if (await self._request("/healthz")).get("status") != "ok":
            raise RuntimeError("API nicht bereit")

    async def mark_attempt(self, attempted_at: datetime) -> None:
        await self._request(
            "/v1/collector/attempt", {"attempted_at": attempted_at.isoformat()}
        )

    async def mark_failure(self, error: str) -> None:
        await self._request("/v1/collector/failure", {"error": error[:500]})

    async def save(self, rows: list[Measurement]) -> None:
        if not rows:
            return
        payload = [
            {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in asdict(row).items()
            }
            for row in rows
        ]
        await self._request("/v1/measurements", {"rows": payload})
