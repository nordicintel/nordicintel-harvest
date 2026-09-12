from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from nordicintel_harvest.providers.adapters.pxweb_v2.parsing import (
    parse_pxweb2_discovery_table,
    parse_pxweb2_metadata_payload,
)
from nordicintel_harvest.providers.interface import DatasetCandidate, ProviderAdapter
from nordicintel_harvest.schemas.contracts import encoded_url
from nordicintel_harvest.schemas.datasets import (
    DatasetDocuments,
    DatasetInfo,
    DatasetMetadata,
    DatasetSubject,
)

_VALID_PXWEB2_ENDPOINTS = {"config", "tables", "metadata", "data", "codelists"}


def _get_api_url(base_api_url: str, endpoint: str, id: str | None = None) -> str:
    """Helper to construct API URLs for PxWeb2 endpoints.
    Args:
        base_api_url (str): The base API URL for the provider.
        endpoint (str): The API endpoint to construct the URL for. Must be one of:
            - "config": For fetching API configuration.
            - "tables": For fetching list of tables (datasets).
            - "metadata": For fetching metadata of a specific table (requires id).
            - "data": For fetching data of a specific table (requires id).
            - "codelists": For fetching codelists.
        id (str, optional): The dataset code (id) required for "metadata" and "data" endpoints. Defaults to None.
    Returns:
        str: The constructed API URL for the specified endpoint and id.
    Raises:
        ValueError: If the base_api_url is missing, if the endpoint is invalid, or if the required id is not provided for "metadata" or "data" endpoints.
    """

    if endpoint not in _VALID_PXWEB2_ENDPOINTS:
        raise ValueError(f"Invalid PxWeb2 endpoint: {endpoint}")

    if endpoint == "config":
        return f"{base_api_url}/config"
    elif endpoint == "tables" and id or endpoint == "tables":
        return f"{base_api_url}/tables"
    elif endpoint == "metadata" and id:
        return f"{base_api_url}/tables/{id}/metadata"
    elif endpoint == "data" and id:
        return f"{base_api_url}/tables/{id}/data"
    elif endpoint == "codelists":
        return f"{base_api_url}/codelists"
    else:
        raise ValueError(f"Invalid combination of endpoint and id: {endpoint}, {id}")


