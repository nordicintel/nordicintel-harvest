"""Domain exception hierarchy for nordicintel-backend."""


class NordicIntelError(Exception):
    """Base exception for all application-level errors."""


class ProviderNotFoundError(NordicIntelError):
    """Raised when a provider_code does not exist in the static catalog."""

    def __init__(self, provider_code: str) -> None:
        self.provider_code = provider_code
        super().__init__(f"Provider not found: {provider_code!r}")


class UnsupportedLanguageError(NordicIntelError):
    """Raised when a language code is not supported by the provider."""

    def __init__(self, language: str, *, provider_code: str) -> None:
        self.language = language
        self.provider_code = provider_code
        super().__init__(f"Language {language!r} not supported by provider {provider_code!r}")


class ExternalAPIError(NordicIntelError):
    """Raised when an external API call fails after all retries are exhausted."""

    def __init__(self, message: str, *, url: str | None = None) -> None:
        self.url = url
        super().__init__(message)


class UnsupportedAdapterError(NordicIntelError):
    """Raised when a provider selects an unregistered adapter."""

    def __init__(self, adapter: str) -> None:
        self.adapter = adapter
        super().__init__(f"No provider adapter registered for {adapter!r}")
