from __future__ import annotations

import asyncio
import email.utils
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientResponse, ClientSession, ClientTimeout, TCPConnector

from nordicintel_harvest.core.config import Settings, get_settings
from nordicintel_harvest.core.errors import ExternalAPIError
from nordicintel_harvest.core.rate_limiter import RateLimiter


def get_base_host(url: str) -> str:
    """Extract the lower-cased host from a URL."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid URL: {url}")
    return host.lower()


@dataclass
class _HostState:
    limiter: RateLimiter
    base_interval: float
    max_concurrency: int
    cooldown_until: float = 0.0
    cooldown_events: int = 0
    last_retry_after_seconds: float | None = None
    interval_slowdown: float = 0.0


@dataclass(frozen=True)
class RequestResult:
    """A fully buffered HTTP response detached from the client session."""

    status: int
    url: str
    headers: dict[str, str]
    body: bytes
    encoding: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the response has a successful HTTP status."""
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        """Decode the buffered response body as text."""
        return self.body.decode(self.encoding or "utf-8", errors="replace")

    def json(self) -> Any:
        """Decode the buffered response body as JSON."""
        return json.loads(self.text)


class RequestManager:
    """Shared request controller for per-host + process-wide HTTP limits."""

    def __init__(
        self,
        *,
        global_max_concurrency: int,
        default_interval: float = 2.1,
        default_max_concurrency: int | None = None,
        default_host_max_concurrency: int | None = None,
        timeout_seconds: int = 60,
        retry_429_default_cooldown_seconds: float = 60.0,
        retry_429_interval_increase_seconds: float = 0.5,
        retry_429_max_cooldown_seconds: float = 900.0,
        logger: logging.Logger | None = None,
        logger_level: int | None = None,
    ) -> None:
        host_max_concurrency = (
            default_host_max_concurrency
            if default_host_max_concurrency is not None
            else default_max_concurrency
        )
        if host_max_concurrency is None:
            host_max_concurrency = 10

        if default_interval < 0:
            raise ValueError("default_interval must be non-negative")
        if host_max_concurrency < 1:
            raise ValueError("default_host_max_concurrency must be at least 1")
        if global_max_concurrency < 1:
            raise ValueError("global_max_concurrency must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retry_429_default_cooldown_seconds < 0:
            raise ValueError("retry_429_default_cooldown_seconds must be >= 0")
        if retry_429_interval_increase_seconds < 0:
            raise ValueError("retry_429_interval_increase_seconds must be >= 0")
        if retry_429_max_cooldown_seconds < 0:
            raise ValueError("retry_429_max_cooldown_seconds must be >= 0")

        self.logger = logger or logging.getLogger("RequestManager")
        if logger_level is not None:
            # Explicit override only - otherwise inherit the level configured
            # by app.core.logging.configure_logging() on the root logger.
            self.logger.setLevel(logger_level)

        self._default_interval = float(default_interval)
        self._default_host_max_concurrency = int(host_max_concurrency)
        self._global_max_concurrency = int(global_max_concurrency)
        self._retry_429_default_cooldown_seconds = float(retry_429_default_cooldown_seconds)
        self._retry_429_interval_increase_seconds = float(retry_429_interval_increase_seconds)
        self._retry_429_max_cooldown_seconds = float(retry_429_max_cooldown_seconds)
        self._timeout = ClientTimeout(total=timeout_seconds)

        self._host_states: dict[str, _HostState] = {}
        self._state_lock = asyncio.Lock()

        self.session: ClientSession | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        logger: logging.Logger | None = None,
        logger_level: int | None = None,
    ) -> RequestManager:
        cfg = settings or get_settings()
        return cls(
            global_max_concurrency=cfg.REQUEST_GLOBAL_MAX_CONCURRENCY,
            default_interval=cfg.REQUEST_DEFAULT_INTERVAL_SECONDS,
            default_host_max_concurrency=cfg.REQUEST_DEFAULT_HOST_MAX_CONCURRENCY,
            timeout_seconds=cfg.REQUEST_TIMEOUT_SECONDS,
            retry_429_default_cooldown_seconds=cfg.REQUEST_429_DEFAULT_COOLDOWN_SECONDS,
            retry_429_interval_increase_seconds=cfg.REQUEST_429_INTERVAL_INCREASE_SECONDS,
            retry_429_max_cooldown_seconds=cfg.REQUEST_429_MAX_COOLDOWN_SECONDS,
            logger=logger,
            logger_level=logger_level,
        )

    async def start(self) -> None:
        if self.session is None or bool(getattr(self.session, "closed", False)):
            self.session = ClientSession(
                timeout=self._timeout,
                connector=TCPConnector(limit=self._global_max_concurrency),
            )

    async def close(self) -> None:
        self.logger.info("Shutting down RequestManager.")
        if self.session is not None:
            try:
                await self.session.close()
            finally:
                self.session = None

    async def _get_or_create_host_state(self, host: str) -> _HostState:
        async with self._state_lock:
            state = self._host_states.get(host)
            if state is None:
                state = _HostState(
                    limiter=RateLimiter(
                        interval=self._default_interval,
                        max_concurrency=self._default_host_max_concurrency,
                    ),
                    base_interval=self._default_interval,
                    max_concurrency=self._default_host_max_concurrency,
                )
                self._host_states[host] = state
            return state

    async def set_limit(
        self,
        url: str,
        interval: float | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        resolved_interval = self._default_interval if interval is None else float(interval)
        resolved_max_concurrency = (
            self._default_host_max_concurrency if max_concurrency is None else int(max_concurrency)
        )
        if resolved_interval < 0:
            raise ValueError("Interval must be non-negative.")
        if resolved_max_concurrency < 1:
            raise ValueError("Max concurrency must be at least 1.")

        host = get_base_host(url)
        async with self._state_lock:
            state = self._host_states.get(host)
            if state is None:
                self._host_states[host] = _HostState(
                    limiter=RateLimiter(
                        interval=resolved_interval,
                        max_concurrency=resolved_max_concurrency,
                    ),
                    base_interval=resolved_interval,
                    max_concurrency=resolved_max_concurrency,
                )
                return

            state.base_interval = resolved_interval
            if state.max_concurrency != resolved_max_concurrency:
                state.max_concurrency = resolved_max_concurrency
                state.limiter = RateLimiter(
                    interval=resolved_interval,
                    max_concurrency=resolved_max_concurrency,
                )
            else:
                state.limiter.interval = resolved_interval

    async def _wait_for_host_cooldown(self, state: _HostState) -> None:
        now = asyncio.get_running_loop().time()
        cooldown_remaining = state.cooldown_until - now
        if cooldown_remaining > 0:
            await asyncio.sleep(cooldown_remaining)

    def _effective_interval(self, state: _HostState) -> float:
        return state.base_interval + state.interval_slowdown

    def _parse_retry_after_seconds(self, value: str | None) -> float | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None

        try:
            seconds = float(normalized)
            return max(0.0, seconds)
        except ValueError:
            pass

        try:
            retry_after_dt = email.utils.parsedate_to_datetime(normalized)
            if retry_after_dt.tzinfo is None:
                retry_after_dt = retry_after_dt.replace(tzinfo=UTC)
            delta = (retry_after_dt - datetime.now(UTC)).total_seconds()
            return max(0.0, delta)
        except (TypeError, ValueError):
            return None

    def _compute_cooldown_seconds(self, retry_after: str | None) -> float:
        parsed = self._parse_retry_after_seconds(retry_after)
        seconds = self._retry_429_default_cooldown_seconds if parsed is None else parsed
        return min(seconds, self._retry_429_max_cooldown_seconds)

    async def _record_429(
        self,
        *,
        host: str,
        retry_after: str | None,
    ) -> None:
        cooldown_seconds = self._compute_cooldown_seconds(retry_after)
        now = asyncio.get_running_loop().time()

        async with self._state_lock:
            state = self._host_states[host]
            state.cooldown_until = max(state.cooldown_until, now + cooldown_seconds)
            state.cooldown_events += 1
            state.last_retry_after_seconds = cooldown_seconds

            state.interval_slowdown += self._retry_429_interval_increase_seconds

        self.logger.warning(
            "HTTP 429 received for host=%s cooldown_seconds=%.3f",
            host,
            cooldown_seconds,
        )

    async def _make_request(
        self,
        *,
        url: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | list[Any] | None = None,
        form_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ClientResponse | None:
        await self.start()
        if self.session is None:
            return None

        if "json" in kwargs and data is not None:
            raise ValueError("Cannot pass both 'data' and 'json' in request.")
        if form_data is not None and (data is not None or "json" in kwargs):
            raise ValueError("Cannot pass both JSON and form data in request.")
        json_data = kwargs.pop("json", data)

        host = get_base_host(url)
        state = await self._get_or_create_host_state(host)

        for attempt in range(1, 4):
            await self._wait_for_host_cooldown(state)
            interval_override = self._effective_interval(state)

            self.logger.debug(
                "-> %s %s (host=%s attempt=%d) params=%s headers=%s body=%s",
                method,
                url,
                host,
                attempt,
                params,
                headers,
                json_data,
            )
            try:
                async with state.limiter.throttle(interval_override=interval_override):
                    response = await self.session.request(
                        url=url,
                        method=method,
                        params=params,
                        json=json_data,
                        data=form_data,
                        headers=headers,
                        **kwargs,
                    )
            except Exception as exc:
                self.logger.error("Request to %s failed: %s", url, exc)
                return None

            self.logger.debug(
                "<- %s %s -> %d headers=%s",
                method,
                url,
                response.status,
                dict(response.headers),
            )
            if response.status != 429:
                return response

            await self._record_429(
                host=host,
                retry_after=response.headers.get("Retry-After"),
            )
            response.release()
            if attempt >= 3:
                self.logger.error("Request to %s failed after retries (HTTP 429)", url)
                raise ExternalAPIError(
                    f"Request to {url} failed after 3 retries (HTTP 429)",
                    url=url,
                )

        return None

    async def request(
        self,
        url: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_data: dict[str, Any] | list[Any] | None = None,
        form_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> RequestResult | None:
        """Make a managed request and return a status-preserving buffered result.

        Unlike :meth:`make_request`, non-success HTTP responses are returned so
        callers can implement protocol-specific fallback behavior. The response
        body is consumed and the underlying connection is released before this
        method returns. ``form_data`` is sent as form-encoded request data;
        ``json_data`` is sent as JSON.
        """
        response = await self._make_request(
            url=url,
            method=method,
            params=params,
            headers=headers,
            data=json_data,
            form_data=form_data,
            **kwargs,
        )
        if response is None:
            return None

        try:
            body = await response.read()
            return RequestResult(
                status=response.status,
                url=str(response.url),
                headers=dict(response.headers),
                body=body,
                encoding=response.charset,
            )
        except Exception as exc:
            self.logger.error("Failed to buffer response from %s: %s", url, exc)
            return None
        finally:
            response.release()

    async def make_request(
        self,
        url: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> ClientResponse | None:
        response = await self._make_request(
            url=url,
            method=method,
            params=params,
            headers=headers,
            data=data,
            **kwargs,
        )
        if response is None:
            return None
        try:
            response.raise_for_status()
        except Exception as exc:
            self.logger.error("Request to %s returned error status: %s", url, exc)
            return None
        return response

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | list[Any] | None:
        response = await self.make_request(
            url=url,
            method="GET",
            params=params,
            **kwargs,
        )
        if response is None:
            return None

        try:
            payload = await response.json(content_type=None)
        except TypeError:
            payload = await response.json()
        except Exception:
            try:
                payload = json.loads(await response.text())
            except Exception:
                return None

        if not isinstance(payload, (dict, list)):
            return None
        return payload

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str | None:
        response = await self.make_request(
            url=url,
            method="GET",
            params=params,
            **kwargs,
        )
        if response is None:
            return None
        try:
            return await response.text()
        except Exception:
            return None

    async def get_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        response = await self.make_request(
            url=url,
            method="GET",
            params=params,
            **kwargs,
        )
        if response is None:
            return None
        try:
            return await response.read()
        except Exception:
            return None

    def get_host_cooldown_events(self) -> dict[str, int]:
        return {host: state.cooldown_events for host, state in self._host_states.items()}

    def get_effective_interval(self, url: str) -> float:
        host = get_base_host(url)
        state = self._host_states.get(host)
        if state is None:
            return self._default_interval
        return self._effective_interval(state)
