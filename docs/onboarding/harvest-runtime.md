# Harvest runtime onboarding

This note is a source-backed orientation to how the harvest runtime works today in `nordicintel-harvest`.

## Scope and evidence

This report investigates `nordicintel-harvest` first, with only narrow interface cross-references to `../nordicintel-core` and `../nordicintel-adapter-pxweb2` where the harvest runtime depends on them.

**How to read the claims in this document**

- **Fact** = directly supported by code, docs, or tests read during this research.
- **Inference** = a conclusion derived from multiple facts, or from the absence of a feature/test/doc. These are useful, but should be treated as hypotheses to confirm when changing behavior.

Primary sources used:

- `README.md`
- `docs/operations.md`
- `pyproject.toml`
- `src/nordicintel_harvest/bootstrap.py`
- `src/nordicintel_harvest/worker.py`
- `src/nordicintel_harvest/engine.py`
- `src/nordicintel_harvest/scheduler.py`
- `src/nordicintel_harvest/control.py`
- `src/nordicintel_harvest/registry.py`
- `src/nordicintel_harvest/settings.py`
- `src/nordicintel_harvest/secrets.py`
- `src/nordicintel_harvest/diagnostics.py`
- `src/nordicintel_harvest/errors.py`
- `tests/test_configuration.py`
- `tests/integration/test_bootstrap.py`
- `tests/integration/test_worker.py`
- `tests/engine/test_incremental.py`
- `tests/support.py`
- `tests/conftest.py`
- `../nordicintel-core/docs/adapters.md`
- `../nordicintel-core/src/nordicintel_core/models/adapters.py`
- `../nordicintel-adapter-pxweb2/pyproject.toml`
- `../nordicintel-adapter-pxweb2/src/nordicintel_adapter_pxweb2/factory.py`

## Runtime roles at a glance

| Component | What it does | Why it exists |
|---|---|---|
| `nordicintel-bootstrap` | Operator CLI for providers, schedules, queue inspection, and manual enqueue/cancel. See `src/nordicintel_harvest/bootstrap.py`. | It is the temporary control plane until an API exists, but intentionally stores no separate state. |
| `nordicintel-scheduler` | Recovers stale jobs, then enqueues due schedules. See `src/nordicintel_harvest/scheduler.py`. | It centralizes schedule admission and stale-job recovery in exactly one singleton process. |
| `nordicintel-worker` | Claims one job, builds its adapter + HTTP client + secrets, runs the harvest, finalizes the job, and releases the provider. See `src/nordicintel_harvest/worker.py`. | It is the execution loop that turns queued work into harvested metadata. |
| `HarvestEngine` | Traverses one claimed job: validate language, discover scope, decide whether each table needs fetching, fetch/validate/persist, and record item results. See `src/nordicintel_harvest/engine.py`. | It contains the harvest decisions that core deliberately does not own. |
| `JobControl` | Heartbeat loop, stop observation, and cancellation bridge. See `src/nordicintel_harvest/control.py`. | It keeps liveness and cancellation semantics in one place. |

### Facts

- `README.md` and `docs/operations.md` both present the system as three operational processes: bootstrap, scheduler, and worker.
- `bootstrap.py` uses the pooled API engine (`create_api_engine`) for short administrative transactions, while `worker.py` and `scheduler.py` use the owner engine (`create_owner_engine`) because they hold session-scoped ownership and advisory locks across calls.
- `pyproject.toml` exposes exactly three console scripts: `nordicintel-worker`, `nordicintel-scheduler`, and `nordicintel-bootstrap`.

### Inference

- The runtime is intentionally split so that "control-plane" actions (configuring providers, schedules, manual queue operations) stay cheap and stateless, while long-running job ownership stays on dedicated physical database sessions.

## Why the job model is per provider and per language

### Facts

