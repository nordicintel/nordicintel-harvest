from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from typing import Any

import pytest
from aiohttp import TCPConnector

from nordicintel_harvest.core.errors import ExternalAPIError
from nordicintel_harvest.core.rate_limiter import RateLimiter
from nordicintel_harvest.core.request_manager import RequestManager, RequestResult, get_base_host


async def test_zero_rate_limit_keeps_concurrency_without_spacing():
    manager = RequestManager(global_max_concurrency=2, default_interval=0)
    try:
        await manager.set_limit("https://example.com", interval=0, max_concurrency=1)
        assert manager.get_effective_interval("https://example.com") == 0
        limiter = RateLimiter(interval=0, max_concurrency=1)
        async with limiter.throttle():
            assert limiter.semaphore.locked()
        assert not limiter.semaphore.locked()
    finally:
        await manager.close()


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        payload: Any | None = None,
        *,
        headers: dict[str, str] | None = None,
        text_payload: str | None = None,
        bytes_payload: bytes | None = None,
        url: str = "https://api.example.com/final",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}
        self._text_payload = text_payload
        self._bytes_payload = bytes_payload
        self.url = url
        self.charset = "utf-8"
        self.released = False

    async def json(self, content_type: str | None = None) -> Any:
        return self._payload

    async def text(self) -> str:
        if self._text_payload is not None:
            return self._text_payload
        return json.dumps(self._payload)

    async def read(self) -> bytes:
        if self._bytes_payload is not None:
            return self._bytes_payload
        return (self._text_payload or "").encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def release(self) -> None:
        self.released = True


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []
        self.call_count = 0
        self.closed = False

    async def request(self, **kwargs: Any) -> _FakeResponse:
        self.call_count += 1
        self.calls.append(kwargs)
        if not self._responses:
            return _FakeResponse(status=500)
        return self._responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def test_get_base_host_lowercases() -> None:
    assert get_base_host("https://API.SCB.SE/OV0104/v2beta/api/v2") == "api.scb.se"


def test_get_base_host_invalid_url_raises() -> None:
    with pytest.raises(ValueError):
        get_base_host("not-a-url")


@pytest.mark.asyncio
async def test_set_limit_updates_host_limiter() -> None:
    manager = RequestManager(
        global_max_concurrency=5,
        default_interval=2.0,
        default_max_concurrency=5,
    )

    await manager.set_limit(
        "https://api.scb.se/OV0104/v2beta/api/v2", interval=0.1, max_concurrency=2
    )

    assert manager.get_effective_interval(
        "https://api.scb.se/OV0104/v2beta/api/v2/tables"
    ) == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_make_request_raises_when_both_json_and_data_provided() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    manager.session = _FakeSession([_FakeResponse(status=200)])  # type: ignore[assignment]

    with pytest.raises(ValueError):
        await manager.make_request(
            url="https://api.scb.se/OV0104/v2beta/api/v2/tables",
            method="POST",
            data={"a": 1},
            json={"b": 2},
        )


@pytest.mark.asyncio
async def test_make_request_returns_none_on_http_error_status() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    manager.session = _FakeSession([_FakeResponse(status=500)])  # type: ignore[assignment]

    response = await manager.make_request(url="https://api.scb.se/OV0104/v2beta/api/v2/tables")
    assert response is None


@pytest.mark.asyncio
async def test_make_request_returns_response_on_success() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    fake_session = _FakeSession([_FakeResponse(status=200, payload={"ok": True})])
    manager.session = fake_session  # type: ignore[assignment]

    response = await manager.make_request(url="https://api.scb.se/OV0104/v2beta/api/v2/tables")

    assert response is not None
    assert len(fake_session.calls) == 1


@pytest.mark.asyncio
async def test_request_returns_buffered_error_response_with_final_url() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    response = _FakeResponse(
        status=404,
        text_payload='{"detail": "missing"}',
        headers={"Content-Type": "application/json; charset=utf-8"},
        url="https://api.example.com/redirected",
    )
    manager.session = _FakeSession([response])  # type: ignore[assignment]

    result = await manager.request("https://api.example.com/original")

    assert result == RequestResult(
        status=404,
        url="https://api.example.com/redirected",
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=b'{"detail": "missing"}',
        encoding="utf-8",
    )
    assert result is not None
    assert result.ok is False
    assert result.json() == {"detail": "missing"}
    assert response.released is True


