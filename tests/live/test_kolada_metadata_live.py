"""Full catalogue audit and bounded, full-history public adapter resolutions.

Run: uv run pytest tests/live/test_kolada_metadata_live.py -q
Inspect .artifacts/kolada-metadata-live.json, including partial failures.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from legacy_inputs import provider_catalog

from nordicintel_harvest.core.request_manager import RequestManager
from nordicintel_harvest.providers.adapters.kolada.adapter import KoladaAdapter
from nordicintel_harvest.providers.interface import HarvestContext
from tests.live._helpers import to_jsonable


class AuditRequests(RequestManager):
    def __init__(self):
        super().__init__(
            global_max_concurrency=3,
            default_interval=0.35,
            default_host_max_concurrency=3,
        )
        self.catalogue_pages = {}
        self.request_count = 0

    async def get_json(self, url, **kwargs):
        self.request_count += 1
        result = await super().get_json(url, **kwargs)
        if url.split("?")[0].endswith(("/kpi", "/kpi_groups")):
            self.catalogue_pages[url] = result
        return result


@pytest.mark.live
async def test_kolada_metadata_live():
    artifact = Path(".artifacts/kolada-metadata-live.json")
    artifact.parent.mkdir(exist_ok=True)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "scopes": {},
        "errors": [],
    }
    requests = AuditRequests()
    adapters = []

    def save():
        report["request_count"] = requests.request_count
        report["catalogue_pages"] = requests.catalogue_pages
        artifact.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    try:
        await requests.start()
        now = datetime.now(UTC)
        for provider in ("kolada",):
            definition = provider_catalog.require_scope(provider, "sv")
            await requests.set_limit(
                definition.base_api_url,
                interval=float(definition.rate_limit),
                max_concurrency=3,
            )
            adapter = KoladaAdapter(definition, "sv", requests)
            adapters.append(adapter)
            selected = await asyncio.wait_for(_discover(adapter, now), timeout=300)
            candidates = {s.candidate.dataset_code: s.candidate for s in selected}
            assert len(candidates) == len(selected)
            assert candidates
            summary = {
                "kpis": len(candidates),
                "with_subject": sum(bool(c.subject_label) for c in candidates.values()),
                "with_paths": sum(bool(c.paths) for c in candidates.values()),
                "discontinued": sum(c.discontinued for c in candidates.values()),
                "distinct_sources": len({c.source for c in candidates.values()}),
                "source_origins": dict(
                    Counter(c.extension["kolada"]["source_origin"] for c in candidates.values())
                ),
                "path_counts": dict(Counter(len(c.paths or []) for c in candidates.values())),
                "fallback_ids": [
                    c.dataset_code
                    for c in candidates.values()
                    if c.extension["kolada"]["source_origin"] == "fallback"
                ],
                "long_source_ids": [
                    c.dataset_code for c in candidates.values() if len(c.source or "") > 120
                ],
            }
            scope = {
                "summary": summary,
                "candidates": to_jsonable(list(candidates.values())),
                "resolved": {},
            }
            report["scopes"][provider] = scope
            assert all(c.updated is None for c in candidates.values())
            assert all(s.source_updated_at is None for s in selected)
            assert all(c.paths for c in candidates.values() if c.subject_label)
            # Two different labels must never collapse to the same synthetic ID.
            labels = {c.subject_label for c in candidates.values() if c.subject_label}
            codes = {c.subject_code for c in candidates.values() if c.subject_label}
            assert len(labels) == len(codes)
            raw_kpis = {
                row["id"]
                for url, page in requests.catalogue_pages.items()
                if url.split("?")[0].endswith("/kpi")
                for row in page["values"]
            }
            raw_groups = {
                row["id"]: row
                for url, page in requests.catalogue_pages.items()
                if url.split("?")[0].endswith("/kpi_groups")
                for row in page["values"]
            }
            summary["groups"] = len(raw_groups)
            summary["unknown_group_members"] = sorted(
                {member["member_id"] for g in raw_groups.values() for member in g["members"]}
                - raw_kpis
            )
            save()
            sample_ids = ["N15522", "U00300", "N60988", "N15033_OU"]

            async def resolve(
                code,
                adapter=adapter,
                candidates=candidates,
                scope=scope,
                provider=provider,
            ):
                try:
                    metadata = await asyncio.wait_for(
                        adapter.resolve_dataset(candidates[code]), timeout=900
                    )
                    document = metadata.documents()
                    scope["resolved"][code] = document
                    assert "updated" not in document["dataset"]
                    assert metadata.metadata.paths == candidates[code].paths
                    assert metadata.metadata.source == candidates[code].source
                    assert set(metadata.metadata.id) == set(metadata.metadata.dimension)
                    assert metadata.dataset.last_period
                except Exception as exc:
                    report["errors"].append({"provider": provider, "kpi": code, "error": repr(exc)})
                finally:
                    save()

            await asyncio.gather(*(resolve(code) for code in sample_ids))
        assert not report["errors"], report["errors"]
        report["completed_at"] = datetime.now(UTC).isoformat()
    except Exception as exc:
        report["errors"].append({"error": repr(exc)})
        raise
    finally:
        save()
        for adapter in adapters:
            await adapter.close()
        await requests.close()


async def _discover(adapter, now):
    return [selection async for selection in adapter.discover_datasets(HarvestContext({}, now))]
