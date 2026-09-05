"""Incremental refresh, per-language outcomes, and absence handling.

These run against a real PostgreSQL because every decision under test is read back out of
the database: which languages have succeeded, which are still failing, and which Tables
the last authoritative inventory contained.
"""

from __future__ import annotations

import asyncio

import pytest
from nordicintel_core.database import HarvestRepository, MetadataRepository
from nordicintel_core.errors import UpstreamResponseError
from nordicintel_core.models import (
    DiagnosticStage,
    DiscoveryEntry,
    HarvestRequest,
    ItemStatus,
)
from support import (
    PROVIDER_ID,
    StubAdapter,
    fetch_result,
    harvest,
    owner,
    register,
)

from nordicintel_harvest.control import JobControl
from nordicintel_harvest.errors import HarvestStopped, JobFailed

pytestmark = pytest.mark.postgres

TAB1 = DiscoveryEntry(native_table_id="TAB1")
TAB2 = DiscoveryEntry(native_table_id="TAB2")

UPSTREAM_DOWN = UpstreamResponseError(
    "Upstream returned an unsuccessful response", code="upstream_response", status_code=503
)


async def test_first_run_harvests_every_language_and_a_second_run_skips() -> None:
    with owner() as session:
        register(session)
        first = await harvest(session, StubAdapter(entries=[TAB1, TAB2]))
        assert (first.summary.updated, first.summary.skipped, first.summary.failed) == (2, 0, 0)
        metadata = MetadataRepository(session)
        stored = metadata.get_table_by_native(PROVIDER_ID, "TAB1")
        assert stored is not None and stored.retired is False
        assert metadata.get_language(stored.table_id, "sv") is not None
        assert metadata.get_language(stored.table_id, "en") is not None

        # The adapter now reports its markers as unchanged for both Tables.
        unchanged = StubAdapter(entries=[TAB1, TAB2], refresh={"TAB1": [], "TAB2": []})
        second = await harvest(session, unchanged)
        assert (second.summary.updated, second.summary.skipped, second.summary.failed) == (
            0,
            2,
            0,
        )
        assert unchanged.fetched == []
        # A skipped language is still recorded as checked, so "unchanged" and "not looked
        # at since the last harvest" stay distinguishable.
        state = metadata.load_language_state(stored.table_id)
        assert state["sv"].last_checked_at > state["sv"].last_harvested_at


async def test_one_language_fails_while_the_other_is_published() -> None:
    with owner() as session:
        register(session)
        adapter = StubAdapter(entries=[TAB1], behaviour={("TAB1", "sv"): UPSTREAM_DOWN})
        run = await harvest(session, adapter)
        assert (run.summary.updated, run.summary.skipped, run.summary.failed) == (0, 0, 1)
        metadata = MetadataRepository(session)
        table = metadata.get_table_by_native(PROVIDER_ID, "TAB1")
        assert table is not None
        assert metadata.get_language(table.table_id, "en") is not None
        assert metadata.get_language(table.table_id, "sv") is None
        state = metadata.load_language_state(table.table_id)
        assert state["sv"].failed is True
        assert state["en"].failed is False
        item = HarvestRepository(session).list_items(run.job.id)[0]
        assert item.status is ItemStatus.FAILED
        assert item.error is not None
        assert item.error.details["languages"][0]["language"] == "sv"


async def test_a_failed_language_is_retried_even_when_the_adapter_says_nothing_changed() -> None:
    with owner() as session:
        register(session)
        await harvest(
            session, StubAdapter(entries=[TAB1], behaviour={("TAB1", "sv"): UPSTREAM_DOWN})
        )
        # The adapter's markers now match for both languages. Swedish is still owed a
        # retry, because it has no successful harvest to compare against.
        recovering = StubAdapter(entries=[TAB1], refresh={"TAB1": []})
        run = await harvest(session, recovering)
        assert recovering.fetched == [("TAB1", "sv")]
        assert (run.summary.updated, run.summary.skipped, run.summary.failed) == (1, 0, 0)
        metadata = MetadataRepository(session)
        table = metadata.get_table_by_native(PROVIDER_ID, "TAB1")
        assert table is not None
        assert metadata.load_language_state(table.table_id)["sv"].failed is False
        assert table.availability_status.value == "available"


async def test_a_brand_new_table_whose_first_fetch_fails_publishes_nothing() -> None:
    with owner() as session:
        register(session)
        adapter = StubAdapter(
            entries=[TAB1], languages=["sv"], behaviour={("TAB1", "sv"): UPSTREAM_DOWN}
        )
        run = await harvest(session, adapter)
        assert run.summary.failed == 1
        metadata = MetadataRepository(session)
        # No identity is minted for something that has never been read successfully.
        assert metadata.get_table_by_native(PROVIDER_ID, "TAB1") is None
        item = HarvestRepository(session).list_items(run.job.id)[0]
        assert item.table_id is None
        assert item.error is not None and item.error.stage is DiagnosticStage.FETCH_METADATA


async def test_force_refetches_a_table_the_adapter_considers_unchanged() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1]))
        forced = StubAdapter(entries=[TAB1], refresh={"TAB1": []})
        run = await harvest(session, forced, request=HarvestRequest(force=True))
        assert sorted(forced.fetched) == [("TAB1", "en"), ("TAB1", "sv")]
        assert run.summary.updated == 1


