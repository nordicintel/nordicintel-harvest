"""Traversal of one claimed job: discovery, then one Table at a time, in one language.

A job is one Provider in one language. That is not a convenience: upstream catalogues are
published per language, and a Table that exists in Swedish and not in English is absent
from the English catalogue rather than empty in it. Scoping the run to a language means
discovery answers "which Tables can be fetched in this language" as a matter of fact,
instead of the engine having to infer it per Table from some signal an adapter invented.

The engine owns the decisions core deliberately does not make. Core stores an adapter's
comparison marker without interpreting it, so what a marker means stays with the adapter.
What core does own is whether a language has ever been accepted, and the engine passes
that in rather than asking the adapter to remember it.

Everything is sequential. One job holds one provider lock and one HTTP wrapper whose rate
limiting is per instance, so concurrency inside a job would only mean the same upstream
being asked more questions at once, with no coordination against the other processes
sharing that quota.

Two boundaries decide what a failure costs. Inside an item, an upstream or validation
failure is recorded and the traversal continues, so one broken Table does not end a run.
Outside it, a failure means the job could not be carried out at all. ``OwnershipLost``
belongs to neither: it says this session may no longer write, so it passes straight
through to the worker.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    language: str
    updated: int = 0
    skipped: int = 0
    failed: int = 0

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
        self._language = job.request.language

    async def run(self) -> HarvestSummary:
        """Traverse the job's scope.

        Returns:
            A summary of the items processed.

        Raises:
            HarvestStopped: A cancellation was observed; the job stops where it is.
            JobFailed: The job could not be carried out, with the diagnostic to record.
            OwnershipLost: This session may no longer write; nothing else may be attempted.
        """
        await self._check_language()
        scope = self._resolve_scope()
        discovery = await self._discover(scope)
        summary = HarvestSummary(language=self._language)
        for entry in discovery.entries:
            self._control.raise_if_stopping()
            status = await self._process(entry)
            if status is ItemStatus.UPDATED:
                summary.updated += 1
            elif status is ItemStatus.SKIPPED:
                summary.skipped += 1
            else:
                summary.failed += 1
        return summary

    async def _check_language(self) -> None:
        """Refuse a language this Provider does not publish, before touching a Table.

        Upstream answers a request for a language it does not serve with an error per
        Table, which would otherwise arrive as thousands of identical item failures
        rather than as one statement about the job.
        """
        try:
            supported = await self._control.guard(self._adapter.supported_languages())
        except (HarvestStopped, JobFailed):
            raise
        except Exception as exc:
            raise JobFailed(diagnostics.diagnose(exc, stage=DiagnosticStage.DISCOVERY)) from exc
        available = {language.strip().lower() for language in supported}
        if self._language not in available:
            raise JobFailed(
                diagnostics.build(
                    "language_not_published",
                    f"Provider {self._provider.id!r} does not publish {self._language!r}.",
                    stage=DiagnosticStage.DISCOVERY,
                    details={"supported": sorted(available)},
                )
            )

    def _resolve_scope(self) -> DiscoveryScope:
        """Turn the request into a scope an adapter can act on.

        A single-table request names a canonical identity, which no adapter can resolve.
        It is translated here into the upstream identifier stored beside it, so the
        adapter never sees a slug and never has to parse one.
        """
        requested = self._job.request.table_id
        if requested is None:
            return DiscoveryScope(language=self._language)
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
            language=self._language,
            table_id=table.table_id,
            native_table_id=table.native_table_id,
        )

    async def _discover(self, scope: DiscoveryScope) -> DiscoveryResult:
        try:
            discovery = await self._control.guard(self._adapter.discover(scope))
        except (HarvestStopped, JobFailed):
            raise
        except Exception as exc:
            raise JobFailed(diagnostics.diagnose(exc, stage=DiagnosticStage.DISCOVERY)) from exc
        if discovery.scope != scope:
            raise JobFailed(
                diagnostics.build(
                    "discovery_scope_mismatch",
                    "The adapter returned a discovery for a different scope.",
                    stage=DiagnosticStage.DISCOVERY,
                )
            )
        native = scope.native_table_id
        if native is not None:
            wanted = [entry for entry in discovery.entries if entry.native_table_id == native]
            if len(wanted) != 1:
                raise JobFailed(
                    diagnostics.build(
                        "table_not_discovered",
                        f"The adapter did not return table {native!r}.",
                        stage=DiagnosticStage.DISCOVERY,
                    )
                )
            return discovery.model_copy(update={"entries": wanted})
        return discovery

    async def _process(self, entry: DiscoveryEntry) -> ItemStatus:
        """Harvest one Table in this job's language, recording the outcome as one item."""
        stored = self._metadata.get_table_by_native(self._provider.id, entry.native_table_id)
        table_id = None if stored is None else stored.table_id
        item = self._queue.begin_item(self._job.id, entry.native_table_id, table_id=table_id)
        stage = DiagnosticStage.FETCH_METADATA
        try:
            state = (
                None
                if table_id is None
                else self._metadata.load_language_state(table_id).get(self._language)
            )
            if not await self._needs_fetch(entry, state):
                # Unchanged. Record that it was looked at, so "current" and "not checked
                # since the last harvest" stay distinguishable.
                if table_id is not None and state is not None:
                    self._metadata.mark_checked(self._job.id, table_id, self._language)
                self._queue.finish_item(
                    self._job.id, item.id, ItemStatus.SKIPPED, table_id=table_id
                )
                return ItemStatus.SKIPPED

            result = await self._control.guard(
                self._adapter.fetch_metadata(entry, self._language)
            )
            stage = DiagnosticStage.NORMALIZE
            self._validate(result, entry)
            stage = DiagnosticStage.PERSIST
            table_id = self._metadata.upsert_language(self._job.id, result)
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
        except _TABLE_FAILURES as exc:
            error = diagnostics.diagnose(exc, stage=stage, details={"language": self._language})
            # A Table with no accepted metadata has no row to attach an error to, and
            # inventing one would publish an identity for something that has never been
            # read successfully. The item carries the failure either way.
            if table_id is not None:
                self._metadata.record_failure(
                    self._job.id, table_id, error, language=self._language
                )
            self._queue.finish_item(
                self._job.id, item.id, ItemStatus.FAILED, error=error, table_id=table_id
            )
            return ItemStatus.FAILED
        self._queue.finish_item(self._job.id, item.id, ItemStatus.UPDATED, table_id=table_id)
        return ItemStatus.UPDATED

    async def _needs_fetch(self, entry: DiscoveryEntry, state: LanguageState | None) -> bool:
        """Decide whether this Table has to be fetched, asking the adapter only if needed.

        Three cases do not depend on what a marker means, and so are not the adapter's to
        judge. Two of them are facts about our own database — this language has never been
        accepted, or its last attempt failed, so there is nothing to compare a marker
        against. The third is ``force``, which was requested precisely to bypass the
        comparison. An adapter that answered "unchanged" to any of them would leave a
        Table permanently unharvested, and it would be unharvested for a reason no
        diagnostic records.
        """
        if self._job.request.force or state is None:
            return True
        if state.failed or state.last_harvested_at is None:
            return True
        return await self._control.guard(
            self._adapter.should_refresh(entry, state, force=False)
        )

    def _validate(self, result: MetadataFetchResult, entry: DiscoveryEntry) -> None:
        """Accept exactly the one result that was asked for.

        ``upsert_language`` checks that the job owns the Provider, but not that this is
        the Table the engine asked about: a result for the wrong Table of the right
        Provider would otherwise be written under the wrong identity.
        """
        if result.provider_id != self._provider.id:
            raise ValueError("The adapter returned metadata for a different provider")
        if result.native_table_id != entry.native_table_id:
            raise ValueError("The adapter returned metadata for a different table")
        if result.metadata.language != self._language:
            raise ValueError("The adapter returned metadata for a different language")
