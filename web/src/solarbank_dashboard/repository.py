from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Settings

MAX_RANGE = timedelta(days=90, hours=2)
MAX_ROWS = 100_000
TIME_FIELDS = {"bucket_start", "collected_at", "last_attempt_at", "last_success_at"}


class ApiError(RuntimeError):
    def __init__(self, status: int = 503, retry_after: int = 60) -> None:
        super().__init__("Daten nicht verfügbar")
        self.status = status
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_api(
    settings: Settings,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = settings.api_url + path
    if params:
        url += "?" + urlencode(params)
    headers = {
        "Authorization": f"Bearer {settings.api_token}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, allow_nan=False).encode()
    request = Request(url, data=data, headers=headers)
    try:
        with build_opener(NoRedirect).open(
            request, timeout=settings.api_timeout_seconds
        ) as response:
            result = json.load(response)
    except HTTPError as error:
        raw_retry = error.headers.get("Retry-After", "60")
        retry_after = int(raw_retry) if raw_retry.isdigit() else 60
        raise ApiError(error.code, retry_after) from None
    except (HTTPException, URLError, TimeoutError, OSError, ValueError):
        raise ApiError() from None
    if not isinstance(result, dict):
        raise ApiError()
    return result


@dataclass(frozen=True, slots=True)
class Device:
    site_id: str
    device_sn: str
    device_name: str | None
    device_model: str | None

    @property
    def key(self) -> tuple[str, str]:
        return self.site_id, self.device_sn

    @property
    def label(self) -> str:
        return (
            f"{self.device_name or self.device_model or 'Solarbank'} · {self.device_sn}"
        )


def _dates(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: datetime.fromisoformat(value) if key in TIME_FIELDS and value else value
        for key, value in row.items()
    }


def _filters(
    site_ids: Sequence[str], devices: Sequence[tuple[str, str]]
) -> dict[str, str]:
    return {"site_ids": json.dumps(site_ids), "devices": json.dumps(devices)}


def verify_schema(settings: Settings) -> None:
    if request_api(settings, "/healthz").get("status") != "ok":
        raise ApiError()


def fetch_devices(settings: Settings) -> list[Device]:
    return [Device(**row) for row in request_api(settings, "/v1/devices")["rows"]]


def fetch_measurements(
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
    site_ids: Sequence[str] = (),
    devices: Sequence[tuple[str, str]] = (),
    valid_only: bool = True,
    limit: int = MAX_ROWS,
) -> list[dict[str, Any]]:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Zeitraum muss eine Zeitzone enthalten")
    if end <= start or end - start > MAX_RANGE:
        raise ValueError("Zeitraum muss zwischen 0 und 90 Tagen liegen")
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError("Limit ist ungültig")
    params = {
        **_filters(site_ids, devices),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "valid_only": str(valid_only).lower(),
        "limit": limit,
    }
    return [
        _dates(row)
        for row in request_api(settings, "/v1/measurements", params=params)["rows"]
    ]


def fetch_collector_state(settings: Settings) -> dict[str, Any] | None:
    state = request_api(settings, "/v1/collector-state")["state"]
    return _dates(state) if state else None


def fetch_latest_measurements(
    settings: Settings,
    *,
    site_ids: Sequence[str] = (),
    devices: Sequence[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    result = request_api(
        settings, "/v1/measurements/latest", params=_filters(site_ids, devices)
    )
    return [_dates(row) for row in result["rows"]]


def fetch_snapshot(
    settings: Settings,
    *,
    timestamp: datetime,
    site_id: str,
    device_sn: str,
) -> dict[str, Any] | None:
    if timestamp.tzinfo is None:
        raise ValueError("Messzeit muss eine Zeitzone enthalten")
    result = request_api(
        settings,
        "/v1/snapshot",
        params={
            "timestamp": timestamp.isoformat(),
            "site_id": site_id,
            "device_sn": device_sn,
        },
    )["snapshot"]
    return _dates(result) if result else None