@pytest.mark.asyncio
async def test_request_sends_form_encoded_data_separately_from_json() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    fake_session = _FakeSession([_FakeResponse(status=200, text_payload="expanded")])
    manager.session = fake_session  # type: ignore[assignment]

    result = await manager.request(
        "https://example.com/tree",
        method="POST",
        form_data={"__VIEWSTATE": "state", "ActionButton": "expand"},
    )

    assert result is not None
    assert result.text == "expanded"
    assert fake_session.calls[0]["data"] == {
        "__VIEWSTATE": "state",
        "ActionButton": "expand",
    }
    assert fake_session.calls[0]["json"] is None


@pytest.mark.asyncio
async def test_request_rejects_json_and_form_data_together() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    manager.session = _FakeSession([_FakeResponse(status=200)])  # type: ignore[assignment]

    with pytest.raises(ValueError, match="JSON and form data"):
        await manager.request(
            "https://example.com/tree",
            json_data={"query": "all"},
            form_data={"ActionButton": "expand"},
        )


@pytest.mark.asyncio
async def test_start_configures_connector_global_limit() -> None:
    manager = RequestManager(
        global_max_concurrency=2,
        default_interval=0.001,
        default_max_concurrency=10,
    )

    await manager.start()
    assert manager.session is not None
    assert isinstance(manager.session.connector, TCPConnector)
    assert manager.session.connector.limit == 2
    await manager.close()


@pytest.mark.asyncio
async def test_host_cooldown_blocks_retry_after_429() -> None:
    manager = RequestManager(
        global_max_concurrency=2,
        default_interval=0.001,
        default_max_concurrency=2,
    )
    session = _FakeSession(
        [
            _FakeResponse(status=429, headers={"Retry-After": "1"}),
            _FakeResponse(status=200, payload={"ok": True}),
        ]
    )
    manager.session = session  # type: ignore[assignment]

    started = time.perf_counter()
    response = await manager.make_request("https://api.example.com/path")
    elapsed = time.perf_counter() - started

    assert response is not None
    assert session.call_count == 2
    assert elapsed >= 0.9
    assert manager.get_host_cooldown_events().get("api.example.com") == 1


@pytest.mark.asyncio
async def test_429_fallback_cooldown_used_for_invalid_retry_after() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.001,
        default_max_concurrency=1,
        retry_429_default_cooldown_seconds=0.05,
        retry_429_max_cooldown_seconds=1.0,
    )
    session = _FakeSession(
        [
            _FakeResponse(status=429, headers={"Retry-After": "bad"}),
            _FakeResponse(status=200, payload={"ok": True}),
        ]
    )
    manager.session = session  # type: ignore[assignment]

    started = time.perf_counter()
    response = await manager.make_request("https://api.example.com/path")
    elapsed = time.perf_counter() - started

    assert response is not None
    assert elapsed >= 0.045


@pytest.mark.asyncio
async def test_host_slowdown_accumulates_and_persists() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
        retry_429_interval_increase_seconds=0.25,
    )
    session = _FakeSession(
        [
            _FakeResponse(status=429, headers={"Retry-After": "0"}),
            _FakeResponse(status=200, payload={"ok": True}),
            _FakeResponse(status=429, headers={"Retry-After": "0"}),
            _FakeResponse(status=200, payload={"ok": True}),
            _FakeResponse(status=200, payload={"ok": True}),
        ]
    )
    manager.session = session  # type: ignore[assignment]

    url = "https://api.example.com/path"
    await manager.make_request(url)
    await manager.make_request(url)
    await manager.make_request(url)

    assert manager.get_host_cooldown_events()["api.example.com"] == 2
    assert manager.get_effective_interval(url) == pytest.approx(0.51)


