"""Control-flow signals raised inside one harvest attempt.

These are not upstream failures. They say how an attempt ended, which decides how it is
finalized, so they are deliberately distinct from the shared errors in
:mod:`nordicintel_core.errors` that a Table-level failure is built from.
"""

from __future__ import annotations

from nordicintel_core.models import Diagnostic


class HarvestStopped(Exception):
    """The attempt observed a cancellation request and stopped cooperatively.

    Reaching this means the stop was seen, not that the database session was lost, so the
    job can still be finalized on the owner session that raised it.
    """

    def __init__(self, reason: str = "The job was asked to stop.") -> None:
        super().__init__(reason)
        self.reason = reason


class JobFailed(Exception):
    """The attempt could not proceed at all, and the whole job fails with a diagnostic.

    Discovery and configuration failures land here. A Table that fails is not: it becomes
    a failed item inside a job that keeps going.
    """

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
