# nordicintel-harvest

The scheduler and worker that keep the NordicIntel catalogue current.

Core owns the schema, the repositories, the adapter protocol and the HTTP transport. This
repository owns the processes that use them: what to fetch, in what order, and what an
outcome means. It declares no tables, ships no migrations, and reimplements no client.

## Processes

| Command | Responsibility |
|---|---|
| `nordicintel-scheduler` | One singleton process. Recovers abandoned jobs, then enqueues due schedules. |
| `nordicintel-worker` | Claims one job at a time, runs it on one database backend, finalizes it. |
| `nordicintel-bootstrap` | Operator commands for providers, schedules and the queue, until the API exists. |

Migrations are a separate release task and belong to core:

```bash
python -m nordicintel_core.database migrate upgrade head
```

## Adapters

An adapter package registers its `AdapterFactory` in the `nordicintel.adapters` entry
point group, under the name a Provider row uses as its `adapter_type`:

```toml
[project.entry-points."nordicintel.adapters"]
pxweb = "nordicintel_adapter_pxweb:factory"
```

`nordicintel-bootstrap adapters` lists what this process can actually run.

See [docs/operations.md](docs/operations.md) for configuration, a first run, and failure
diagnosis.

## Development

```bash
uv sync --extra pxweb2
uv run ruff check . && uv run mypy
uv run pytest -m "not postgres"

# Ownership, cancellation and recovery need real backends.
export NORDICINTEL_TEST_DATABASE_URL=postgresql://postgres:pw@localhost:5432/nordicintel_test
uv run python -m nordicintel_core.database migrate upgrade head
uv run pytest
```

`NORDICINTEL_TEST_DATABASE_URL` must name a **throwaway** database: every database test
truncates the schema first. The suite refuses to run when it names the same database as
`NORDICINTEL_DATABASE_URL`, because emptying a working catalogue fails silently — an
empty catalogue is a perfectly valid state.
