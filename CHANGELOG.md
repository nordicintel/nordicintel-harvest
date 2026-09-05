# Changelog

## Unreleased

- First implementation: settings, adapter registry, secret resolution, the harvest engine,
  the same-session heartbeat, the worker and scheduler processes, and operator commands.
- Confine a Table's languages to the ones its discovery entry reports it as having. The
  floor that forces a refetch of a never-harvested or failed language is about freshness;
  a language a Table was never published in is not stale but absent, and asking for it is
  an upstream error. At SCB that is 1,878 of 5,253 tables, each of which would otherwise
  have failed in English on every run and stayed permanently `unavailable`.
- Read a Provider's own `config.request_interval_seconds` when building its HTTP client.
  An upstream quota is a property of the publisher, and the process-wide default now only
  applies to Providers that do not state one. Values decoded from JSONB arrive as exact
  `Decimal`s, which the validator accepts.
- Add one optional extra per adapter family (`nordicintel-harvest[pxweb2]`), and a working
  `providers/scb.json` for a first run.
