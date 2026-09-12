"""Live tests — PxWeb2 adapter (Statistics Sweden / SCB).

Exercises real HTTP calls to the SCB API.  Run with::

    pytest tests/live/test_pxweb2_live.py -v

Output files written to ``tests/live/output/`` (overwritten each run):

* ``pxweb2_discovered.json`` — up to 3 DatasetCandidate objects
* ``pxweb2_resolved.json``   — 1 DatasetDocuments pair
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from legacy_inputs import ProviderDefinition

from nordicintel_harvest.core.request_manager import RequestManager
from nordicintel_harvest.providers.adapters.pxweb_v2.adapter import PxWebV2Adapter
from nordicintel_harvest.providers.interface import HarvestContext

from ._helpers import (
    assert_discovered_dataset_contract,
    assert_resolved_dataset_contract,
    collect_with_timeout,
    save_json,
)

logger = logging.getLogger(__name__)

_PROVIDER: dict = {
    "provider_code": "scb",
    "adapter": "pxweb_v2",
    "default_language": "sv",
    "languages": ["sv", "en"],
    "label": {"sv": "Statistiska centralbyrån", "en": "Statistics Sweden"},
    "country_code": "SE",
    "base_api_url": "https://statistikdatabasen.scb.se/api/v2",
    # base_web_url: pxweb2.pages.dev is the official PxWeb 2.0 reference frontend
    # hosted by SCB — not a development domain; it is the live web interface.
    "base_web_url": "https://pxweb2.pages.dev",
    "rate_limit": 0.4,
    "cell_limit": 150000,
    "variable_limit": 1000,
    "data_formats": ["csv", "json-stat2"],
    "extension": {},
}

_SAMPLE_COUNT = 3


def _make_provider(language: str) -> ProviderDefinition:
    labels = _PROVIDER["label"]
    return ProviderDefinition(
        provider_code=_PROVIDER["provider_code"],
        adapter=_PROVIDER["adapter"],
        default_language=_PROVIDER["default_language"],
        languages=_PROVIDER["languages"],
        label=labels.get(language, labels[_PROVIDER["default_language"]]),
        country_code=_PROVIDER["country_code"],
        base_api_url=_PROVIDER["base_api_url"],
        base_web_url=_PROVIDER["base_web_url"],
        rate_limit=_PROVIDER["rate_limit"],
        cell_limit=_PROVIDER["cell_limit"],
        variable_limit=_PROVIDER["variable_limit"],
        data_formats=_PROVIDER["data_formats"],
        extension=_PROVIDER["extension"],
    )


@pytest.mark.live
@pytest.mark.asyncio
async def test_pxweb2_discover_and_resolve(output_dir: Path, live_timeout: float) -> None:
    """Discover up to 3 datasets and resolve the first one; save JSON samples."""
    language = "sv"
    manager = RequestManager(
        global_max_concurrency=10,
        default_interval=_PROVIDER["rate_limit"],
        default_max_concurrency=1,
    )
    adapter = PxWebV2Adapter(
        provider=_make_provider(language),
        language=language,
        request_manager=manager,
    )

    discovered, timed_out = await collect_with_timeout(
        adapter.discover_datasets(HarvestContext({}, datetime.now(UTC))),
        max_items=_SAMPLE_COUNT,
        timeout=live_timeout,
        label="pxweb2 datasets",
    )
    await manager.close()
    discovered = [selection.candidate for selection in discovered]

    if not discovered:
        if timed_out:
            pytest.skip("Kill switch fired before any datasets were discovered")
        pytest.skip("SCB returned no datasets — skipping")

    for item in discovered:
        assert_discovered_dataset_contract(item)

    save_json(output_dir / "pxweb2_discovered.json", discovered)
    logger.info("pxweb2: %d dataset(s) discovered", len(discovered))

    # Resolve the first discovered dataset
    manager2 = RequestManager(
        global_max_concurrency=10,
        default_interval=_PROVIDER["rate_limit"],
        default_max_concurrency=1,
    )
    adapter2 = PxWebV2Adapter(
        provider=_make_provider(language),
        language=language,
        request_manager=manager2,
    )
    resolved = None
    try:
        resolved = await asyncio.wait_for(
            adapter2.resolve_dataset(discovered[0]),
            timeout=live_timeout,
        )
    except TimeoutError:
        logger.warning("pxweb2 resolve_dataset kill switch triggered after %.0f s", live_timeout)
    finally:
        await manager2.close()

    if resolved is not None:
        assert_resolved_dataset_contract(resolved)
        save_json(output_dir / "pxweb2_resolved.json", resolved)
        logger.info("pxweb2: resolved dataset '%s'", discovered[0].dataset_code)

    assert len(discovered) > 0
    if timed_out:
        logger.warning("pxweb2 discovery timed out; partial results saved")
