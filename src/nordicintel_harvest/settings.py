"""Explicit configuration for the worker and the scheduler.

Nothing here reads the environment implicitly at import time. A process builds one
:class:`Settings` from a mapping it chose, and every module that needs a value receives it
as an argument, so a test configures a run the same way a deployment does.

The intervals form one chain, and the constructor rejects a configuration that breaks it:
a worker must report liveness several times inside the window recovery uses to declare it
abandoned, or the scheduler will hand its job to someone else while it is still working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from nordicintel_core.errors import ConfigurationError

_PREFIX = "NORDICINTEL_"


@dataclass(frozen=True, slots=True)
class Settings:
    """One process's configuration, already validated."""

    database_url: str
    adapters: frozenset[str] | None = None
    heartbeat_seconds: float = 5.0
    stale_after_seconds: int = 180
    queue_poll_seconds: float = 2.0
    scheduler_poll_seconds: float = 15.0
    shutdown_budget_seconds: float = 30.0
    http_timeout_seconds: float = 30.0
    minimum_request_interval_seconds: float = 0.0
    statement_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.database_url.strip():
            raise ConfigurationError(f"{_PREFIX}DATABASE_URL must not be blank")
        positive = {
            "heartbeat_seconds": self.heartbeat_seconds,
            "stale_after_seconds": float(self.stale_after_seconds),
            "queue_poll_seconds": self.queue_poll_seconds,
            "scheduler_poll_seconds": self.scheduler_poll_seconds,
            "shutdown_budget_seconds": self.shutdown_budget_seconds,
            "http_timeout_seconds": self.http_timeout_seconds,
            "statement_timeout_seconds": self.statement_timeout_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ConfigurationError(f"{name} must be positive")
        if self.minimum_request_interval_seconds < 0:
            raise ConfigurationError("minimum_request_interval_seconds cannot be negative")
        # Two missed beats must still leave the job comfortably inside the stale window.
        if self.heartbeat_seconds * 3 > self.stale_after_seconds:
            raise ConfigurationError(
                "stale_after_seconds must be at least three heartbeat intervals"
            )
        # A statement that outlives the stale window can hold a job past its own recovery.
        if self.statement_timeout_seconds >= self.stale_after_seconds:
            raise ConfigurationError("statement_timeout_seconds must be below stale_after_seconds")
        if self.adapters is not None and not self.adapters:
            raise ConfigurationError(f"{_PREFIX}ADAPTERS must name at least one adapter")

    @property
    def statement_timeout_option(self) -> str:
        """The libpq ``options`` string that bounds a synchronous database call.

        A stalled statement blocks the owner thread, and the heartbeat timer runs on that
        same thread, so an unbounded query is indistinguishable from a dead worker.
        """
        return f"-c statement_timeout={int(self.statement_timeout_seconds * 1000)}"


def _name(key: str) -> str:
    return f"{_PREFIX}{key}"


def _number(environment: Mapping[str, str], key: str, default: float) -> float:
    raw = environment.get(_name(key))
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{_name(key)} must be a number") from exc


def _integer(environment: Mapping[str, str], key: str, default: int) -> int:
    raw = environment.get(_name(key))
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{_name(key)} must be an integer") from exc


def load_settings(environment: Mapping[str, str]) -> Settings:
    """Build settings from a supplied environment mapping.

    Args:
        environment: The variables to read, normally ``os.environ``.

    Returns:
        Validated settings.

    Raises:
        ConfigurationError: A required variable is missing, or a value is unusable.
    """
    database_url = environment.get(_name("DATABASE_URL"), "")
    if not database_url.strip():
        raise ConfigurationError(f"{_name('DATABASE_URL')} is required")
    raw_adapters = environment.get(_name("ADAPTERS"))
    adapters: frozenset[str] | None = None
    if raw_adapters is not None and raw_adapters.strip():
        adapters = frozenset(
            part.strip().lower() for part in raw_adapters.split(",") if part.strip()
        )
    return Settings(
        database_url=database_url,
        adapters=adapters,
        heartbeat_seconds=_number(environment, "HEARTBEAT_SECONDS", 5.0),
        stale_after_seconds=_integer(environment, "STALE_AFTER_SECONDS", 180),
        queue_poll_seconds=_number(environment, "QUEUE_POLL_SECONDS", 2.0),
        scheduler_poll_seconds=_number(environment, "SCHEDULER_POLL_SECONDS", 15.0),
        shutdown_budget_seconds=_number(environment, "SHUTDOWN_BUDGET_SECONDS", 30.0),
        http_timeout_seconds=_number(environment, "HTTP_TIMEOUT_SECONDS", 30.0),
        minimum_request_interval_seconds=_number(environment, "REQUEST_INTERVAL_SECONDS", 0.0),
        statement_timeout_seconds=_number(environment, "STATEMENT_TIMEOUT_SECONDS", 30.0),
    )
