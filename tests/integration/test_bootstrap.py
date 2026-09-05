"""The operator commands, exercised the way an operator runs them.

These go through ``main`` rather than the helpers underneath it, because the part worth
testing is the wiring: that a command reaches the right repository, and that the
create/preserve decision a schedule's first run needs is actually made.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nordicintel_core.database import HarvestRepository
from nordicintel_core.models import HarvestRequest, JobStatus
from support import PROVIDER_ID, database_url, owner, register

from nordicintel_harvest.bootstrap import main

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NORDICINTEL_DATABASE_URL", database_url())


def output(capsys: pytest.CaptureFixture[str]) -> object:
    return json.loads(capsys.readouterr().out)


def test_a_provider_and_schedule_can_be_configured_from_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = tmp_path / "scb.json"
    definition.write_text(
        json.dumps(
            {
                "id": PROVIDER_ID,
                "label": "Statistics Sweden",
                "region": "se",
                "adapter_type": "stub",
                "config": {"base_url": "https://example.test"},
            }
        ),
        encoding="utf-8",
    )
    assert main(["provider", "upsert", str(definition)]) == 0
    assert output(capsys)["region"] == "SE"

    assert main(["schedule", "set", PROVIDER_ID, "--every-seconds", "3600", "--start-now"]) == 0
    first = output(capsys)["next_run_at"]
    # Updating an interval without saying when it should next run must not silently move
    # the run that was already scheduled.
    assert main(["schedule", "set", PROVIDER_ID, "--every-seconds", "7200"]) == 0
    updated = output(capsys)
    assert updated["next_run_at"] == first
    assert updated["every_seconds"] == 7200


def test_a_manual_request_is_admitted_replayed_and_cancellable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with owner() as session:
        register(session)

    assert main(["harvest", "enqueue", PROVIDER_ID, "--languages", "sv,en", "--key", "k1"]) == 0
    job = output(capsys)
    assert job["request"]["languages"] == ["en", "sv"]
    assert main(["harvest", "enqueue", PROVIDER_ID, "--languages", "EN,SV", "--key", "k1"]) == 0
    assert output(capsys)["id"] == job["id"]
    # The same key with a different request is a conflict, not a second job.
    assert main(["harvest", "enqueue", PROVIDER_ID, "--force", "--key", "k1"]) == 1

    assert main(["jobs", "cancel", str(job["id"])]) == 0
    assert output(capsys)["status"] == JobStatus.CANCELLED.value


def test_disabling_a_provider_can_cascade_to_its_queue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with owner() as session:
        register(session)
        queued = HarvestRepository(session).enqueue(PROVIDER_ID, HarvestRequest())

    assert main(["provider", "disable", PROVIDER_ID, "--cascade"]) == 0
    result = output(capsys)
    assert result["enabled"] is False
    assert [job["id"] for job in result["cancelled"]] == [queued.id]

    with owner() as session:
        cancelled = HarvestRepository(session).get_job(queued.id)
        assert cancelled is not None and cancelled.status is JobStatus.CANCELLED


def test_a_missing_job_reports_an_error_rather_than_printing_nothing() -> None:
    assert main(["jobs", "show", "9999"]) == 1
