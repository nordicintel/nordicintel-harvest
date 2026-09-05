# Operations

## What runs

| Process | Count | What it does |
|---|---|---|
| `nordicintel-scheduler` | Exactly one | Recovers abandoned jobs, enqueues due schedules. |
| `nordicintel-worker` | One or more | Claims one job at a time and runs it. |
| Migration task | Once per release | `python -m nordicintel_core.database migrate upgrade head` |

Start with one worker. Add the second only after the ownership tests pass against the
deployed database, because that is what proves two workers cannot execute one Provider
concurrently.

A second scheduler is not a redundancy: it exits with status 3 because the first holds the
singleton advisory lock. Run one, and let the supervisor restart it.

## Configuration

Every variable is read once at startup from the process environment.

| Variable | Default | Meaning |
|---|---|---|
| `NORDICINTEL_DATABASE_URL` | required | PostgreSQL URL. Must be a direct, session-preserving endpoint. |
| `NORDICINTEL_ADAPTERS` | every installed | Comma-separated allowlist of adapter types this process may run. |
| `NORDICINTEL_HEARTBEAT_SECONDS` | `5` | Liveness cadence, and how promptly a cancellation is noticed. |
| `NORDICINTEL_STALE_AFTER_SECONDS` | `180` | Heartbeat age at which recovery may consider a job abandoned. |
| `NORDICINTEL_QUEUE_POLL_SECONDS` | `2` | Wait between claim attempts when the queue is empty. |
| `NORDICINTEL_SCHEDULER_POLL_SECONDS` | `15` | Scheduler tick. |
| `NORDICINTEL_SHUTDOWN_BUDGET_SECONDS` | `30` | How long a signalled worker may spend finishing its job. |
| `NORDICINTEL_HTTP_TIMEOUT_SECONDS` | `30` | Per-request upstream timeout. |
| `NORDICINTEL_REQUEST_INTERVAL_SECONDS` | `0` | Default minimum spacing between requests, per running job. |
| `NORDICINTEL_STATEMENT_TIMEOUT_SECONDS` | `30` | `statement_timeout` on every connection this process opens. |
| `NORDICINTEL_LOG_LEVEL` | `INFO` | Standard logging level. |

Startup refuses a configuration whose intervals contradict each other: at least three
heartbeats must fit inside the stale window, and a statement may not outlive it. Otherwise
a worker that is merely slow gets its job taken away while it is still holding it.

There is one heartbeat interval rather than a separate cancellation poll, because
`HarvestRepository.heartbeat` is the single call that both refreshes liveness and reports
whether a stop was requested. Lowering it makes cancellation more prompt; raising it makes
a healthy worker look dead for longer. Adjust `stale_after_seconds` with it.

The database endpoint must not be a transaction pooler. Ownership is a physical backend:
`harvest_job.owner_backend_pid` and session-scoped advisory locks are both silently lost if
something hands the process a different connection.

An upstream quota belongs to the provider, not to the process, so a Provider may state
its own spacing and it wins over the default:

```json
{ "config": { "request_interval_seconds": 0.34 } }
```

SCB publishes 30 calls per 10 seconds, which is where that 0.34 comes from. The spacing is
per running job, and a job holds its provider lock for its whole run, so one provider is
never being asked more than one question at a time by this deployment.

Secrets are never stored in the database. A Provider's `secret_refs` maps the name the
adapter asks for to the name of an environment variable the deployment supplies:

```json
{ "secret_refs": { "api_key": "SCB_API_KEY" } }
```

A missing reference fails that Provider's job with a diagnostic naming the reference and
never the value.

## Installing an adapter

Adapters are installed as extras, one per adapter family a deployment serves:

```bash
pip install "nordicintel-harvest[pxweb2]"
```

An adapter package registers its `AdapterFactory` under the name Providers use as their
`adapter_type`:

```toml
[project.entry-points."nordicintel.adapters"]
pxweb2 = "nordicintel_adapter_pxweb2:factory"
```

The registered object may be an instance or a class; a class is instantiated once at
startup, so a broken adapter fails the process rather than one job at a time.

```bash
nordicintel-bootstrap adapters     # what this process can actually run
```

An empty list here with a configured Provider is the usual cause of jobs failing with
`configuration_invalid`.

## A first run

```bash
export NORDICINTEL_DATABASE_URL=postgresql://user:pass@host/nordicintel

python -m nordicintel_core.database migrate upgrade head
nordicintel-bootstrap adapters                              # pxweb2 should be listed
nordicintel-bootstrap provider upsert providers/scb.json
nordicintel-bootstrap harvest enqueue scb sv
nordicintel-worker
```

A run is one Provider in one language, so the language is an argument and not a flag.
Harvesting SCB in both languages is two jobs, and they are genuinely different work: the
Swedish catalogue has 5,253 tables and the English one 3,315, because a table never
published in English is absent from the English catalogue rather than empty in it.

