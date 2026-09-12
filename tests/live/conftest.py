"""Conftest for the live adapter test suite.

These tests make real HTTP calls to external provider APIs and are intended
for human verification of adapter interactions and final metadata output.

**Run manually** — live tests are excluded from the default ``pytest`` run:

    pytest tests/live/ -v

Baseline full-suite command (current adapters):

    pytest tests/live -v

Kill switch: each test respects a configurable timeout (seconds).
Set the ``LIVE_TEST_TIMEOUT`` environment variable to override the default
(120 seconds).  When the timeout fires the test saves whatever was collected
and is skipped rather than failed.

Logs are written to ``tests/live/live_test.log`` (overwritten each session).
Sample output objects are saved to ``tests/live/output/`` as static-named
JSON files (overwritten each run). The committed repository only keeps the
output directory placeholder; generated JSON samples are intentionally
ephemeral so stale schema snapshots do not linger in git.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

_LIVE_DIR = Path(__file__).parent
_LOG_FILE = _LIVE_DIR / "live_test.log"
_OUTPUT_DIR = _LIVE_DIR / "output"

_DEFAULT_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Session-scoped file logging (overwritten on every run)
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Wire up a fresh log file and create the output directory."""
    if "ENVIRONMENT" not in os.environ:
        os.environ["ENVIRONMENT"] = "dev"
    if "REQUEST_GLOBAL_MAX_CONCURRENCY" not in os.environ:
        os.environ["REQUEST_GLOBAL_MAX_CONCURRENCY"] = "10"

    _OUTPUT_DIR.mkdir(exist_ok=True)

    handler = logging.FileHandler(_LOG_FILE, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.addHandler(handler)
    # Ensure root level is permissive so per-logger levels control filtering.
    if root.level == logging.WARNING or root.level == 0:
        root.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def output_dir() -> Path:
    """Path to the directory where sample JSON outputs are saved."""
    return _OUTPUT_DIR


@pytest.fixture(scope="session")
def live_timeout() -> float:
    """Kill-switch timeout in seconds.  Override with LIVE_TEST_TIMEOUT env var."""
    return float(os.environ.get("LIVE_TEST_TIMEOUT", str(_DEFAULT_TIMEOUT)))
