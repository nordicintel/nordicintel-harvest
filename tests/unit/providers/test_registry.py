from pathlib import Path

import pytest
from legacy_inputs import ProviderDefinition

import nordicintel_harvest.providers.adapters
from nordicintel_harvest.core.errors import UnsupportedAdapterError
from nordicintel_harvest.providers.adapters.kolada.adapter import KoladaAdapter
from nordicintel_harvest.providers.adapters.pxweb_v1.adapter import PxWebV1Adapter
from nordicintel_harvest.providers.registry import ADAPTER_REGISTRY, get_adapter


class _RequestManager:
    pass


def test_registry_contains_only_canonical_adapter_identifiers() -> None:
    adapter_root = Path(nordicintel_harvest.providers.adapters.__file__).parent
    package_names = {
        path.name
        for path in adapter_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    }
    assert set(ADAPTER_REGISTRY) == package_names


def test_get_adapter_constructs_the_selected_adapter() -> None:
    provider = ProviderDefinition(
        provider_code="pxweb_v1",
        adapter="pxweb_v1",
        default_language="en",
        languages=["en"],
        label="Example PXWeb provider",
        country_code="DK",
        base_api_url="https://api.example.test",
    )

    adapter = get_adapter(
        "pxweb_v1",
        provider=provider,
        language="en",
        request_manager=_RequestManager(),  # type: ignore[arg-type]
    )

    assert isinstance(adapter, PxWebV1Adapter)


@pytest.mark.parametrize("provider_code", ["kolada"])
def test_kolada_provider_scopes_use_the_kolada_package(provider_code: str) -> None:
    provider = ProviderDefinition(
        provider_code=provider_code,
        adapter="kolada",
        default_language="sv",
        languages=["sv"],
        label="Kolada",
        country_code="SE",
        base_api_url="https://api.kolada.se/v3",
    )

    adapter = get_adapter(
        provider.adapter,
        provider=provider,
        language="sv",
        request_manager=_RequestManager(),  # type: ignore[arg-type]
    )

    assert isinstance(adapter, KoladaAdapter)


def test_get_adapter_rejects_unknown_identifiers() -> None:
    with pytest.raises(UnsupportedAdapterError):
        get_adapter(
            "pxweb2",
            provider=None,
            language="en",
            request_manager=_RequestManager(),  # type: ignore[arg-type]
        )
