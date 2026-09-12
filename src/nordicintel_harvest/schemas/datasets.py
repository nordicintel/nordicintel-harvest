"""Pydantic schemas for datasets."""

from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from nordicintel_harvest.schemas.contracts import validate_contract
from nordicintel_harvest.schemas.dimensions import (
    DatasetDimension,
    DatasetRole,
)


class DatasetIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_code: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    dataset_code: str = Field(pattern=r"\S")
    language: Literal["sv", "en"]


class PathItem(BaseModel):
    """One thematic classification node; never an access route."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"\S")
    label: str = Field(pattern=r"\S")
    extension: dict[str, Any] = Field(default_factory=dict)


class DatasetPath(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: list[PathItem] = Field(min_length=1)
    extension: dict[str, Any] = Field(default_factory=dict)


class DatasetContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw: str | None = None
    mail: str | None = None
    name: str | None = None
    phone: str | None = None
    organization: str | None = None
    extension: dict[str, Any] = Field(default_factory=dict)


class DatasetSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str | None = Field(default=None, pattern=r"\S")
    label: str | None = Field(default=None, pattern=r"\S")
    extension: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_description(self):
        if self.code is None and self.label is None:
            raise ValueError("subject requires code or label")
        return self


class PxWebRetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_url: str
    extension: dict[str, Any] = Field(default_factory=dict)


class KoladaRetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extension: dict[str, Any] = Field(default_factory=dict)


class PxWebRetrieval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["pxweb_v1", "pxweb_v2"]
    config: PxWebRetrievalConfig


class KoladaRetrieval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["kolada"] = "kolada"
    config: KoladaRetrievalConfig = Field(default_factory=KoladaRetrievalConfig)


class DatasetInfo(BaseModel):
    """Basic catalog information for one dataset language variant."""

    model_config = ConfigDict(extra="forbid")
    identity: DatasetIdentity
    label: str = Field(pattern=r"\S")
    description: str | None = None
    updated: date | AwareDatetime | None = None
    next_release: date | AwareDatetime | None = None
    time_unit: str | None = Field(default=None, pattern=r"\S")
    first_period: str | None = Field(default=None, pattern=r"\S")
    last_period: str | None = Field(default=None, pattern=r"\S")
    discontinued: bool | None = None
    source_url: str | None = None
    doc_url: str | None = None
    extension: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract_shape(self):
        validate_contract("dataset", self.model_dump(mode="json", exclude_none=True))
        return self


class DatasetMetadata(BaseModel):
    """Detailed metadata and self-contained retrieval configuration."""

    model_config = ConfigDict(extra="forbid")
    identity: DatasetIdentity
    id: list[str]
    dimension: dict[str, DatasetDimension]
    role: DatasetRole | None = None
    subject: DatasetSubject | None = None
    paths: list[DatasetPath] | None = None
    official_statistics: bool | None = None
    source: str | None = None
    note: list[str] | None = None
    contact: list[DatasetContact] | None = None
    metadata_url: str | None = None
    retrieval: PxWebRetrieval | KoladaRetrieval | None = Field(default=None, discriminator="type")
    extension: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dimension_structure(self) -> "DatasetMetadata":
        if not self.id:
            raise ValueError("id must contain at least one dimension id")
        if any(not dimension_id for dimension_id in self.id):
            raise ValueError("id must contain only non-empty strings")
        if len(self.id) != len(set(self.id)):
            raise ValueError("id must not contain duplicates")
        if set(self.dimension) != set(self.id):
            raise ValueError("id must exactly match dimension keys")

        if self.role is not None:
            assigned: dict[str, str] = {}
            positions = {dimension_id: index for index, dimension_id in enumerate(self.id)}
            for role_name in ("time", "geo", "metric"):
                values = getattr(self.role, role_name)
                if values is None:
                    continue
                unknown = [value for value in values if value not in positions]
                if unknown:
                    raise ValueError(f"role.{role_name} contains unknown dimension IDs: {unknown}")
                role_positions = [positions[value] for value in values]
                if role_positions != sorted(role_positions):
                    raise ValueError(f"role.{role_name} must follow id order")
                for value in values:
                    previous_role = assigned.get(value)
                    if previous_role is not None:
                        raise ValueError(
                            f"dimension {value!r} belongs to both {previous_role!r} "
                            f"and {role_name!r} roles"
                        )
                    assigned[value] = role_name
        return self

    @model_validator(mode="after")
    def validate_contract_shape(self):
        validate_contract("dataset-metadata", self.model_dump(mode="json", exclude_none=True))
        return self


class DatasetDocuments(BaseModel):
    """One resolved dataset, with matching basic and detailed documents."""

    model_config = ConfigDict(extra="forbid")
    dataset: DatasetInfo
    metadata: DatasetMetadata

    @model_validator(mode="after")
    def validate_pair(self):
        if self.dataset.identity != self.metadata.identity:
            raise ValueError("Dataset document identities must match")
        return self

    def documents(self):
        """JSON-ready documents; unavailable optional fields are omitted."""
        return self.model_dump(mode="json", exclude_none=True)
