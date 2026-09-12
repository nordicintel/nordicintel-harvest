import asyncio

import pytest
from aiohttp import web

from nordicintel_harvest.catalog import CatalogClient, CatalogError


@pytest.mark.parametrize("status,expected_calls", [(503, 3), (401, 1), (422, 1)])
async def test_proxy_errors_retry_only_transient_statuses(status, expected_calls):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return web.Response(
            status=status, text="<html>Unavailable</html>", content_type="text/html"
        )

    app = web.Application()
    app.router.add_get("/test", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = CatalogClient(f"http://127.0.0.1:{port}", "test")
    try:
        with pytest.raises(CatalogError) as exc:
            await asyncio.wait_for(client.request("GET", "/test"), 5)
        assert exc.value.status == status
        assert calls == expected_calls
    finally:
        await client.close()
        await runner.cleanup()
