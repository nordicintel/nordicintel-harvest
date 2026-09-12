"""Translate preserved historical fixture constructors to the public input contract."""

from nordicintel_harvest.inputs import HarvestInput


def ProviderDefinition(**values):
    adapter = values["adapter"]
    config = {} if adapter == "kolada" else {"base_api_url": values["base_api_url"]}
    if adapter == "pxweb_v1":
        config["database_ids"] = list(values.get("extension", {}).get("dbid", {"test": "Test"}))
    extension = {
        k: values[k]
        for k in ("base_web_url", "cell_limit", "max_concurrency")
        if values.get(k) is not None
    }
    if extension:
        config["extension"] = extension
    return HarvestInput(
        adapter=adapter,
        provider_code=values["provider_code"],
        language=values["default_language"],
        rate_limit=values.get("rate_limit", 1),
        config=config,
    )


class TestCatalog:
    def require_scope(self, code, language):
        return HarvestInput(
            adapter="kolada", provider_code=code, language=language, rate_limit=1, config={}
        )


provider_catalog = TestCatalog()