@pytest.mark.asyncio
async def test_make_request_raises_external_api_error_when_429_retries_exhausted() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    fake_session = _FakeSession(
        [
            _FakeResponse(status=429, headers={"Retry-After": "0"}),
            _FakeResponse(status=429, headers={"Retry-After": "0"}),
            _FakeResponse(status=429, headers={"Retry-After": "0"}),
        ]
    )
    manager.session = fake_session  # type: ignore[assignment]

    with pytest.raises(ExternalAPIError) as exc_info:
        await manager.make_request(url="https://api.scb.se/OV0104/v2beta/api/v2/tables")

    assert "https://api.scb.se/OV0104/v2beta/api/v2/tables" in str(exc_info.value)
    assert exc_info.value.url == "https://api.scb.se/OV0104/v2beta/api/v2/tables"
    assert len(fake_session.calls) == 3


@pytest.mark.asyncio
async def test_rate_limiter_wait_enforces_interval_override() -> None:
    limiter = RateLimiter(interval=0.05, max_concurrency=1)

    start = time.perf_counter()
    async with limiter.throttle(interval_override=0.02):
        pass
    async with limiter.throttle(interval_override=0.02):
        pass
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.015


@pytest.mark.asyncio
async def test_rate_limiter_host_semaphore_cap() -> None:
    limiter = RateLimiter(interval=0.0001, max_concurrency=2)
    hold = asyncio.Event()
    active = 0
    max_active = 0

    async def runner() -> None:
        nonlocal active, max_active
        async with limiter.throttle(interval_override=0.0001):
            active += 1
            max_active = max(max_active, active)
            try:
                await hold.wait()
            finally:
                active -= 1

    tasks = [asyncio.create_task(runner()) for _ in range(4)]
    await asyncio.sleep(0.05)
    hold.set()
    await asyncio.gather(*tasks)

    assert max_active <= 2


def test_rate_limiter_init_emits_no_deprecation_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        RateLimiter(interval=1.0, max_concurrency=1)


@pytest.mark.asyncio
async def test_make_request_logs_retry_exhaustion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    manager.session = _FakeSession(  # type: ignore[assignment]
        [
            _FakeResponse(429, headers={"Retry-After": "0"}),
            _FakeResponse(429, headers={"Retry-After": "0"}),
            _FakeResponse(429, headers={"Retry-After": "0"}),
        ]
    )

    with caplog.at_level(logging.ERROR, logger="RequestManager"):
        with pytest.raises(ExternalAPIError):
            await manager.make_request("http://example.com/path")

    assert any("failed after retries" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_make_request_logs_request_and_response_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    manager.session = _FakeSession(  # type: ignore[assignment]
        [_FakeResponse(status=200, payload={"ok": True})]
    )

    with caplog.at_level(logging.DEBUG, logger="RequestManager"):
        await manager.make_request("https://api.example.com/path", params={"a": "1"})

    messages = [r.message for r in caplog.records]
    assert any("-> GET https://api.example.com/path" in m and "params=" in m for m in messages)
    assert any("<- GET https://api.example.com/path -> 200" in m for m in messages)


@pytest.mark.asyncio
async def test_make_request_does_not_retry_on_500() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    manager.session = _FakeSession([_FakeResponse(500)])  # type: ignore[assignment]

    result = await manager.make_request("http://example.com/path")

    assert result is None
    assert manager.session.call_count == 1


@pytest.mark.asyncio
async def test_make_request_returns_none_on_connection_error() -> None:
    class _ErrorSession:
        def __init__(self) -> None:
            self.call_count = 0
            self.closed = False

        async def request(self, **kwargs: Any) -> None:
            self.call_count += 1
            raise OSError("connection refused")

        async def close(self) -> None:
            self.closed = True

    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )
    manager.session = _ErrorSession()  # type: ignore[assignment]

    result = await manager.make_request("http://example.com/path")

    assert result is None


@pytest.mark.asyncio
async def test_request_manager_start_and_close_are_idempotent() -> None:
    manager = RequestManager(
        global_max_concurrency=1,
        default_interval=0.01,
        default_max_concurrency=1,
    )

    await manager.start()
    first_session = manager.session
    assert first_session is not None

    await manager.start()
    assert manager.session is first_session

    await manager.close()
    assert manager.session is None

    await manager.close()
    assert manager.session is None
