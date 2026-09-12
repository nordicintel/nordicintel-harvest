# nordicintel-harvest

Statistical metadata harvesting through the authenticated NordicIntel catalog API.
PXWeb v1, PXWeb v2 and Kolada adapters emit basic/metadata pairs conforming to
[nordicintel-schemas 1.0.0](https://github.com/nordicintel/nordicintel-schemas/releases/tag/v1.0.0).
Schemas are bundled for offline validation. Extraction source: nordicintel-backend
commit `35b7310`; relevant Apache-2.0 licensing and regression fixtures are retained.
The backend and schemas repositories are not runtime dependencies.

## Run and control

Install Python 3.12 and uv, then `uv sync --locked`. Copy `.env.example` to `.env`
and set the catalog URL and write token. Never commit credentials.

```sh
uv run nordicintel-harvest import-providers providers/initial.json
uv run nordicintel-harvest request --provider scb --language sv --limit 5
uv run nordicintel-harvest request-all --limit 5
uv run nordicintel-harvest status JOB_ID
uv run nordicintel-harvest cancel JOB_ID
uv run nordicintel-harvest worker
```

All request/control commands use the durable queue. There is no direct-execution
CLI shortcut. Repeat a request to retry a terminal job; `--force` bypasses selection
skips. Active duplicates return 409. Import creates missing providers, reports
existing differences, and never overwrites existing configuration. The import file
is bootstrap data, not a runtime configuration source; see [provenance](providers/README.md).

Use catalog Swagger `/docs` to inspect history/outcomes and to
`PUT /v1/harvest-control` with `{"paused": true}` or `{"paused": false}`.
Pausing leaves active work running. The queue starts paused after migration.
Read tokens may observe; requests and controls require the write token.

## Execution and configuration

The worker polls FIFO jobs and accepts resolved `HarvestInput` snapshots containing
adapter, provider_code, language, rate_limit and config. Provider descriptions,
harvest configuration and dataset retrieval configuration have separate roles.
Only Swedish/English metadata is supported, and Kolada accepts Swedish only.
PXWeb v1 needs base_api_url/database_ids; v2 needs base_api_url. Used optional
base_web_url/cell_limit/max_concurrency settings live in config.extension.
Kolada uses its fixed endpoint; OU dataset codes append `_OU`.

`rate_limit` means minimum seconds between request starts, including concurrent
requests. Catalog traffic has separate timeouts/retries. A positive `--limit`
counts metadata fetch attempts, including exhausted failures, excluding skips and
internal retries. Bounded runs never advance complete checkpoints. Dataset failures
continue; fatal discovery or catalog failures stop the run. Successful documents
remain saved when another dataset fails.

Incremental selection preserves accepted listing timestamps (including backward
changes), retry flags and Kolada change windows/refetch behavior. Only a complete,
unbounded successful run establishes a checkpoint. Configuration and state are
read from the catalog; this application has no database connection.

The catalog fences mutations using a global claim token and 90-second lease.
Heartbeats run every 15 seconds. The worker stops before its local lease deadline
if renewal fails. This fences writes, but cannot instantly stop a disconnected
process's upstream requests. SIGTERM/SIGINT stops claims and drains/finalizes work
within one 20-second cleanup budget; repeated signals do not extend it. If cleanup
cannot reach the catalog, lease expiry records failure and releases execution.

| Variable | Default / purpose |
| --- | --- |
| ENVIRONMENT | staging on Heroku |
| CATALOG_API_URL | Required catalog base URL |
| CATALOG_WRITE_TOKEN | Required write credential |
| LOG_LEVEL | INFO |
| HARVEST_MAX_CONCURRENCY | 5 |
| REQUEST_TIMEOUT_SECONDS | 60 |
| CATALOG_REQUEST_TIMEOUT_SECONDS | 10 |
| WORKER_POLL_SECONDS | 5 |

## Deployment and checks

Heroku app `nordicintel-harvest` is in EU, with the official Python buildpack and
one Basic worker dyno. No web process or Postgres add-on is required.

```procfile
worker: nordicintel-harvest worker
```

Develop on main; CI runs lint, formatting, tests and wheel installation with
read-only permissions. Deploy the catalog migration/interface first, then this
worker. Initial worker deployment uses `git push heroku main` after GitHub checks
pass. Automatic GitHub deployment is not yet configured on the worker app; choose
main and enable wait-for-checks in its Heroku deployment settings when available.
The catalog already uses that configuration.

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

For diagnosis, inspect catalog job error and per-dataset outcomes first, then
`heroku logs --app nordicintel-harvest`. Restart with
`heroku ps:restart worker --app nordicintel-harvest`; interrupted work fails and
requires a fresh explicit request. Do not run a local worker against the hosted
catalog during manual claim tests. Durable records survive process replacement.
Observation retrieval/storage, scheduling and public PxWeb responses remain future work.
