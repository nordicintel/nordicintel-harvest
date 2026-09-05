"""Traversal of one claimed job: discovery, then one Table and one language at a time.

The engine owns the decisions core deliberately does not make. Core stores an adapter's
comparison marker without interpreting it, so what a marker means stays with the adapter;
what it can never override is decided here, because "this language has never been
harvested successfully" is a fact about the database, not about the upstream publisher.

Everything is sequential. One job holds one provider lock and one HTTP wrapper whose rate
limiting is per instance, so concurrency inside a job would only mean the same upstream
being asked more questions at once, with no coordination against the other processes
sharing that quota.

Two boundaries decide what a failure costs. Inside an item, an upstream or validation
failure for one language is recorded and the traversal continues, so a Table that fails in
Swedish still publishes English. Outside it, a failure means the job could not be carried
out at all. ``OwnershipLost`` belongs to neither: it says this session may no longer write,
so it passes straight through to the worker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from nordicintel_core.database import HarvestRepository, MetadataRepository
from nordicintel_core.errors import UpstreamError
from nordicintel_core.models import (
    DiagnosticStage,
    DiscoveryEntry,
    DiscoveryResult,
    DiscoveryScope,
    HarvestJob,
    ItemStatus,
    LanguageState,
    MetadataFetchResult,
    NordicIntelAdapter,
    ProviderDefinition,
)

from . import diagnostics
from .control import JobControl
from .errors import HarvestStopped, JobFailed

# Failures a Table is allowed to have. Anything else is a defect or a lost session, and
# turning those into an item failure would let a job report a full traversal it never made.
_TABLE_FAILURES = (UpstreamError, ValueError)


@dataclass(slots=True)
class HarvestSummary:
    """What one traversal did, for the worker's log and for tests."""

    updated: int = 0
    skipped: int = 0
    failed: int = 0
    restored: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)

    @property
    def items(self) -> int:
        return self.updated + self.skipped + self.failed