- A harvest job names one Provider and one language. This is stated in `README.md`, `docs/operations.md`, and repeated in `src/nordicintel_harvest/engine.py`.
- The stated reason is upstream reality: catalogues are published per language, and the language-specific inventories differ. `README.md` uses SCB as the example: Swedish and English catalogues are different sizes.
- `HarvestEngine.run()` only traverses `job.request.language`, and `HarvestEngine._check_language()` rejects unsupported languages before touching any table.
- `tests/engine/test_incremental.py::test_each_language_is_its_own_run_over_its_own_catalogue` proves the model: Swedish and English runs may discover different table sets, and English is never fetched for a table absent from the English catalogue.
- `docs/operations.md` states that schedules are per provider and per language, so Swedish and English runs are queued independently.
- `tests/integration/test_worker.py::test_the_scheduler_enqueues_each_due_language_without_stacking` proves that due schedules for two languages enqueue two distinct jobs.

### Why this exists

### Facts

- `src/nordicintel_harvest/engine.py` explicitly says this is not a convenience. By scoping discovery to one language, the system treats “does this table exist in this language?” as a fact about the upstream listing, rather than forcing adapters or the host to invent a per-table absence convention.

### Inference

- This model avoids a class of ambiguity that would otherwise leak into adapters and persistence: a missing English variant is represented as “not discovered in the English catalogue”, not as a failed fetch, null payload, or synthetic unavailable record.

## Runtime lifecycle: from queued job to finalization

The lifecycle below is a direct synthesis of `src/nordicintel_harvest/worker.py`, `src/nordicintel_harvest/engine.py`, `src/nordicintel_harvest/control.py`, `src/nordicintel_harvest/scheduler.py`, and the integration tests.

1. **Admission / enqueue**
   - **Fact:** Manual jobs are created by `nordicintel-bootstrap harvest enqueue` in `bootstrap.py`.
   - **Fact:** Scheduled jobs are created by `ScheduleRepository.enqueue_due()` inside `scheduler.py`.
   - **Fact:** The scheduler does not stack repeated missed runs for a busy provider; `docs/operations.md` and `scheduler.py` say the due schedule is advanced instead. `tests/integration/test_worker.py::test_the_scheduler_enqueues_each_due_language_without_stacking` proves this.

2. **Claim**
   - **Fact:** A worker opens one owner session and calls `HarvestRepository.claim()` in `Worker.run_once()`.
   - **Fact:** If no eligible job exists, the worker sleeps for `queue_poll_seconds` and tries again.
   - **Fact:** The worker claims at most one job per pass.

3. **Preparation**
   - **Fact:** `Worker._prepare()` loads the provider definition from the database, resolves the provider’s adapter type through `AdapterRegistry`, resolves secrets from environment variables, creates one `httpx.AsyncClient`, wraps it in core’s `HttpClient`, and asks the adapter factory to create a `NordicIntelAdapter`.
   - **Fact:** Any failure in this phase becomes a job-level failure with a discovery-stage diagnostic.

4. **Heartbeat and stop observation**
   - **Fact:** `Worker._run_job()` creates a `JobControl`, whose background task repeatedly calls `HarvestRepository.heartbeat(job_id)`.
   - **Fact:** That heartbeat both refreshes liveness and returns whether a stop has been requested; this is called out explicitly in `control.py` and `docs/operations.md`.
   - **Fact:** If the heartbeat detects ownership loss or another session failure, `JobControl` marks `ownership_lost`, stops the run, and prevents further writes from this session.

5. **Language validation and scope resolution**
   - **Fact:** `HarvestEngine._check_language()` asks the adapter for `supported_languages()` and fails the whole job once if the requested language is unsupported.
   - **Fact:** `HarvestEngine._resolve_scope()` turns a single-table request from canonical `table_id` into the stored upstream `native_table_id`, so the adapter never has to parse NordicIntel canonical IDs.

6. **Discovery**
   - **Fact:** `HarvestEngine._discover()` calls `adapter.discover(scope)` under `JobControl.guard()`.
   - **Fact:** If a single-table job was requested, the engine requires the discovered entries to contain exactly that upstream table; otherwise the job fails.

