# Hosted acceptance - 2026-09-12

The full path passed: catalog request, durable queue, Heroku worker, validated
pair submission, PostgreSQL persistence and authenticated observation.

Twenty providers were imported. All **33 configured provider/language scopes
succeeded**, five fetch attempts each: **165 saved pairs, zero failed outcomes**.
Every pair was read back through the catalog API and independently validated
against bundled 1.0.0 schemas plus identity/dimension/category consistency checks.
No bounded job established a checkpoint. This is bounded acceptance, not a test of
every upstream table.

| Provider | Language | Saved | Job ID |
| --- | --- | ---: | --- |
| asub | en | 5 | `34b37882-7e61-44d3-ba72-68efee32709e` |
| asub | sv | 5 | `71431ff6-6fde-4df4-8d24-4e2dde7117cf` |
| csn | sv | 5 | `1893f046-3857-4b09-a6a6-ec7c40429fd9` |
| domstol | sv | 5 | `ef3a4291-fc14-4e1b-8301-d707d2285fff` |
| domstol | en | 5 | `4860fd04-a757-4002-bc47-1aacbe7d17e6` |
| energimyndigheten | sv | 5 | `763489bd-a7c0-42fa-9a4d-54484213d72a` |
| energimyndigheten | en | 5 | `53c7c0dc-0f06-4055-b0ec-d900d5b03a76` |
| fohm | sv | 5 | `a6378552-5d65-4c3c-8fcd-4bcb2c62e1bc` |
| fohm | en | 5 | `426b0124-e6f9-41c1-a633-991e21aca0da` |
| kolada | sv | 5 | `61fc4f1b-4de3-4669-b4b5-01e07268d231` |
| konj | sv | 5 | `5056d1b7-fe7f-4558-bc77-d82bcddc9ca0` |
| konj | en | 5 | `1afacbf2-f5d3-4fef-a54b-0328fa734cce` |
| lansstyrelsen | sv | 5 | `934e7310-1a97-478d-8a9c-2fb3a85fca64` |
| luke | en | 5 | `2d56d0b5-469b-4535-8656-af98ec36ab02` |
| luke | sv | 5 | `2fe79b46-3d0a-473e-b278-2a4fd4ebc4b7` |
| msb | sv | 5 | `17381243-f7ca-4047-9f71-472574f10c60` |
| msb | en | 5 | `57c333fe-4cca-4b09-80a9-aa051a14a060` |
| nordicstat | en | 5 | `df0c9e63-f1f0-45b1-9d89-a62de20b0632` |
| riksskog | sv | 5 | `ea0b4cb3-5347-4916-b0c2-558f4db62bf5` |
| riksskog | en | 5 | `9fe63608-9a93-43e1-8652-89f7f522002e` |
| scb | sv | 5 | `10a34a98-1071-4be5-ae0e-9713b667ef30` |
| scb | en | 5 | `e5317b2b-146d-42a2-88ad-36211da3c045` |
| sjv | sv | 5 | `89ccafaa-93de-4209-85dc-556bcff95274` |
| skogsstyrelsen | sv | 5 | `24da5ba4-0097-4ccc-80dc-ef1de84a3197` |
| skogsstyrelsen | en | 5 | `14839320-3b65-4afb-813f-5978f85c58d2` |
| ssb | en | 5 | `e431c597-cbd0-4ba7-a69d-f1361ad0ba6c` |
| statfin | en | 5 | `7aad9182-7cf2-4b5c-baec-d9ed7b8a9475` |
| statfin | sv | 5 | `ae7f3eb6-0c3e-4b1e-944e-bfa6187bebbd` |
| trafi2 | en | 5 | `92f95b85-c8a6-4370-a4ec-71854fae1ff2` |
| trafi2 | sv | 5 | `c010162e-89e9-405b-8f62-dd6bb8ac94ff` |
| unece | en | 5 | `7ee93d61-3703-4f3b-8cc1-dfb139d1a4ab` |
| vero | en | 5 | `401d56d1-3dbf-429f-8c84-6757a5846afa` |
| vero | sv | 5 | `918248ab-dce4-4fde-a0db-79d6e5f24a7f` |

The hosted Kolada sample contained municipality KPIs. A separate live adapter
resolution of **N15033_OU** passed validation; targeted tests cover both kinds and
separate state. Three unknown KPI-group members were reported by the upstream
catalogue without blocking metadata.

## Operational verification

- Read token observes but cannot request/claim; write token controls execution.
- Pausing prevented claims; resuming allowed execution. Active duplicate returned
  409; batch reported one created scope and one conflict. Queued cancellation passed.
- An abandoned hosted claim expired. Its heartbeat, outcome and completion all
  returned 409. Later jobs ran, and history survived temporary provider deletion.
- Incremental Aland/Swedish skipped five existing datasets and saved five more.
  Force saved five without skips.
- Running cancellation became cancelled. Worker restart during a slow temporary
  scope persisted failed / `Worker interrupted`. An explicit retry was subsequently
  claimed and cancelled for cleanup.
- Catalog restart preserved document content and all 33 successful summaries.
- Real providers and datasets remain. Temporary lifecycle providers were removed
  only after terminal jobs. Queue left unpaused with one Basic worker.

- incremental: `528c2e4e-ec8b-496e-8b69-a9a564957c7e` (succeeded; saved=5, skipped=5).
- forced: `55512efc-9c70-4032-ad49-2a7489009da3` (succeeded; saved=5, skipped=0).
- running_cancel: `d14ff501-7d80-4fdc-aec9-fc2180ba1a3f` (cancelled; saved=0, skipped=0).
- worker_restart: `da36dd57-1bcf-4ade-98ff-5d3506995565` (failed; saved=0, skipped=0).
- explicit_retry: `0eed9a6b-6eb6-4eee-8845-8b66df181ef7` (cancelled; saved=0, skipped=0).

## Automated checks and local HTTP integration

Catalog CI: **181 tests**, covering real PostgreSQL concurrent claims, atomic
rollback/replay, stale fencing, snapshots, cancellation, retry state, CRUD
invalidation and existing regressions. Worker CI: **240 tests**, including extracted
adapter regressions, singleton PXWeb enrichment, throttling, bounded attempts,
interruption deadlines and transient HTML proxy errors. Lint, formatting and wheel
installation checks pass. All eight catalog-bundled schemas match public 1.0.0.

An actual local catalog HTTP process with disposable PostgreSQL and a controlled
PXWeb server also completed saved, skipped and forced-saved runs. Bulky evidence
remains ignored under `tmp/`: live-results.json, verified-pairs.json,
lifecycle-results.json, hosted-controls.json, http-integration.json and
kolada-ou-live.json. Claim credentials are excluded from published evidence.

The backend remains untouched at `35b7310`; schemas remain 1.0.0. Both applications
use Python 3.12, uv and EU Heroku apps. The worker has no database add-on.

This acceptance does not claim full upstream coverage, recurring scheduling or
observation retrieval. See the README for requesting, observing, cancelling,
retrying, pausing, configuring and diagnosing jobs.

Both apps now have automatic deployments from `main` gated by GitHub checks.
The worker setting was enabled and verified in the Heroku dashboard.
