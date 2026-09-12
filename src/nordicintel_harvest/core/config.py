from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    ENVIRONMENT: Literal["dev", "test", "staging", "production"] = "dev"

    DEFAULT_MAX_CONCURRENCY: int = 10
    REQUEST_GLOBAL_MAX_CONCURRENCY: int = 10
    REQUEST_TIMEOUT_SECONDS: int = 60
    REQUEST_DEFAULT_INTERVAL_SECONDS: float = 2.1
    REQUEST_DEFAULT_HOST_MAX_CONCURRENCY: int = 10
    REQUEST_429_DEFAULT_COOLDOWN_SECONDS: float = 60.0
    REQUEST_429_INTERVAL_INCREASE_SECONDS: float = 0.5
    REQUEST_429_MAX_COOLDOWN_SECONDS: float = 900.0

    @field_validator(
        "REQUEST_GLOBAL_MAX_CONCURRENCY",
        "REQUEST_TIMEOUT_SECONDS",
        "REQUEST_DEFAULT_HOST_MAX_CONCURRENCY",
        "DEFAULT_MAX_CONCURRENCY",
    )
    @classmethod
    def _validate_positive_int_settings(cls, value: int) -> int:
        if value < 1:
            raise ValueError("setting must be >= 1")
        return value

    @field_validator("REQUEST_DEFAULT_INTERVAL_SECONDS")
    @classmethod
    def _validate_default_interval_seconds(cls, value: float) -> float:
        if value < 0:
            raise ValueError("REQUEST_DEFAULT_INTERVAL_SECONDS must be >= 0")
        return value

    @field_validator(
        "REQUEST_429_DEFAULT_COOLDOWN_SECONDS",
        "REQUEST_429_INTERVAL_INCREASE_SECONDS",
        "REQUEST_429_MAX_COOLDOWN_SECONDS",
    )
    @classmethod
    def _validate_non_negative_float_settings(cls, value: float) -> float:
        if value < 0:
            raise ValueError("setting must be >= 0")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