7. **Per-table processing**
   - **Fact:** For each discovered `DiscoveryEntry`, the engine opens a harvest item with `begin_item()`.
   - **Fact:** It then decides whether a fetch is needed using `_needs_fetch()`.
   - **Fact:** Three cases force a fetch without trusting the adapter’s marker semantics: `force=True`, no accepted language state yet, or the last attempt for that language failed / never harvested. This behavior is described in `engine.py` and proven in `tests/engine/test_incremental.py`.
   - **Fact:** If the table is unchanged, the engine marks it checked (when a prior accepted state exists), finishes the item as `skipped`, and moves on.
   - **Fact:** If a fetch is needed, the engine calls `adapter.fetch_metadata(entry, language)`, validates the returned provider/table/language triple, persists it with `MetadataRepository.upsert_language()`, and finishes the item as `updated`.
   - **Fact:** Allowed table-level failures (`UpstreamError`, `ValueError`) are converted into failed items while the job continues over later tables.
   - **Fact:** Discovery/configuration failures are not item failures; they become job failures via `JobFailed`.

8. **Cancellation or shutdown during work**
   - **Fact:** `JobControl.guard()` cancels in-flight async work when a stop is observed and raises `HarvestStopped`.
   - **Fact:** If cancellation arrives during a table fetch, the engine records the interrupted item as failed with an interrupted-stage diagnostic unless ownership was already lost.
   - **Fact:** `tests/engine/test_incremental.py::test_cancellation_stops_the_traversal_and_closes_the_running_item` proves that already-finished items stay committed while the interrupted in-flight item is closed as failed.

9. **Finalization**
   - **Fact:** `Worker._run_job()` translates the run outcome into one terminal job status: `completed`, `failed`, or `cancelled`, then calls `finish_job()`.
   - **Fact:** If ownership is lost before finalization, the worker must not finalize, because another process may already be recovering the job.
   - **Fact:** Whether success or failure occurs, `Worker.run_once()` attempts to release the provider lock in `finally`.

10. **Recovery if the owner disappears**
   - **Fact:** The scheduler periodically runs `recover_stale(stale_after_seconds)`.
   - **Fact:** `tests/integration/test_worker.py::test_a_job_whose_owner_disappears_is_recovered_not_resumed` proves the intended result: the stale job becomes `failed` with code `worker_abandoned`, items are closed, and no retry job is inserted automatically.

## Adapter loading and interface boundaries

### Facts

- Providers store an `adapter_type` string in the database.
- `src/nordicintel_harvest/registry.py` is the only place where that string becomes code.
- `AdapterRegistry.from_entry_points()` only considers installed entry points in the `nordicintel.adapters` group, and can narrow them further using the `NORDICINTEL_ADAPTERS` allowlist.
- A configured allowlisted adapter that is not actually installed causes startup failure, not a delayed runtime surprise. This is implemented in `registry.py` and tested in `tests/test_configuration.py`.
- A registered object may be either an instance or a class; classes are instantiated once at startup. Broken adapters therefore fail process startup instead of failing jobs one at a time.
- The structural adapter protocols live in `../nordicintel-core/src/nordicintel_core/models/adapters.py`, while the higher-level interface guidance lives in `../nordicintel-core/docs/adapters.md`.
- The current harvest package declares the `pxweb2` optional dependency in `pyproject.toml`, and the sibling adapter package registers itself under the `pxweb2` name in `../nordicintel-adapter-pxweb2/pyproject.toml`.
- The adapter factory entry point in `../nordicintel-adapter-pxweb2/src/nordicintel_adapter_pxweb2/factory.py` receives `provider`, `secrets`, and shared async HTTP access, and returns a job-scoped adapter instance.

### Inference

- The entry-point-based registry is a deliberate containment boundary: the database may choose among installed adapter families, but it may not trigger arbitrary imports.

## Secrets handling

### Facts

