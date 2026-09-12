"""Resolved schema input, not a provider catalog record."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas.contracts import validate_contract


class HarvestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: Literal["pxweb_v1", "pxweb_v2", "kolada"]
    provider_code: str
    language: Literal["sv", "en"]
    rate_limit: float = Field(ge=0)
    config: dict

    @model_validator(mode="after")
    def validate_input(self):
        validate_contract("harvest-input", self.model_dump())
        for key in ("cell_limit", "max_concurrency"):
            value = self.config.get("extension", {}).get(key)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"config.extension.{key} must be a positive integer")
        return self

    @property
    def base_api_url(self):
        return (
            "https://api.kolada.se/v3" if self.adapter == "kolada" else self.config["base_api_url"]
        )

    @property
    def base_web_url(self):
        return self.config.get("extension", {}).get("base_web_url")

    @property
    def cell_limit(self):
        return self.config.get("extension", {}).get("cell_limit", 5000)

    @property
    def max_concurrency(self):
        return self.config.get("extension", {}).get("max_concurrency")