`providers/scb.json` in this repository is a working Statistics Sweden definition. Nothing
in it lists tables: what a provider publishes is discovered every run, and the only
statement of what exists is the provider's own catalogue listing.

A first full SCB run is around an hour per language at the configured request spacing.
It is safe to interrupt — the worker finalizes the job as cancelled and keeps everything
already accepted, and the next run continues from there.

Then look at what happened:

```bash
nordicintel-bootstrap jobs list --provider-id scb --language sv
nordicintel-bootstrap jobs items 1 --status failed
nordicintel-bootstrap tables search "befolkning" --language sv
nordicintel-bootstrap tables show scb-tab1 --language sv
```

To schedule it instead, give each language its own schedule and run
`nordicintel-scheduler`:

```bash
nordicintel-bootstrap schedule set scb sv --every-seconds 86400 --start-now
nordicintel-bootstrap schedule set scb en --every-seconds 86400 --start-now
```

They run on their own intervals and neither can starve the other: the scheduler treats a
Provider as busy per language, so one being queued never suppresses the other. Execution
is still serialized by the provider lock, so the two simply queue behind each other.

An interval schedule has no natural first run, so the first timestamp is explicit:
`--start-now`, `--at <ISO timestamp>`, or, when updating an existing schedule, neither,
which keeps the timestamp it already had.

## Reading an outcome

| Job status | What it means |
|---|---|
| `completed` | The scope was fully traversed. Individual items may still have failed. |
| `failed` | The job could not be carried out: discovery, configuration, or a defect. |
| `cancelled` | A stop was requested and observed. Work already committed is kept. |

| Item status | What it means |
|---|---|
| `updated` | The Table's metadata was fetched and accepted in this job's language. |
| `skipped` | It was already current, so nothing was fetched. |
| `failed` | It could not be harvested. Other Tables in the same run are unaffected. |

A failed item's diagnostic names its stage and carries the language in `details`. A failed
job carries one diagnostic and no items are left running. A job asking for a language its
Provider does not publish fails once, before any Table is touched, rather than once per
Table.

`worker_abandoned` on a job means recovery closed it: the worker's session ended without
finalizing. Recovery inserts no retry. The next scheduled run picks the work up, or you
enqueue one.

## Stopping

`SIGTERM` or `SIGINT` asks the worker to stop. It records the cancellation in the database
first, so a process that dies during shutdown is recovered as cancelled rather than as
abandoned, then abandons the upstream request it is waiting on and finalizes the job.

Past `NORDICINTEL_SHUTDOWN_BUDGET_SECONDS` the attempt is cancelled outright and the job
is left for the scheduler's recovery. Set the supervisor's own grace period above this
value, or the process is killed before it can finalize anything.

## Pausing a Provider

```bash
nordicintel-bootstrap provider disable scb --cascade
```

Without `--cascade` this stops admission and makes the running job's next heartbeat read
as a stop request, but leaves queued jobs queued. With it, queued jobs are cancelled and
the running one is asked to stop. A running job always stops cooperatively: the command
reports a request, not a completed stop.

## Diagnosing a failure

1. `nordicintel-bootstrap jobs show <id>` — a job-level diagnostic means the job never got
   going. `configuration_invalid` is an adapter or a secret; `upstream_*` with stage
   `discovery` is the Provider's catalogue endpoint.
2. `nordicintel-bootstrap jobs items <id> --status failed` — per-Table failures, with the
   failing language in `details.languages`.
3. `nordicintel-bootstrap tables show <table-id>` — which languages are currently failing,
   and when each last succeeded. A language with `failed: true` is refetched on the next
   run whatever the adapter's markers say.
4. A Table missing in one language but not another? That is normal, and nothing is wrong.
   Catalogues are published per language and are not the same size; each language is
   harvested by its own job over its own listing. `nordicintel-bootstrap tables show
   <table-id>` lists the languages a Table actually has.
5. A Table that vanished upstream? Nothing acts on that. It keeps its identity, its stored
   metadata and its place in search until someone decides otherwise.

Diagnostics never contain URLs, request bodies or credentials: only exception types this
project and core define contribute their own text, and everything else is reported by
type. They are trimmed to fit core's 16 KiB limit rather than being dropped.

## Upgrading

Pin core and the adapter packages together, and run the migration as its own task before
starting the new workers:

```bash
python -m nordicintel_core.database migrate check    # revision heads and model drift
python -m nordicintel_core.database migrate upgrade head
```

`migrate check` compares the database against `Base.metadata`, not just the revision
string, so schema drift fails the release rather than a query at runtime.

Core is pinned to an exact version, currently `nordicintel-core==0.2.0` resolved from its
`v0.2.0` tag. A worker and the database it claims jobs from have to agree about the
schema, so the migration task and every worker image are built from the same pin. Run
`nordicintel-bootstrap adapters` and `migrate check` after an upgrade: the first says what
the process can run, the second says whether the database agrees with it.