- Secrets are not stored in the database. `docs/operations.md` and `src/nordicintel_harvest/secrets.py` both make this explicit.
- A provider stores `secret_refs`, mapping adapter-facing secret names to environment variable names.
- `resolve_secrets()` resolves all declared references against the process environment and fails atomically if any reference is missing or blank.
- Error messages name only the adapter-facing name and environment variable reference (for example `api_key -> SCB_API_KEY`) and never include secret values. This is implemented in `secrets.py` and tested in `tests/test_configuration.py` and `tests/integration/test_worker.py`.
- Secret resolution happens during `Worker._prepare()`, before adapter creation.

### Inference

- Because secret resolution happens per claimed job, operator mistakes surface as job failures for the affected provider rather than as global startup failures, unless the adapter itself fails to load.

## Rate limiting and request cadence

### Facts

- The process-wide default minimum request spacing comes from `NORDICINTEL_REQUEST_INTERVAL_SECONDS`, loaded in `src/nordicintel_harvest/settings.py`.
- A provider may override that spacing with `provider.config.request_interval_seconds`; `Worker._request_interval()` reads and validates it in `src/nordicintel_harvest/worker.py`.
- Fractional JSON values may round-trip from JSONB as `Decimal`; `Worker._request_interval()` explicitly accepts `Decimal`, and `tests/test_configuration.py` proves this path.
- The worker creates the HTTP wrapper once per job attempt and passes the chosen minimum interval to core’s `HttpClient`.
- `src/nordicintel_harvest/engine.py` states that traversal is sequential inside one job. Combined with the provider lock, that means one provider is never queried concurrently by multiple workers in this deployment model.

### Inference

- Rate limiting is intentionally enforced at the runtime boundary, not delegated to adapter authors, so different adapters still inherit the same host-side pacing semantics.

## Heartbeat, ownership, and cancellation semantics

### Facts

- Ownership is defined by a physical database backend plus session-scoped advisory locks. This is stated repeatedly in `src/nordicintel_harvest/worker.py`, `src/nordicintel_harvest/scheduler.py`, and `docs/operations.md`.
- Because ownership is session-bound, the worker does not reconnect mid-attempt. Losing the session means losing authority to speak for the job.
- `JobControl` runs its heartbeat task on the same event loop/session owner thread, not on a worker thread. `control.py` explains why: core repository calls are synchronous and ownership depends on the same physical backend.
- `Settings.__post_init__()` requires at least three heartbeats to fit inside the stale window, and requires the database statement timeout to be lower than the stale window. `tests/test_configuration.py` covers both validations.
- On process shutdown, `JobControl` first writes cancellation intent with `queue.cancel(job_id)`, then stops the in-flight work. `docs/operations.md` and `tests/integration/test_worker.py::test_shutting_the_worker_down_cancels_the_job_it_is_running` show why: a dying process should be recovered as `cancelled`, not as mysteriously abandoned work.
- A second scheduler is expected to fail startup with exit code 3 because the first holds the singleton advisory lock. This is documented in `docs/operations.md` and tested in `tests/integration/test_worker.py::test_a_second_scheduler_refuses_to_start`.
- Provider disabling without `--cascade` blocks new admission and asks the running job to stop on its next heartbeat; with `--cascade`, queued jobs are also cancelled. This is documented in `docs/operations.md` and wired through `bootstrap.py`.

### Inference

- The runtime’s safety model depends more on database session semantics than on Python process semantics. Changing connection pooling, retry, or threading behavior is therefore a high-risk architectural change even if the Python code still “looks” correct.

## Highest-value docs and tests to read first

If you are new to this repo, this is the shortest reading path that reaches the real runtime model quickly.

1. `docs/operations.md`
   - Best operator-oriented overview of processes, configuration, shutdown, recovery, and expected outcomes.
2. `README.md`
   - Best short framing of repository boundaries: core owns schema/repositories/protocols; harvest owns process behavior and runtime meaning.
3. `src/nordicintel_harvest/worker.py`
   - Best place to understand the attempt lifecycle, preparation, finalization, and why ownership loss is special.
4. `src/nordicintel_harvest/engine.py`
   - Best place to understand what “harvesting” actually means table by table.
