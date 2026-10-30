import pytest
from solarbank_dashboard import repository
from solarbank_dashboard.app import create_app
from solarbank_dashboard.auth import AuthSettings
from solarbank_dashboard.config import Settings


def test_factories_have_independent_clients_and_config():
    calls = []

    def transport(config, path, **kwargs):
        calls.append((config.api_url, path))
        return {"status": "ok"}

    first = create_app(
        Settings("http://first", "t" * 32, "Europe/Berlin"),
        auth_config=AuthSettings(enabled=False),
        api_client=transport,
    )
    second = create_app(
        Settings("http://second", "t" * 32, "UTC"),
        auth_config=AuthSettings(enabled=False),
        api_client=transport,
    )
    assert first.server.test_client().get("/healthz").json == {"status": "ok"}
    assert second.server.test_client().get("/healthz").json == {"status": "ok"}
    assert calls == [("http://first", "/healthz"), ("http://second", "/healthz")]


def test_answer_limit_applies_before_json_decode(monkeypatch):
    monkeypatch.setattr(repository, "SMALL_RESPONSE_BYTES", 16)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b" " * size

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(repository, "build_opener", lambda *args: Opener())
    with pytest.raises(repository.ApiError):
        repository.request_api(Settings("http://api", "t" * 32, "UTC"), "/healthz")
