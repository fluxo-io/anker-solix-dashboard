from solarbank_dashboard import healthcheck


def test_healthcheck_uses_internal_web_port(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_url(url, timeout):
        assert url == "http://127.0.0.1:8000/healthz"
        assert timeout == 5
        return Response()

    monkeypatch.setattr(healthcheck, "urlopen", open_url)
    assert healthcheck.main() == 0


def test_healthcheck_reports_failure(monkeypatch):
    def failed(*a, **kw):
        raise OSError("unavailable")

    monkeypatch.setattr(healthcheck, "urlopen", failed)
    assert healthcheck.main() == 1
