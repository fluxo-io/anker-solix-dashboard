import asyncio
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

from solarbank_collector.source import AnkerCloudSource


def test_auth_cache_is_inside_configured_data_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    settings = SimpleNamespace(
        api_request_timeout_seconds=90,
        anker_username="synthetic@example.invalid",
        anker_password="synthetic-password",
        anker_country="DE",
        anker_payload_encryption=False,
    )

    async def exercise():
        async with AnkerCloudSource(settings, logging.getLogger("test")) as source:
            cache_path = Path(source._api.apisession._authFile)
            assert cache_path.parent == tmp_path / "authcache"
            assert source._session is not None

    asyncio.run(exercise())
