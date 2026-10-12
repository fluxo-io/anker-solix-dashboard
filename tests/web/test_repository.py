from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from solarbank_dashboard import repository
from solarbank_dashboard.config import Settings


def test_client_preserves_filters_timestamps_and_uses_bearer_token():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "rows": [
                            {
                                "bucket_start": "2026-10-01T10:00:00+00:00",
                                "data_valid": True,
                            }
                        ]
                    }
                ).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        f"http://127.0.0.1:{server.server_port}", "t" * 32, "Europe/Berlin"
    )
    start = datetime(2026, 10, 1, tzinfo=UTC)
    try:
        rows = repository.fetch_measurements(
            settings,
            start=start,
            end=start + timedelta(days=1),
            site_ids=["a&b"],
            devices=[("site", "device")],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert rows[0]["bucket_start"] == datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert "site_ids=%5B%22a%26b%22%5D" in requests[0][0]
    assert requests[0][1] == "Bearer " + "t" * 32


def test_client_rejects_invalid_period_before_request():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        repository.fetch_measurements(
            Settings("http://api:8001", "t" * 32, "Europe/Berlin"),
            start=start,
            end=start + timedelta(days=91),
        )


def test_incomplete_http_response_becomes_api_error(monkeypatch):
    from http.client import IncompleteRead

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            raise IncompleteRead(b'{"rows":', 100)

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(repository, "build_opener", lambda *args: Opener())
    with pytest.raises(repository.ApiError):
        repository.request_api(
            Settings("http://api:8001", "t" * 32, "Europe/Berlin"), "/v1/devices"
        )
