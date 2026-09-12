from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote

from nordicintel_harvest.providers.adapters.common import parse_dt
from nordicintel_harvest.providers.interface import DatasetCandidate
from nordicintel_harvest.schemas.datasets import DatasetPath


def _encode_segment(value: str) -> str:
    return quote(unquote(value), safe="")


def _normalize_path(path: str) -> str:
    ids = path.split("/")
    normalized_ids = [str(pid).strip() for pid in ids if str(pid).strip()]
    if len(normalized_ids) == 0:
        return ""
    elif len(normalized_ids) == 1:
        return normalized_ids[0]
    else:
        return "/".join(normalized_ids)


@dataclass
class PxWebTableEntry:
    id: str
    title: str
    path: str

    db_id: str
    language: str
    provider_code: str

    base_api_url: str
    base_web_url: str | None

    published: str | None = None
    updated: str | None = None

    @property
    def api_url(self) -> str:
        base_url = "{base_api_url}/{language}/{db_id}/{path}/{id}"
        api_url = base_url.format(
            base_api_url=self.base_api_url.rstrip("/"),
            language=self.language,
            db_id=_encode_segment(self.db_id),
            path=_normalize_path(self.path),
            id=_encode_segment(self.id),
        )
        return api_url

    @property
    def web_url(self) -> str | None:
        if not self.base_web_url:
            return None
        base_url = "{base_web_url}/{language}/{db_id}/{db_id}__{path}/{id}/"
        path_segment = _normalize_path(self.path)
        pat_web_segment = path_segment.replace("/", "__") if path_segment else ""
        web_url = base_url.format(
            base_web_url=self.base_web_url.rstrip("/"),
            language=self.language,
            db_id=_encode_segment(self.db_id),
            path=pat_web_segment,
            id=_encode_segment(self.id),
        )
        return web_url

    def to_discovered_dataset(
        self,
        path_id_mapping: dict[str, str] | None = None,
    ) -> DatasetCandidate | None:
        if path_id_mapping is None:
            path_id_mapping = {}

        path_ids = [segment.strip() for segment in self.path.split("/") if segment.strip()]

        path: list[dict[str, str]] = [
            {"code": pid, "label": path_id_mapping.get(pid, pid)} for pid in path_ids
        ]
        paths_value = [DatasetPath.model_validate({"path": path})] if path else None

        subject_code = path[-1]["code"] if path else None
        subject_label = path[-1]["label"] if path else None

        updated_dt = parse_dt(self.updated) if self.updated else parse_dt(self.published)
        if not updated_dt:
            raise ValueError(
                "Invalid date format for dataset %s from provider %s: updated=%s, published=%s",
                self.id,
                self.provider_code,
                self.updated,
                self.published,
            )

        return DatasetCandidate(
            provider_code=self.provider_code,
            dataset_code=self.id,
            language=self.language,
            metadata_url=self.api_url,
            data_url=self.api_url,
            web_url=self.web_url,
            label=self.title,
            updated=updated_dt,
            paths=paths_value,
            subject_code=subject_code,
            subject_label=subject_label,
        )