class PxWebV2Adapter(ProviderAdapter):
    def _get_api_url(self, endpoint: str, id: str | None = None) -> str:
        """Helper to construct API URLs for PxWeb2 endpoints.
        Args:
            endpoint (str): The API endpoint to construct the URL for. Must be one of:
                - "config": For fetching API configuration.
                - "tables": For fetching list of tables (datasets).
                - "metadata": For fetching metadata of a specific table (requires id).
                - "data": For fetching data of a specific table (requires id).
                - "codelists": For fetching codelists.
            id (str, optional): The dataset code (id) required for "metadata" and "data" endpoints. Defaults to None.
        Returns:
            str: The constructed API URL for the specified endpoint and id.
        Raises:
            ValueError: If the base_api_url is missing, if the endpoint is invalid, or if the required id is not provided for "metadata" or "data" endpoints.
        """

        if not self.base_api_url:
            raise ValueError("Provider missing base_api_url; cannot construct API URLs")

        return _get_api_url(self.base_api_url, endpoint, id)

    async def _fetch_page_tables(
        self,
        page_number: int = 1,
        page_size: int = 1000,
        updated_after: str | None = None,
        include_discontinued: bool = True,
    ) -> tuple[list[dict[str, Any]], int, int] | None:
        """Helper to fetch a page of tables from the /tables endpoint.
        Returns a tuple of (tables, current_page, total_pages) or None on failure.
        """

        if not self.base_api_url:
            self.logger.error("Provider missing base_api_url; cannot fetch tables")
            return None

        params: dict[str, Any] = {
            "lang": self.language,
            "pageSize": page_size,
            "pageNumber": page_number,
            "includeDiscontinued": str(include_discontinued).lower(),
        }
        if updated_after:
            params["updatedAfter"] = updated_after

        url = self._get_api_url("tables")
        response = await self.request_manager.get_json(
            url=url,
            params=params,
        )
        # extract tables list and pagination info from response
        if response and isinstance(response, dict):
            tables = response.get("tables")
            if not isinstance(tables, list):
                raise RuntimeError("Invalid pxweb2 tables response shape")
            page_info = response.get("page", {})
            current_page = int(page_info.get("pageNumber", page_number))
            total_pages = int(page_info.get("totalPages", current_page))
            return tables, current_page, total_pages
        else:
            self.logger.error(
                "Invalid response when fetching tables page for provider=%s language=%s page=%s",
                self.provider_code,
                self.language,
                page_number,
            )
            return [], page_number, page_number

    async def _api_table_to_discovered_dataset(
        self, table: dict[str, Any]
    ) -> DatasetCandidate | None:
        """Convert a table dict from /tables response to a DatasetCandidate.
        Returns None if the table dict is invalid or missing required fields.

        Args:
            table (dict): The table dict from the /tables API response.
        Returns:
            DatasetCandidate | None: the converted candidate, or None when invalid.
        Raises:
            ValueError: If the table dict is missing required fields or has invalid format.
        """
        try:
            assert isinstance(table, dict), "Table is not a dict"
            assert self.base_api_url is not None, "Provider missing base_api_url"

            parsed_table = parse_pxweb2_discovery_table(
                table,
                base_api_url=self.base_api_url,
                base_web_url=self.base_web_url,
                language=self.language,
            )
            assert parsed_table is not None, "Parsed table is None"

            updated = parsed_table["updated"]
            return DatasetCandidate(
                provider_code=self.provider_code,
                dataset_code=parsed_table["dataset_code"],
                language=self.language,
                label=parsed_table["label"],
                updated=updated,
                time_unit=parsed_table["time_unit"],
                first_period=parsed_table["first_period"],
                last_period=parsed_table["last_period"],
                description=parsed_table["description"],
                source=parsed_table["source"],
                note=parsed_table["note"],
                subject_code=parsed_table["subject_code"],
                paths=parsed_table["paths"],
                discontinued=parsed_table["discontinued"],
                metadata_url=parsed_table["metadata_url"],
                data_url=parsed_table["data_url"],
                web_url=parsed_table["web_url"],
                extension=parsed_table["extension"],
            )
        except Exception as e:
            self.logger.error(
                "Error parsing table into DatasetCandidate for provider=%s language=%s error=%s",
                self.provider_code,
                self.language,
                str(e),
            )
            return None

    async def _discover_candidates(self) -> AsyncIterator[DatasetCandidate]:
        """Discover PxWeb2 datasets by paginating /tables endpoint."""

        if not self.base_api_url:
            self.logger.error("Provider missing base_api_url; cannot discover datasets")
            return

        self.logger.info(
            "Starting pxweb2 discover_datasets provider=%s language=%s",
            self.provider_code,
            self.language,
        )

        page_number = 0
        accumulated = 0
        total_pages = 1  # initialize to enter the loop
        while True:
            page_number += 1

            result = await self._fetch_page_tables(
                page_number=page_number,
                page_size=1000,
                updated_after=None,
                include_discontinued=True,
            )
            if result is None:
                break

            tables, current_page, total_pages = result

            for table in tables:
                discovered = await self._api_table_to_discovered_dataset(table)
                if discovered:
                    yield discovered
                    accumulated += 1

            if current_page >= total_pages:
                break

        self.logger.info(
            "Completed pxweb2 discover_datasets provider=%s language=%s total=%s pages=%s",
            self.provider_code,
            self.language,
            accumulated,
            page_number,
        )

    async def resolve_dataset(
        self,
        discovered: DatasetCandidate,
    ) -> DatasetDocuments:
        """Resolve a PxWeb2 dataset by fetching /tables/{id}/metadata."""

        dataset_code = discovered.dataset_code
        dimension_ids: list[str] | None = None
        role = None
        dimensions = None
        note = discovered.note
        subject_label = discovered.subject_label
        official_statistics = discovered.official_statistics
        contact = discovered.contact
        extension = dict(discovered.extension)

        if self.base_api_url:
            metadata_url = self._get_api_url("metadata", id=dataset_code)
            params = {
                "lang": self.language,
                "outputFormat": "json-stat2",
            }
            payload = await self.request_manager.get_json(
                url=metadata_url,
                params=params,
            )

            if payload and isinstance(payload, dict):
                parsed_metadata = parse_pxweb2_metadata_payload(
                    payload,
                    default_note=note,
                    default_subject_label=subject_label,
                    default_official_statistics=official_statistics,
                    default_contact=contact,
                    default_extension=extension,
                )
                dimension_ids = parsed_metadata["id"]
                role = parsed_metadata["role"]
                dimensions = parsed_metadata["dimension"]
                note = parsed_metadata["note"]
                subject_label = parsed_metadata["subject_label"]
                official_statistics = parsed_metadata["official_statistics"]
                contact = parsed_metadata["contact"]
                extension = parsed_metadata["extension"]

        label = discovered.label or dataset_code

        identity = discovered.identity
        dataset = DatasetInfo(
            identity=identity,
            label=label,
            updated=discovered.updated,
            description=discovered.description,
            time_unit=discovered.time_unit,
            first_period=discovered.first_period or None,
            last_period=discovered.last_period or None,
            discontinued=discovered.discontinued,
            source_url=encoded_url(discovered.web_url) if discovered.web_url else None,
            doc_url=encoded_url(discovered.doc_url) if discovered.doc_url else None,
        )
        subject = {
            k: v
            for k, v in {
                "code": discovered.subject_code,
                "label": subject_label,
            }.items()
            if v
        }
        metadata_url = (
            discovered.metadata_url
            or f"{self._get_api_url('metadata', id=dataset_code)}?lang={self.language}"
        )
        for link in extension.get("links") or []:
            if (
                isinstance(link, dict)
                and link.get("rel") == "metadata"
                and link.get("hreflang") == self.language
                and link.get("href")
            ):
                metadata_url = link["href"]
                break
        detail = DatasetMetadata(
            identity=identity,
            source=discovered.source,
            note=note,
            subject=DatasetSubject(**subject) if subject else None,
            metadata_url=encoded_url(metadata_url),
            retrieval={
                "type": "pxweb_v2",
                "config": {
                    "data_url": encoded_url(
                        discovered.data_url
                        or f"{self._get_api_url('data', id=dataset_code)}?lang={self.language}"
                    )
                },
            },
            paths=discovered.paths,
            official_statistics=official_statistics,
            contact=contact,
            id=dimension_ids,
            role=role,
            dimension=dimensions,
            extension=extension,
        )
        attributes = DatasetDocuments(dataset=dataset, metadata=detail)

        return attributes