5. `src/nordicintel_harvest/control.py`
   - Best place to understand heartbeat, cooperative stop, and shutdown semantics.
6. `tests/integration/test_worker.py`
   - Highest-value proof suite for ownership, cancellation, recovery, scheduler singleton behavior, and provider serialization.
7. `tests/engine/test_incremental.py`
   - Highest-value proof suite for per-language catalogues, incremental refresh, forced refresh, discovery failures, interrupted items, and missing-table behavior.
8. `tests/support.py`
   - Best test fixture file for understanding the adapter contract in runnable form.
9. `tests/integration/test_bootstrap.py`
   - Best proof that the operator CLI is only a thin wrapper over repositories/admission rules.
10. `tests/test_configuration.py`
   - Best compact reference for startup validation, diagnostics safety, secret resolution, and rate-limit config parsing.

## Risky or subtle behaviors worth keeping in mind

### Facts

- **Physical backend ownership is non-negotiable.** `docs/operations.md` explicitly warns that transaction poolers break both `owner_backend_pid` and session-scoped advisory locks.
- **No reconnect during an attempt.** In `worker.py`, a lost connection ends the attempt; recovery decides what happens next.
- **Recovery does not insert a retry.** Stale jobs are closed; the next scheduled run or manual enqueue creates new work.
- **A busy provider causes missed schedule ticks to be skipped, not accumulated.** This avoids queue explosions but changes what “every N seconds” means under sustained slowness.
- **Table absence is passive.** `tests/engine/test_incremental.py::test_a_table_missing_from_a_later_run_keeps_everything_it_had` proves that when a table disappears from discovery, nothing deletes or tombstones it.
- **Table-level failure and job-level failure are intentionally different.** Item failures are survivable; discovery/configuration failures are not. See `src/nordicintel_harvest/errors.py` and `src/nordicintel_harvest/engine.py`.
- **Diagnostics are intentionally redacted and size-limited.** `src/nordicintel_harvest/diagnostics.py` trims unknown exception detail and enforces core’s 16 KiB limit.
- **Skipped tables are still marked checked.** This preserves the difference between “unchanged and examined” vs. “not revisited yet”.

### Inference

- The runtime strongly favors correctness and operator legibility over maximal throughput. Sequential traversal, one-provider serialization, explicit recovery, and conservative cancellation behavior all point in that direction.

## Documentation gaps and open questions

### Facts

- Before this note, the repo had `README.md` and `docs/operations.md`, but no dedicated onboarding document for the runtime lifecycle.
- The tests contain several important behavioral guarantees that are only partially summarized in prose docs, especially around missing tables, interrupted items, and the engine’s override of adapter refresh decisions.

### Inference

- The following are good candidates for future documentation or ADRs:
  1. A compact architecture diagram showing `bootstrap` / `scheduler` / `worker` / core repositories / adapter boundary.
  2. A short note explaining why “skipped schedule tick” is preferred over stacked backlog for busy providers.
  3. A policy document for what should happen when a previously harvested table disappears upstream, since the current behavior is intentionally “do nothing”.
  4. A dedicated operator note on database/pooler requirements, because this is the easiest deployment mistake to make and the hardest one to reason about after the fact.
  5. A small sequence diagram for shutdown and cancellation, since that logic spans `worker.py`, `control.py`, and `docs/operations.md`.

## Bottom line

### Facts

- `nordicintel-harvest` is a thin but opinionated runtime over core’s schema, repositories, and adapter protocol.
- The scheduler owns stale-job recovery and due-schedule admission.
- The worker owns one claimed job at a time, from setup through finalization.
- The engine owns language validation, discovery, incremental-refresh decisions, and per-item outcome recording.
- The whole design depends on session-scoped ownership, per-language discovery, and cooperative cancellation.

### Inference

- If you are changing behavior in this repo, the safest path is to treat `tests/integration/test_worker.py` and `tests/engine/test_incremental.py` as executable specifications, not just regression tests. They are where the runtime’s actual contract is clearest.