async def test_absence_retires_and_reappearance_restores_without_new_metadata() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1, TAB2]))
        metadata = MetadataRepository(session)
        table_two = metadata.get_table_by_native(PROVIDER_ID, "TAB2")
        assert table_two is not None

        gone = await harvest(session, StubAdapter(entries=[TAB1]))
        assert gone.summary.retired == [table_two.table_id]
        refreshed = metadata.get_table(table_two.table_id)
        assert refreshed is not None and refreshed.retired is True

        # TAB2 comes back unchanged, so this run accepts no metadata for it at all.
        back = StubAdapter(entries=[TAB1, TAB2], refresh={"TAB1": [], "TAB2": []})
        run = await harvest(session, back)
        assert back.fetched == []
        assert run.summary.restored == [table_two.table_id]
        restored = metadata.get_table(table_two.table_id)
        assert restored is not None and restored.retired is False


async def test_a_partial_inventory_never_decides_absence() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1, TAB2]))
        run = await harvest(session, StubAdapter(entries=[TAB1], authoritative=False))
        assert run.summary.retired == [] and run.summary.restored == []
        table_two = MetadataRepository(session).get_table_by_native(PROVIDER_ID, "TAB2")
        assert table_two is not None and table_two.retired is False


async def test_a_single_table_job_addresses_the_upstream_identity_and_retires_nothing() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1, TAB2]))
        metadata = MetadataRepository(session)
        table_one = metadata.get_table_by_native(PROVIDER_ID, "TAB1")
        assert table_one is not None

        narrowed = StubAdapter(entries=[TAB1, TAB2])
        run = await harvest(
            session,
            narrowed,
            request=HarvestRequest(table_id=table_one.table_id, force=True),
        )
        scope = narrowed.discovered[0]
        assert scope.table_id == table_one.table_id
        assert scope.native_table_id == "TAB1"
        # Discovery listed both Tables; only the requested one is processed, and the
        # inventory says nothing about absence because the scope named a Table.
        assert run.summary.items == 1
        assert run.summary.retired == []
        assert {native for native, _ in narrowed.fetched} == {"TAB1"}


async def test_a_language_a_table_does_not_exist_in_is_never_fetched() -> None:
    with owner() as session:
        register(session)
        # TAB2 is published in Swedish only, which upstream reports as a missing table
        # rather than an empty answer, so asking for English would fail it on every run.
        swedish_only = DiscoveryEntry(native_table_id="TAB2", available_languages=["sv"])
        adapter = StubAdapter(entries=[TAB1, swedish_only])
        run = await harvest(session, adapter)
        assert (run.summary.updated, run.summary.failed) == (2, 0)
        assert sorted(adapter.fetched) == [
            ("TAB1", "en"),
            ("TAB1", "sv"),
            ("TAB2", "sv"),
        ]
        metadata = MetadataRepository(session)
        table = metadata.get_table_by_native(PROVIDER_ID, "TAB2")
        assert table is not None
        assert table.availability_status.value == "available"
        assert list(metadata.load_language_state(table.table_id)) == ["sv"]

        # The floor that forces a never-harvested language must not reintroduce it.
        again = StubAdapter(entries=[TAB1, swedish_only], refresh={"TAB1": [], "TAB2": []})
        second = await harvest(session, again)
        assert again.fetched == []
        assert second.summary.skipped == 2


async def test_a_result_for_the_wrong_table_is_a_failure_not_a_silent_skip() -> None:
    with owner() as session:
        register(session)
        adapter = StubAdapter(
            entries=[TAB1],
            languages=["sv"],
            behaviour={("TAB1", "sv"): fetch_result("TAB2", "sv")},
        )
        run = await harvest(session, adapter)
        assert run.summary.failed == 1
        item = HarvestRepository(session).list_items(run.job.id)[0]
        assert item.error is not None and item.error.stage is DiagnosticStage.NORMALIZE
        assert MetadataRepository(session).get_table_by_native(PROVIDER_ID, "TAB2") is None


async def test_incomplete_discovery_fails_the_job_rather_than_retiring_everything() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1, TAB2]))
        broken = StubAdapter(entries=[], discovery_error=UPSTREAM_DOWN)
        with pytest.raises(JobFailed) as failure:
            await harvest(session, broken)
        assert failure.value.diagnostic.stage is DiagnosticStage.DISCOVERY
        table_two = MetadataRepository(session).get_table_by_native(PROVIDER_ID, "TAB2")
        assert table_two is not None and table_two.retired is False


async def test_cancellation_stops_the_traversal_and_closes_the_running_item() -> None:
    with owner() as session:
        register(session)
        controls: list[JobControl] = []

        def build(queue: HarvestRepository, job_id: int) -> JobControl:
            control = JobControl(queue, job_id, interval_seconds=60.0)
            controls.append(control)
            return control

        async def stop_and_never_answer() -> object:
            # The stop arrives while this request is still outstanding, which is the case
            # the interrupted item exists for.
            controls[0].request_stop("The job was cancelled.")
            await asyncio.sleep(3600)
            raise AssertionError("the cancelled request should never complete")

        adapter = StubAdapter(
            entries=[TAB1, TAB2],
            languages=["sv"],
            behaviour={("TAB2", "sv"): stop_and_never_answer},
        )
        with pytest.raises(HarvestStopped):
            await harvest(session, adapter, control_factory=build)
        queue = HarvestRepository(session)
        items = queue.list_items(1)
        assert [item.native_table_id for item in items] == ["TAB1", "TAB2"]
        assert items[0].status is ItemStatus.UPDATED
        assert items[1].status is ItemStatus.FAILED
        assert items[1].error is not None
        assert items[1].error.stage is DiagnosticStage.INTERRUPTED
        # The Table finished before the stop keeps everything it published.
        metadata = MetadataRepository(session)
        table = metadata.get_table_by_native(PROVIDER_ID, "TAB1")
        assert table is not None
        assert metadata.get_language(table.table_id, "sv") is not None