class HarvestEngine:
    """Runs one job to completion, or until it is asked to stop."""

    def __init__(
        self,
        *,
        queue: HarvestRepository,
        metadata: MetadataRepository,
        adapter: NordicIntelAdapter,
        job: HarvestJob,
        provider: ProviderDefinition,
        control: JobControl,
    ) -> None:
        self._queue = queue
        self._metadata = metadata
        self._adapter = adapter
        self._job = job
        self._provider = provider
        self._control = control

    async def run(self) -> HarvestSummary:
        """Traverse the job's scope.

        Returns:
            A summary of the items processed and identities reconciled.

        Raises:
            HarvestStopped: A cancellation was observed; the job stops where it is.
            JobFailed: The job could not be carried out, with the diagnostic to record.
            OwnershipLost: This session may no longer write; nothing else may be attempted.
        """
        languages = await self._resolve_languages()
        scope = self._resolve_scope(languages)
        discovery = await self._discover(scope)
        summary = HarvestSummary()
        for entry in discovery.entries:
            self._control.raise_if_stopping()
            status = await self._process(entry, languages)
            if status is ItemStatus.UPDATED:
                summary.updated += 1
            elif status is ItemStatus.SKIPPED:
                summary.skipped += 1
            else:
                summary.failed += 1
        self._reconcile(discovery, summary)
        return summary

    async def _resolve_languages(self) -> list[str]:
        try:
            languages = await self._control.guard(
                self._adapter.resolve_languages(self._job.request.languages)
            )
        except (HarvestStopped, JobFailed):
            raise
        except Exception as exc:
            raise JobFailed(
                diagnostics.diagnose(exc, stage=DiagnosticStage.DISCOVERY)
            ) from exc
        resolved = [language.strip().lower() for language in languages]
        if not resolved or any(not language for language in resolved):
            raise JobFailed(
                diagnostics.build(
                    "no_languages_resolved",
                    "The adapter resolved no usable language for this job.",
                    stage=DiagnosticStage.DISCOVERY,
                )
            )
        return list(dict.fromkeys(resolved))

    def _resolve_scope(self, languages: Sequence[str]) -> DiscoveryScope:
        """Turn the request into a scope an adapter can act on.

        A single-table request names a canonical identity, which no adapter can resolve.
        It is translated here into the upstream identifier stored beside it, so the
        adapter never sees a slug and never has to parse one.
        """
        requested = self._job.request.table_id
        if requested is None:
            return DiscoveryScope(languages=list(languages))
        table = self._metadata.get_table(requested)
        if table is None or table.provider_id != self._provider.id:
            raise JobFailed(
                diagnostics.build(
                    "table_not_found",
                    f"Table {requested!r} does not belong to provider {self._provider.id!r}.",
                    stage=DiagnosticStage.DISCOVERY,
                )
            )
        return DiscoveryScope(
            table_id=table.table_id,
            native_table_id=table.native_table_id,
            languages=list(languages),
        )

    async def _discover(self, scope: DiscoveryScope) -> DiscoveryResult:
        try:
            discovery = await self._control.guard(self._adapter.discover(scope))
        except (HarvestStopped, JobFailed):
            raise
        except Exception as exc:
            raise JobFailed(
                diagnostics.diagnose(exc, stage=DiagnosticStage.DISCOVERY)
            ) from exc
        if discovery.scope != scope:
            raise JobFailed(
                diagnostics.build(
                    "discovery_scope_mismatch",
                    "The adapter returned a discovery for a different scope.",
                    stage=DiagnosticStage.DISCOVERY,
                )
            )
        if scope.native_table_id is not None:
            native = scope.native_table_id
            wanted = [
                entry for entry in discovery.entries if entry.native_table_id == native
            ]
            if len(wanted) != 1:
                raise JobFailed(
                    diagnostics.build(
                        "table_not_discovered",
                        f"The adapter did not return table {scope.native_table_id!r}.",
                        stage=DiagnosticStage.DISCOVERY,
                    )
                )
            return discovery.model_copy(update={"entries": wanted})
        return discovery

    async def _process(self, entry: DiscoveryEntry, languages: Sequence[str]) -> ItemStatus:
        """Harvest one Table, recording the outcome as one item."""
        stored = self._metadata.get_table_by_native(self._provider.id, entry.native_table_id)
        table_id = None if stored is None else stored.table_id
        item = self._queue.begin_item(self._job.id, entry.native_table_id, table_id=table_id)
        failures: list[diagnostics.LanguageFailure] = []
        recorded: set[str] = set()
        accepted = 0
        try:
            state = {} if table_id is None else self._metadata.load_language_state(table_id)
            candidates = self._candidate_languages(entry, languages)
            selected = await self._select_languages(entry, state, candidates)
            for language in selected:
                self._control.raise_if_stopping()
                stage = DiagnosticStage.FETCH_METADATA
                try:
                    results = await self._control.guard(
                        self._adapter.fetch_metadata(entry, [language])
                    )
                    stage = DiagnosticStage.NORMALIZE
                    result = self._single_result(results, entry, language)
                    stage = DiagnosticStage.PERSIST
                    table_id = self._metadata.upsert_language(self._job.id, result)
                    accepted += 1
                except _TABLE_FAILURES as exc:
                    failure = diagnostics.language_failure(language, stage, exc)
                    failures.append(failure)
                    # A Table with no accepted metadata has no row to attach an error to,
                    # and inventing one would publish an identity for something that has
                    # never been read successfully.
                    if table_id is not None:
                        self._record_failure(table_id, failure)
                        recorded.add(language)
            if table_id is not None:
                # A later language can mint the identity the earlier failure had nowhere
                # to attach to. Recording it now is what keeps failed_languages, and the
                # retry the next run owes that language, accurate.
                for failure in failures:
                    if failure.language not in recorded:
                        self._record_failure(table_id, failure)
                for language in candidates:
                    if language not in selected and language in state:
                        self._metadata.mark_checked(self._job.id, table_id, language)
        except HarvestStopped:
            # A lost session may not write at all; recovery closes the item instead.
            if not self._control.ownership_lost:
                self._queue.finish_item(
                    self._job.id,
                    item.id,
                    ItemStatus.FAILED,
                    error=diagnostics.interrupted(self._control.reason),
                    table_id=table_id,
                )
            raise
        if failures:
            status = ItemStatus.FAILED
            error = diagnostics.item_failure(diagnostics.limit_language_details(failures))
        else:
            status = ItemStatus.UPDATED if accepted else ItemStatus.SKIPPED
            error = None
        self._queue.finish_item(self._job.id, item.id, status, error=error, table_id=table_id)
        return status

    def _record_failure(self, table_id: str, failure: diagnostics.LanguageFailure) -> None:
        self._metadata.record_failure(
            self._job.id,
            table_id,
            diagnostics.build(
                failure.code,
                failure.message,
                stage=failure.stage,
                details={"language": failure.language},
            ),
            language=failure.language,
        )

    def _candidate_languages(
        self, entry: DiscoveryEntry, languages: Sequence[str]
    ) -> list[str]:
        """The languages this Table could be fetched in at all.

        ``available_languages`` is a statement about existence, not about freshness: a
        Table that was never published in a language cannot be fetched in it, and asking
        is an upstream error rather than an empty answer. That makes this bound absolute.
        Everything below chooses within it. An adapter that says nothing leaves the
        requested languages standing.
        """
        if entry.available_languages is None:
            return list(languages)
        available = set(entry.available_languages)
        return [language for language in languages if language in available]

    async def _select_languages(
        self,
        entry: DiscoveryEntry,
        state: Mapping[str, LanguageState],
        candidates: Sequence[str],
    ) -> list[str]:
        """Decide which of the candidate languages this Table is fetched in.

        The adapter decides what its own markers mean. It cannot decide the cases that do
        not depend on a marker at all: a language with no successful harvest has nothing
        to compare against, a language holding an outstanding failure has to be retried,
        and ``force`` was requested precisely to bypass the comparison. Those are added
        whatever the adapter answers, and the result is ordered by the resolved languages
        so a partial run always covers them in the same sequence.
        """
        chosen = await self._control.guard(
            self._adapter.languages_to_refresh(
                entry, dict(state), list(candidates), force=self._job.request.force
            )
        )
        wanted = {language.strip().lower() for language in chosen}
        for language in candidates:
            stored = state.get(language)
            unusable = stored is None or stored.failed or stored.last_harvested_at is None
            if unusable or self._job.request.force:
                wanted.add(language)
        return [language for language in candidates if language in wanted]

    def _single_result(
        self,
        results: Sequence[MetadataFetchResult],
        entry: DiscoveryEntry,
        language: str,
    ) -> MetadataFetchResult:
        """Accept exactly the one result that was asked for.

        One language is requested per call because the adapter contract has no way to
        report a partial failure: a list of successes cannot say that Swedish failed while
        English succeeded. With one language per call, an empty or mismatched list is
        unambiguously a failure of that language rather than a silent skip.
        """
        if len(results) != 1:
            raise ValueError(
                f"Expected one metadata result for {language!r}, received {len(results)}"
            )
        result = results[0]
        if result.provider_id != self._provider.id:
            raise ValueError("The adapter returned metadata for a different provider")
        if result.native_table_id != entry.native_table_id:
            raise ValueError("The adapter returned metadata for a different table")
        if result.metadata.language != language:
            raise ValueError("The adapter returned metadata for a different language")
        return result

    def _reconcile(self, discovery: DiscoveryResult, summary: HarvestSummary) -> None:
        """Make stored absence agree with the inventory, when the inventory can decide it.

        Only a complete provider-wide enumeration says anything about absence. A
        single-table scope and a partial listing both say nothing, so neither retires
        anything and neither restores anything.
        """
        if discovery.scope.table_id is not None or not discovery.authoritative:
            return
        self._control.raise_if_stopping()
        reconciled = self._metadata.reconcile_inventory(
            self._job.id, self._provider.id, discovery
        )
        summary.restored = reconciled.restored
        summary.retired = reconciled.retired
