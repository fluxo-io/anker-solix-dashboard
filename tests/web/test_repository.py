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

        def read(self, _size=-1):
            raise IncompleteRead(b'{"rows":', 100)

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(repository, "build_opener", lambda *args: Opener())
    with pytest.raises(repository.ApiError):
        repository.request_api(
            Settings("http://api:8001", "t" * 32, "Europe/Berlin"), "/v1/devices"
        )


def test_dashboard_http_contract_keeps_api_results_and_never_sends_policy(
    dashboard_data,
):
    requests = []
    dashboard_data.update(
        {
            "rows": [{"bucket_start": "2026-10-01T10:00:00Z", "pv_total_w": 321}],
            "energy": [{"day": "2026-10-01", "pv_kwh": 9.25, "pv_coverage_pct": 19}],
            "energy_status": "available",
            "loaded_count": 1,
            "filtered_count": 1,
            "kpis": {"pv_total_w": 456},
        }
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(dashboard_data).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        f"http://127.0.0.1:{server.server_port}",
        "t" * 32,
        "UTC",
        poll_interval_seconds=60,
    )
    start = datetime(2026, 10, 1, tzinfo=UTC)
    try:
        data = repository.fetch_dashboard(
            settings, start=start, end=start + timedelta(days=1)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert data["energy"][0]["pv_kwh"] == 9.25
    assert data["energy"][0]["pv_coverage_pct"] == 19
    assert data["kpis"]["pv_total_w"] == 456
    assert data["rows"][0]["bucket_start"] == datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert data["poll_interval_seconds"] == 300
    assert requests[0][0].startswith("/v1/dashboard?")
    assert "poll_interval" not in requests[0][0] and "timezone" not in requests[0][0]
    assert requests[0][1] == "Bearer " + "t" * 32
    with pytest.raises(repository.ApiError):
        repository.fetch_dashboard(settings, start=start, end=start + timedelta(days=1))


@pytest.mark.parametrize(
    "field,value",
    [
        ("rows", [{}] * 5001),
        ("energy", [{}] * 93),
        ("energy_status", "bad"),
        ("truncated", "false"),
    ],
)
def test_invalid_or_oversized_dashboard_has_no_local_fallback(
    monkeypatch, dashboard_data, field, value
):
    dashboard_data[field] = value
    monkeypatch.setattr(repository, "request_api", lambda *a, **kw: dashboard_data)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(repository.ApiError):
        repository.fetch_dashboard(
            Settings("http://api", "t" * 32, "UTC"),
            start=start,
            end=start + timedelta(days=1),
        )


def test_body_deadline_stops_drip_before_byte_limit(monkeypatch):
    ticks = iter([0.0, 0.1, 0.2, 1.01])
    monkeypatch.setattr(repository.time, "monotonic", lambda: next(ticks))

    class Drip:
        def read(self, size):
            raise AssertionError("read may wait for the entire requested size")

        def read1(self, size):
            return b" "

    with pytest.raises(TimeoutError):
        repository._read_response(Drip(), 1024, 1.0)


def test_dashboard_body_uses_large_bounded_response_budget(monkeypatch, dashboard_data):
    import io

    class Response(io.BytesIO):
        pass

    raw = json.dumps({**dashboard_data, "padding": "x" * (1024 * 1024)}).encode()

    class Opener:
        def open(self, *args, **kwargs):
            return Response(raw)

    monkeypatch.setattr(repository, "build_opener", lambda *args: Opener())
    settings = Settings("http://api", "t" * 32, "UTC")
    assert repository.request_api(settings, "/v1/dashboard")["energy_status"] == "empty"
    monkeypatch.setattr(repository, "MAX_RESPONSE_BYTES", 1024)
    with pytest.raises(repository.ApiError):
        repository.request_api(settings, "/v1/dashboard")


def test_health_normal_and_dashboard_have_independent_time_budgets(monkeypatch):
    import io

    timeouts = []

    class Opener:
        def open(self, request, *, timeout):
            timeouts.append(timeout)
            return io.BytesIO(b'{"status":"ok"}')

    monkeypatch.setattr(repository, "build_opener", lambda *args: Opener())
    settings = Settings(
        "http://api",
        "t" * 32,
        "UTC",
        api_timeout_seconds=5,
        api_dashboard_timeout_seconds=20,
    )
    for path in ("/healthz", "/v1/devices", "/v1/dashboard"):
        repository.request_api(settings, path)
    assert timeouts == [2, 5, 20]
