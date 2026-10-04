from __future__ import annotations

from typing import Any

import pytest

import solarbank_dashboard.healthcheck as healthcheck


class _Response:
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_healthcheck_sends_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def open_request(request: Any, *, timeout: int) -> _Response:
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setenv("DASHBOARD_HEALTH_TOKEN", "health-token")
    monkeypatch.setattr(healthcheck, "urlopen", open_request)

    assert healthcheck.main() == 0
    assert captured == {"authorization": "Bearer health-token", "timeout": 3}


def test_healthcheck_works_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def open_request(request: Any, *, timeout: int) -> _Response:
        captured["authorization"] = request.get_header("Authorization")
        return _Response()

    monkeypatch.delenv("DASHBOARD_HEALTH_TOKEN", raising=False)
    monkeypatch.setattr(healthcheck, "urlopen", open_request)

    assert healthcheck.main() == 0
    assert captured["authorization"] is None
