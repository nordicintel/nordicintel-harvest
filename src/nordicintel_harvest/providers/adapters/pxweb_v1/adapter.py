from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from nordicintel_harvest.providers.adapters.common import (
    coerce_bool,
    detect_role,
    determine_time_unit,
    parse_dt,
)
from nordicintel_harvest.providers.adapters.pxweb_v1.catalog import PxWebTable, list_pxweb_tables
from nordicintel_harvest.providers.adapters.pxweb_v1.enrichment import enrich_metadata
from nordicintel_harvest.providers.adapters.pxweb_v1.parsing import _encode_segment
from nordicintel_harvest.providers.interface import DatasetCandidate, ProviderAdapter
from nordicintel_harvest.schemas.contracts import encoded_url
from nordicintel_harvest.schemas.datasets import (
    DatasetDimension,
    DatasetDocuments,
    DatasetInfo,
    DatasetMetadata,
    DatasetPath,
    DatasetSubject,
)
from nordicintel_harvest.schemas.dimensions import DatasetRole

if TYPE_CHECKING:
    from nordicintel_harvest.core.request_manager import RequestManager
    from nordicintel_harvest.inputs import HarvestInput


class PxWebV1Adapter(ProviderAdapter):
    """
    Adapter for APIs following the PC-Axis/PC-Axis Web (PXWEB) standard.

    Attributes:
        provider (Provider): The provider configuration.
        language (str): The language code for the scraper.
        adapter (str): The type of API (e.g., "pxweb_v1").
        provider_code (str): The id code of the provider (e.g., "scb").
        base_api_url (str): The base API URL for the provider.
        base_web_url (str): The base web URL for the provider.
        request_manager (RequestManager): Request manager for handling API requests.
        logger (logging.Logger): Logger instance for logging messages.
    """

    def __init__(
        self,
        provider: HarvestInput,
        language: str,
        request_manager: RequestManager,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(provider, language, request_manager, logger)
        self.db_ids = tuple(provider.config["database_ids"])
        self.base_api_url = provider.base_api_url
        if not self.base_api_url or len(self.base_api_url.strip()) == 0:
            raise ValueError(
                f"Pxweb Provider '{self.provider_code}' is missing base_api_url or dbid values; cannot initialize adapter"
            )
        self.base_web_url = provider.base_web_url

    def _get_root_url(self, base_api_url: str, db_id: str, language: str | None = None) -> str:
        url = "{base_api_url}/{language}/{db_id}".format(
            base_api_url=base_api_url.rstrip("/"),
            language=language or self.language,
            db_id=_encode_segment(db_id),
        )
        return url

    def _get_web_root_url(self, db_id: str) -> str | None:
        if not self.base_web_url:
            return None
        return "{base_web_url}/{language}/{db_id}/".format(
            base_web_url=self.base_web_url.rstrip("/"),
            language=self.language,
            db_id=_encode_segment(db_id),
        )

    def _candidate_from_occurrences(self, occurrences: list[PxWebTable]) -> DatasetCandidate | None:
        canonical = occurrences[0]
        updated = parse_dt(canonical.updated) or parse_dt(canonical.published)
        if updated is None:
            self.logger.error(
                "Skipping PXWeb table '%s' from provider '%s': invalid updated/published timestamp",
                canonical.id,
                self.provider_code,
            )
            return None

        paths: list[DatasetPath] = []
        seen_paths: set[tuple[tuple[str, str], ...]] = set()
        for occurrence in occurrences:
            path_key = tuple((category.id, category.name) for category in occurrence.category_path)
            if not path_key or path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            paths.append(
                DatasetPath.model_validate(
                    {
                        "path": [
                            {"code": category.id, "label": category.name}
                            for category in occurrence.category_path
                        ]
                    }
                )
            )

        subject = canonical.category_path[-1] if canonical.category_path else None
        return DatasetCandidate(
            provider_code=self.provider_code,
            dataset_code=canonical.id,
            language=self.language,
            label=canonical.title or canonical.id,
            updated=updated,
            subject_code=subject.id if subject else None,
            subject_label=subject.name if subject else None,
            metadata_url=canonical.api_url,
            data_url=canonical.api_url,
            web_url=canonical.web_url,
            paths=paths or None,
        )

    async def _discover_datasets_by_db_id(self, db_id: str) -> list[DatasetCandidate]:
        """Discover datasets for a specific db_id.

        Args:
            db_id (str): The database ID to discover datasets from.
        Yields:
            DatasetCandidate instances for the specified db_id.
        """
        api_url = self._get_root_url(self.base_api_url, db_id)
        try:
            entries = await list_pxweb_tables(
                self.request_manager,
                api_url=api_url,
                web_url=self._get_web_root_url(db_id),
                logger=self.logger,
            )
        except Exception as exc:
            self.logger.error(
                "Failed to list db_id '%s' from provider '%s': %s",
                db_id,
                self.provider_code,
                exc,
            )
            return []

        by_table_id: dict[str, list[PxWebTable]] = {}
        for entry in entries:
            by_table_id.setdefault(entry.id, []).append(entry)

        output: list[DatasetCandidate] = []
        for occurrences in by_table_id.values():
            try:
                discovered = self._candidate_from_occurrences(occurrences)
                if discovered:
                    output.append(discovered)
            except Exception as exc:
                self.logger.error(
                    "Error processing table '%s' for db_id '%s' from provider '%s': %s",
                    occurrences[0].id,
                    db_id,
                    self.provider_code,
                    exc,
                )
        return output

    async def _discover_candidates(self) -> AsyncIterator[DatasetCandidate]:
        """Discover datasets from the provider's catalog/listing API.

        Yields one DatasetCandidate at a time. Each carries identifiers plus
        whatever metadata the listing API returned for free.

        Yields:
            DatasetCandidate instances.
        """

        if not self.base_api_url:
            self.logger.error("Provider missing base_api_url; cannot discover datasets")
            return

        for db_id in self.db_ids:
            db_discoveries = await self._discover_datasets_by_db_id(db_id)
            for discovered in db_discoveries:
                if discovered:
                    yield discovered

    async def resolve_dataset(
        self,
        discovered: DatasetCandidate,
    ) -> DatasetDocuments:
        """Resolve a discovered dataset into typed DatasetDocuments.

        The adapter decides internally whether additional HTTP calls are needed.
        Some adapters may already have full data from discovery; others need
        per-dataset metadata fetches.

        Args:
            discovered: A dataset from the discovery phase.

        Returns:
            DatasetDocuments with resolved metadata, dimensions, role, and
            paths.
        """

        metadata_url = discovered.metadata_url
        if not metadata_url:
            msg = f"Discovered dataset from provider-lang '{self.provider_code}'-'{self.language}' is missing metadata_url; cannot resolve"
            self.logger.error(msg)
            raise ValueError(msg)

        payload = await self.request_manager.get_json(metadata_url)
        dimension_ids: list[str] = []
        dimensions: dict[str, DatasetDimension] = {}
        role_buckets: dict[str, list[str]] = {}
        time_labels: list[str] = []

        title = discovered.label or discovered.dataset_code

        if isinstance(payload, dict):
            payload_title = payload.get("title")
            if isinstance(payload_title, str) and payload_title.strip():
                title = payload_title.strip()

            variables = payload.get("variables")
            if isinstance(variables, list):
                if not variables:
                    raise ValueError("PxWeb v1 metadata payload declares no variables")
                for pos, var in enumerate(variables):
                    if not isinstance(var, dict):
                        raise ValueError(f"PxWeb v1 variable at position {pos} is not an object")

                    code = str(var.get("code") or "").strip()
                    if not code:
                        raise ValueError(f"PxWeb v1 variable at position {pos} has no code")
                    if code in dimensions:
                        raise ValueError(f"PxWeb v1 contains duplicate variable code {code!r}")
                    label = str(var.get("text") or code).strip()

                    raw_values = var.get("values")
                    if not isinstance(raw_values, list) or not raw_values:
                        raise ValueError(f"PxWeb v1 variable {code!r} has no values")
                    values: list[Any] = raw_values

                    raw_value_texts = var.get("valueTexts")
                    value_texts: list[Any] = (
                        raw_value_texts if isinstance(raw_value_texts, list) else []
                    )

                    role = detect_role(code, label, coerce_bool(var.get("time")) is True)

                    category_index: list[str] = []
                    category_labels: dict[str, str] = {}
                    for idx, raw_value in enumerate(values):
                        value_label = value_texts[idx] if idx < len(value_texts) else None
                        raw_code = str(raw_value).strip()
                        if not raw_code:
                            raise ValueError(f"PxWeb v1 variable {code!r} has an empty category id")
                        if raw_code in category_labels:
                            raise ValueError(
                                f"PxWeb v1 variable {code!r} has duplicate category id {raw_code!r}"
                            )
                        category_index.append(raw_code)
                        category_labels[raw_code] = (
                            str(value_label) if value_label is not None else raw_code
                        )

                    if role == "time":
                        labels = [
                            str(vt) for vt in value_texts if isinstance(vt, str) and vt.strip()
                        ]
                        if not labels:
                            labels = [
                                str(v) for v in values if isinstance(v, str) and str(v).strip()
                            ]
                        time_labels = labels
                    if role is not None:
                        role_buckets.setdefault(role, []).append(code)

                    dim_extension = {
                        str(key): value
                        for key, value in var.items()
                        if str(key)
                        not in {
                            "code",
                            "text",
                            "values",
                            "valueTexts",
                            "time",
                            "elimination",
                        }
                    }
                    if pos >= 0:
                        dim_extension["position"] = pos

                    elimination = coerce_bool(var.get("elimination"))
                    dim_extension["elimination"] = elimination if elimination is not None else False
                    dimension_ids.append(code)
                    dimensions[code] = DatasetDimension(
                        label=label,
                        category={
                            "index": {
                                category_code: ordinal
                                for ordinal, category_code in enumerate(category_index)
                            },
                            "label": category_labels,
                        },
                        extension=dim_extension,
                    )

        if not dimension_ids:
            raise ValueError("PxWeb v1 metadata payload declares no usable dimensions")

        sorted_time_labels = sorted(time_labels) if time_labels else []
        first_period = discovered.first_period or (
            sorted_time_labels[0] if sorted_time_labels else None
        )
        last_period = discovered.last_period or (
            sorted_time_labels[-1] if sorted_time_labels else None
        )

        time_unit = discovered.time_unit or determine_time_unit(first_period, last_period)

        # Per-entry extensions are merged into the candidate's extension by the
        # Dimension Set, existing keys first.
        extension: dict[str, Any] = dict(discovered.extension)

        identity = discovered.identity
        dataset = DatasetInfo(
            identity=identity,
            label=title,
            updated=discovered.updated,
            description=discovered.description,
            time_unit=time_unit,
            first_period=first_period,
            last_period=last_period,
            discontinued=discovered.discontinued,
            source_url=encoded_url(discovered.web_url) if discovered.web_url else None,
            doc_url=encoded_url(discovered.doc_url) if discovered.doc_url else None,
        )
        subject = {
            k: v
            for k, v in {
                "code": discovered.subject_code,
                "label": discovered.subject_label,
            }.items()
            if v
        }
        detail = DatasetMetadata(
            identity=identity,
            source=discovered.source,
            note=discovered.note,
            subject=DatasetSubject(**subject) if subject else None,
            metadata_url=encoded_url(metadata_url),
            retrieval={
                "type": "pxweb_v1",
                "config": {"data_url": encoded_url(discovered.data_url or metadata_url)},
            },
            paths=discovered.paths,
            official_statistics=discovered.official_statistics,
            contact=discovered.contact,
            id=dimension_ids,
            role=DatasetRole.model_validate(role_buckets) if role_buckets else None,
            dimension=dimensions,
            extension=extension,
        )
        metadata = DatasetDocuments(dataset=dataset, metadata=detail)

        return await enrich_metadata(metadata, self.request_manager, self.provider.cell_limit)

    async def close(self) -> None:
        """Close adapter-owned resources (shared request manager is external-owned)."""
        return
