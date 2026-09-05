"""Construction of diagnostics that are always safe to store and always fit.

A ``Diagnostic`` is written to a job or item row and read by anyone who can see the
queue, so two properties matter more than detail. It must not carry anything an exception
picked up on the way out — a URL with a token in it, a response body, a credential — and
it must not exceed the 16 KiB core enforces, because a diagnostic that fails validation
loses the explanation of a failure that already happened.

Both are handled by construction rather than by the caller remembering. Only exception
types whose messages this project controls contribute text; everything else contributes
its type name. Everything is then trimmed to fit before the model is built.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from nordicintel_core.errors import (
    AdmissionError,
    ConfigurationError,
    NordicIntelError,
    UpstreamError,
)
from nordicintel_core.models import Diagnostic, DiagnosticStage

MAX_DIAGNOSTIC_BYTES = 16 * 1024
_MAX_MESSAGE_CHARS = 1000
_MAX_LANGUAGE_DETAILS = 20


@dataclass(frozen=True, slots=True)
class LanguageFailure:
    """One language of one Table that could not be harvested during this attempt."""

    language: str
    stage: DiagnosticStage
    code: str
    message: str

    def as_details(self) -> dict[str, str]:
        return {
            "language": self.language,
            "stage": self.stage.value,
            "code": self.code,
            "message": self.message,
        }


def describe(exc: BaseException) -> tuple[str, str]:
    """Reduce an exception to a stable code and a message that is safe to store.

    Args:
        exc: The exception that ended a unit of work.

    Returns:
        A ``(code, message)`` pair. Only exception types defined by this project or by
        core contribute their own text; anything else is reported by type alone, because
        an arbitrary exception's message may repeat the request that produced it.
    """
    if isinstance(exc, UpstreamError):
        return exc.code, _shorten(str(exc))
    if isinstance(exc, ConfigurationError):
        return "configuration_invalid", _shorten(str(exc))
    if isinstance(exc, AdmissionError):
        return "admission_rejected", _shorten(exc.detail)
    if isinstance(exc, NordicIntelError):
        return _snake(type(exc).__name__), _shorten(str(exc))
    if isinstance(exc, ValueError):
        # Adapter output that failed validation. The text is structural, but it quotes the
        # offending input, so it is trimmed hard rather than trusted.
        return "invalid_adapter_result", _shorten(str(exc))
    return "unexpected_error", f"An unexpected {type(exc).__name__} ended this step."


def diagnose(
    exc: BaseException,
    *,
    stage: DiagnosticStage,
    details: Mapping[str, Any] | None = None,
) -> Diagnostic:
    """Build the diagnostic for one failed step."""
    code, message = describe(exc)
    return build(code, message, stage=stage, details=details)


def language_failure(language: str, stage: DiagnosticStage, exc: BaseException) -> LanguageFailure:
    """Record one language's failure for later aggregation onto its item."""
    code, message = describe(exc)
    return LanguageFailure(language=language, stage=stage, code=code, message=message)


def item_failure(failures: Sequence[LanguageFailure]) -> Diagnostic:
    """Summarize every language that failed for one Table into that item's diagnostic.

    The item carries one diagnostic, but a Table can fail in one language and succeed in
    another, so the per-language records go into ``details`` where they stay attributable.
    """
    if not failures:
        raise ValueError("an item failure requires at least one language failure")
    codes = sorted({failure.code for failure in failures})
    languages = [failure.language for failure in failures]
    return build(
        codes[0] if len(codes) == 1 else "language_failures",
        f"{len(failures)} language(s) failed: {', '.join(languages)}.",
        stage=failures[0].stage if len({f.stage for f in failures}) == 1 else None,
        details={"languages": [failure.as_details() for failure in failures]},
    )


def interrupted(reason: str) -> Diagnostic:
    """Build the diagnostic for work stopped by a cancellation rather than a fault."""
    return build("harvest_interrupted", reason, stage=DiagnosticStage.INTERRUPTED)


def build(
    code: str,
    message: str,
    *,
    stage: DiagnosticStage | None = None,
    details: Mapping[str, Any] | None = None,
) -> Diagnostic:
    """Build a diagnostic that is guaranteed to validate.

    Details are dropped before the message is trimmed, and the message keeps at least its
    first sentence: an explanation of what failed is worth more than the evidence.
    """
    payload: dict[str, Any] = dict(details or {})
    text = _shorten(message) or "No description was available."
    while True:
        candidate = Diagnostic.model_construct(
            code=code, message=text, stage=stage, details=payload
        )
        if _size(candidate) <= MAX_DIAGNOSTIC_BYTES:
            return Diagnostic.model_validate(candidate.model_dump())
        if payload:
            payload = _shrink(payload)
            continue
        if len(text) > 80:
            text = text[:77] + "..."
            continue
        # Nothing left to trim; the code alone still explains more than a lost diagnostic.
        return Diagnostic(code=code, message="The diagnostic was too large to store.")


def _shrink(payload: dict[str, Any]) -> dict[str, Any]:
    """Discard the largest remaining detail, halving a language list before dropping it."""
    languages = payload.get("languages")
    if isinstance(languages, list) and len(languages) > 1:
        return {**payload, "languages": languages[: len(languages) // 2]}
    largest = max(payload, key=lambda key: len(repr(payload[key])))
    remaining = {key: value for key, value in payload.items() if key != largest}
    return remaining


def _size(diagnostic: Diagnostic) -> int:
    try:
        payload = diagnostic.model_dump(mode="json")
    except Exception:  # an unserializable detail is itself the problem
        return MAX_DIAGNOSTIC_BYTES + 1
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode())
    except (TypeError, ValueError):
        return MAX_DIAGNOSTIC_BYTES + 1


def _shorten(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_MESSAGE_CHARS:
        return collapsed
    return collapsed[: _MAX_MESSAGE_CHARS - 3] + "..."


def _snake(name: str) -> str:
    return "".join(f"_{part.lower()}" if part.isupper() else part for part in name).lstrip("_")


def limit_language_details(failures: Sequence[LanguageFailure]) -> list[LanguageFailure]:
    """Cap how many language records one item reports, oldest first."""
    return list(failures[:_MAX_LANGUAGE_DETAILS])
