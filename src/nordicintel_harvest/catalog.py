"""Small async client for the catalog's documented operational interface."""

import asyncio

import aiohttp


class CatalogError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"Catalog HTTP {status}: {message}")


class CatalogClient:
    def __init__(self, url, token, timeout=10):
        self.url = url.rstrip("/")
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        )

    async def close(self):
        await self.session.close()

    async def request(self, method, path, *, body=None, claim=None, params=None, retry=True):
        for attempt in range(3 if retry else 1):
            try:
                headers = {"X-Harvest-Claim": claim} if claim else {}
                async with self.session.request(
                    method, self.url + path, json=body, params=params, headers=headers
                ) as response:
                    if response.status == 204:
                        return None
                    payload = await response.json(content_type=None)
                    if response.status >= 400:
                        error = payload.get("error", {}) if isinstance(payload, dict) else {}
                        raise CatalogError(response.status, error.get("message", "request failed"))
                    return payload
            except (aiohttp.ClientError, TimeoutError, CatalogError) as exc:
                if isinstance(exc, CatalogError) and exc.status not in (429, 500, 502, 503, 504):
                    raise
                if attempt == (2 if retry else 0):
                    raise
                await asyncio.sleep(2**attempt)
