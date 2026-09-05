# Changelog

## Unreleased

- First implementation: settings, adapter registry, secret resolution, the harvest engine,
  the same-session heartbeat, the worker and scheduler processes, and operator commands.
- A job is one Provider in one language, following core's contract. The engine no longer
  decides which languages a Table has: discovery for a language returns the Tables that
  exist in it, so `_candidate_languages`, the per-language loop, the per-language failure
  aggregation and the floor that had to override all of it are gone. `harvest enqueue` and
  `schedule set` take the language as an argument.
- Refuse a language the Provider does not publish once, at the start of the job, instead
  of discovering it one upstream error per Table.
- Read a Provider's own `config.request_interval_seconds` when building its HTTP client.
  An upstream quota is a property of the publisher, and the process-wide default now only
  applies to Providers that do not state one. Values decoded from JSONB arrive as exact
  `Decimal`s, which the validator accepts.
- Add one optional extra per adapter family (`nordicintel-harvest[pxweb2]`), and a working
  `providers/scb.json` for a first run.
