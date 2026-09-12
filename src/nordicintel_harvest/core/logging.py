"""Logging configuration for nordicintel-backend.

Call configure_logging() once at application startup - in main.py
and harvest.py. Safe to call multiple times (idempotent).

Environments:
    dev, test - DEBUG level, human-readable format with timestamps.
                Console only.
    other     - INFO level, production format for log aggregators.
                Console only.

All loggers in this project use %s-style formatting (lazy string construction).

Individual modules should not call setLevel() on their own loggers - that
pins them below whatever this function decides, regardless of environment.
Loggers should be created with logging.getLogger(__name__) and left at the
default (NOTSET) so they inherit the level configured here.
"""

import logging
import sys

_CONFIGURED = False

_DEBUG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_PROD_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure_logging() -> None:
    """Configure the root logger. Idempotent - subsequent calls are no-ops."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    from nordicintel_harvest.core.config import (
        get_settings,  # local import avoids circular at module load
    )

    settings = get_settings()
    is_debug_env = settings.ENVIRONMENT in ("dev", "test")

    fmt = _DEBUG_FORMAT if is_debug_env else _PROD_FORMAT
    formatter = logging.Formatter(fmt, datefmt=_DATE_FORMAT)
    level = logging.DEBUG if is_debug_env else logging.INFO

    root = logging.getLogger()

    console_handler = logging.StreamHandler(
        sys.stdout
    )  # stdout is intentional - no separate business output in this service
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    root.setLevel(level)
    _CONFIGURED = True
