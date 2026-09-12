from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nordicintel_harvest.core.errors import UnsupportedAdapterError
from nordicintel_harvest.core.request_manager import RequestManager
from nordicintel_harvest.providers.adapters.kolada.adapter import KoladaAdapter
from nordicintel_harvest.providers.adapters.pxweb_v1.adapter import PxWebV1Adapter
from nordicintel_harvest.providers.adapters.pxweb_v2.adapter import PxWebV2Adapter
from nordicintel_harvest.providers.interface import ProviderAdapter

if TYPE_CHECKING:
    from nordicintel_harvest.inputs import HarvestInput

AdapterType = type[ProviderAdapter]

ADAPTER_REGISTRY: dict[str, AdapterType] = {
    "kolada": KoladaAdapter,
    "pxweb_v1": PxWebV1Adapter,
    "pxweb_v2": PxWebV2Adapter,
}


def has_adapter(name: str) -> bool:
    return name.strip().lower() in ADAPTER_REGISTRY


def get_adapter(
    name: str,
    *,
    provider: HarvestInput,
    language: str,
    request_manager: RequestManager,
    logger: logging.Logger | None = None,
) -> ProviderAdapter:
    adapter_type = ADAPTER_REGISTRY.get(name.strip().lower())
    if adapter_type is None:
        raise UnsupportedAdapterError(name)
    return adapter_type(
        provider=provider,
        language=language,
        request_manager=request_manager,
        logger=logger,
    )
