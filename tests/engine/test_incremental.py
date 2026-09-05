"""Incremental refresh and per-language outcomes.

These run against a real PostgreSQL because every decision under test is read back out of
the database: which languages have succeeded, which are still failing, and what a second
run therefore does.
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


async def test_a_first_run_harvests_the_catalogue_and_a_second_run_skips() -> None:
    with owner() as session:
        register(session)
        first = await harvest(session, StubAdapter(entries=[TAB1, TAB2]))
        assert (first.summary.updated, first.summary.skipped, first.summary.failed) == (2, 0, 0)
        assert first.summary.language == "sv"
        metadata = MetadataRepository(session)
        stored = metadata.get_table_by_native(PROVIDER_ID, "TAB1")
        assert stored is not None
        assert metadata.get_language(stored.table_id, "sv") is not None
        # Only the job's own language was harvested.
        assert metadata.get_language(stored.table_id, "en") is None

        unchanged = StubAdapter(entries=[TAB1, TAB2], refresh={"TAB1": False, "TAB2": False})
        second = await harvest(session, unchanged)
        assert (second.summary.updated, second.summary.skipped, second.summary.failed) == (
            0,
            2,
            0,
        )
        assert unchanged.fetched == []
        # A skipped Table is still recorded as checked, so "current" and "not looked at
        # since the last harvest" stay distinguishable.
        state = metadata.load_language_state(stored.table_id)
        assert state["sv"].last_checked_at > state["sv"].last_harvested_at


async def test_each_language_is_its_own_run_over_its_own_catalogue() -> None:
    with owner() as session:
        register(session)
        # TAB2 is published in Swedish only, so it is simply not in the English listing.
        adapter = StubAdapter(entries=[], catalogues={"sv": [TAB1, TAB2], "en": [TAB1]})
        swedish = await harvest(session, adapter, language="sv")
        english = await harvest(session, adapter, language="en")

        assert swedish.summary.updated == 2
        assert english.summary.updated == 1
        assert sorted(adapter.fetched) == [("TAB1", "en"), ("TAB1", "sv"), ("TAB2", "sv")]
        # English was never requested for a Table that has no English version, so there
        # is no failure to explain and nothing marked unavailable.
        metadata = MetadataRepository(session)
        table_two = metadata.get_table_by_native(PROVIDER_ID, "TAB2")
        assert table_two is not None
        assert table_two.availability_status.value == "available"
        assert list(metadata.load_language_state(table_two.table_id)) == ["sv"]
        table_one = metadata.get_table_by_native(PROVIDER_ID, "TAB1")
        assert table_one is not None
        assert sorted(metadata.load_language_state(table_one.table_id)) == ["en", "sv"]


async def test_a_job_in_a_language_the_provider_does_not_publish_fails_once() -> None:
    with owner() as session:
        register(session)
        adapter = StubAdapter(entries=[TAB1, TAB2], languages=["sv"])
        with pytest.raises(JobFailed) as failure:
            await harvest(session, adapter, language="de")
        assert failure.value.diagnostic.code == "language_not_published"
        assert failure.value.diagnostic.details["supported"] == ["sv"]
        # It failed before any Table was touched, rather than once per Table.
        assert adapter.discovered == [] and adapter.fetched == []
        assert HarvestRepository(session).list_items(1) == []


async def test_a_failed_table_keeps_the_run_going_and_is_retried_next_time() -> None:
    with owner() as session:
        register(session)
        adapter = StubAdapter(entries=[TAB1, TAB2], behaviour={("TAB1", "sv"): UPSTREAM_DOWN})
        run = await harvest(session, adapter)
        assert (run.summary.updated, run.summary.skipped, run.summary.failed) == (1, 0, 1)
        metadata = MetadataRepository(session)
        # A brand new Table whose only fetch failed publishes no identity at all.
        assert metadata.get_table_by_native(PROVIDER_ID, "TAB1") is None
        assert metadata.get_table_by_native(PROVIDER_ID, "TAB2") is not None
        items = {item.native_table_id: item for item in HarvestRepository(session).list_items(1)}
        assert items["TAB1"].status is ItemStatus.FAILED
        assert items["TAB1"].error is not None
        assert items["TAB1"].error.stage is DiagnosticStage.FETCH_METADATA
        assert items["TAB1"].error.details["language"] == "sv"
        assert items["TAB2"].status is ItemStatus.UPDATED

        # The adapter now considers everything unchanged. TAB1 still has no accepted
        # metadata, so the engine's own check overrides that and fetches it anyway.
        recovering = StubAdapter(entries=[TAB1, TAB2], refresh={"TAB1": False, "TAB2": False})
        second = await harvest(session, recovering)
        assert recovering.fetched == [("TAB1", "sv")]
        assert (second.summary.updated, second.summary.skipped) == (1, 1)


async def test_a_failure_on_an_existing_table_is_recorded_against_its_language() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1]))
        run = await harvest(
            session, StubAdapter(entries=[TAB1], behaviour={("TAB1", "sv"): UPSTREAM_DOWN})
        )
        assert run.summary.failed == 1
        metadata = MetadataRepository(session)
        table = metadata.get_table_by_native(PROVIDER_ID, "TAB1")
        assert table is not None
        # The previously accepted metadata survives the failed attempt.
        assert metadata.get_language(table.table_id, "sv") is not None
        state = metadata.load_language_state(table.table_id)
        assert state["sv"].failed is True
        assert table.availability_status.value == "unavailable"


async def test_force_refetches_a_table_the_adapter_considers_unchanged() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1]))
        forced = StubAdapter(entries=[TAB1], refresh={"TAB1": False})
        run = await harvest(session, forced, request=HarvestRequest(language="sv", force=True))
        assert forced.fetched == [("TAB1", "sv")]
        assert run.summary.updated == 1


async def test_a_table_missing_from_a_later_run_keeps_everything_it_had() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1, TAB2]))
        metadata = MetadataRepository(session)
        table_two = metadata.get_table_by_native(PROVIDER_ID, "TAB2")
        assert table_two is not None

        # TAB2 is gone from the listing. Nothing acts on that: its identity, metadata and
        # language state are all left exactly as they were, and it stays searchable.
        run = await harvest(session, StubAdapter(entries=[TAB1]))
        assert run.summary.items == 1
        assert metadata.get_table(table_two.table_id) is not None
        assert metadata.get_language(table_two.table_id, "sv") is not None
        assert {row.table_id for row in metadata.search("Befolkning")} == {
            table_two.table_id,
            "scb-tab1",
        }


async def test_a_single_table_job_addresses_the_upstream_identity() -> None:
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
            request=HarvestRequest(language="sv", table_id=table_one.table_id, force=True),
        )
        scope = narrowed.discovered[0]
        assert (scope.language, scope.table_id, scope.native_table_id) == (
            "sv",
            table_one.table_id,
            "TAB1",
        )
        assert run.summary.items == 1
        assert narrowed.fetched == [("TAB1", "sv")]


async def test_a_result_for_the_wrong_table_is_a_failure_not_a_silent_skip() -> None:
    with owner() as session:
        register(session)
        adapter = StubAdapter(
            entries=[TAB1], behaviour={("TAB1", "sv"): fetch_result("TAB2", "sv")}
        )
        run = await harvest(session, adapter)
        assert run.summary.failed == 1
        item = HarvestRepository(session).list_items(1)[0]
        assert item.error is not None and item.error.stage is DiagnosticStage.NORMALIZE
        assert MetadataRepository(session).get_table_by_native(PROVIDER_ID, "TAB2") is None


async def test_a_failed_discovery_fails_the_job_and_changes_nothing() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1, TAB2]))
        broken = StubAdapter(entries=[], discovery_error=UPSTREAM_DOWN)
        with pytest.raises(JobFailed) as failure:
            await harvest(session, broken)
        assert failure.value.diagnostic.stage is DiagnosticStage.DISCOVERY
        metadata = MetadataRepository(session)
        assert metadata.get_table_by_native(PROVIDER_ID, "TAB2") is not None
        assert len(metadata.search("Befolkning")) == 2


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
            entries=[TAB1, TAB2], behaviour={("TAB2", "sv"): stop_and_never_answer}
        )
        with pytest.raises(HarvestStopped):
            await harvest(session, adapter, control_factory=build)
        items = HarvestRepository(session).list_items(1)
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


async def test_the_engine_decides_the_cases_a_marker_cannot_speak_to() -> None:
    with owner() as session:
        register(session)
        # An adapter that always claims "unchanged" must not be able to strand a Table
        # that has never been accepted, or whose last attempt failed.
        stubborn = StubAdapter(entries=[TAB1], refresh={"TAB1": False})
        first = await harvest(session, stubborn)
        assert stubborn.fetched == [("TAB1", "sv")]
        assert first.summary.updated == 1
        # Now it has been accepted, so the adapter's answer is what settles it.
        stubborn.fetched.clear()
        second = await harvest(session, stubborn)
        assert stubborn.fetched == []
        assert second.summary.skipped == 1
        # force overrides it whatever the adapter says.
        third = await harvest(session, stubborn, request=HarvestRequest(language="sv", force=True))
        assert stubborn.fetched == [("TAB1", "sv")]
        assert third.summary.updated == 1


async def test_a_failed_language_is_refetched_even_when_the_adapter_says_unchanged() -> None:
    with owner() as session:
        register(session)
        await harvest(session, StubAdapter(entries=[TAB1]))
        await harvest(
            session, StubAdapter(entries=[TAB1], behaviour={("TAB1", "sv"): UPSTREAM_DOWN})
        )
        recovering = StubAdapter(entries=[TAB1], refresh={"TAB1": False})
        run = await harvest(session, recovering)
        # An outstanding failure is ours to know about; the marker says nothing about it.
        assert recovering.fetched == [("TAB1", "sv")]
        assert run.summary.updated == 1
        table = MetadataRepository(session).get_table_by_native(PROVIDER_ID, "TAB1")
        assert table is not None
        assert MetadataRepository(session).load_language_state(table.table_id)["sv"].failed is False
