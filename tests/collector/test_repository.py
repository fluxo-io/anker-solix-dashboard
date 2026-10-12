import asyncio
from dataclasses import fields
from datetime import UTC, datetime
from types import SimpleNamespace

from aiohttp import web
from solarbank_collector.models import Measurement
from solarbank_collector.repository import MeasurementRepository


def test_collector_http_round_trip():
    async def exercise():
        calls = []

        async def handle(request):
            assert request.headers["Authorization"] == "Bearer " + "t" * 32
            payload = await request.json() if request.method == "POST" else None
            calls.append((request.path, payload))
            return web.json_response({"status": "ok"})

        application = web.Application()
        application.router.add_route("*", "/{path:.*}", handle)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        settings = SimpleNamespace(
            api_url=f"http://127.0.0.1:{port}",
            api_token="t" * 32,
            api_timeout_seconds=5,
        )
        now = datetime(2026, 10, 1, tzinfo=UTC)
        try:
            async with MeasurementRepository(settings) as repository:
                await repository.mark_attempt(now)
                await repository.mark_failure("test failure")
                await repository.save([])
                values = {field.name: None for field in fields(Measurement)}
                values.update(
                    bucket_start=now,
                    collected_at=now,
                    site_id="site",
                    device_sn="device",
                    data_valid=True,
                    raw_data={"device": {"temperature": 22, "seen_at": now}},
                )
                await repository.save([Measurement(**values)])
            assert calls[0] == ("/healthz", None)
            assert calls[1] == (
                "/v1/collector/attempt",
                {"attempted_at": now.isoformat()},
            )
            assert calls[2] == ("/v1/collector/failure", {"error": "test failure"})
            assert calls[3][0] == "/v1/measurements"
            assert calls[3][1]["rows"][0]["bucket_start"] == now.isoformat()
            assert calls[3][1]["rows"][0]["raw_data"] == {
                "device": {"temperature": 22, "seen_at": str(now)}
            }
        finally:
            await runner.cleanup()

    asyncio.run(exercise())
