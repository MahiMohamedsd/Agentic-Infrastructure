"""Error taxonomy shared by every MAIT component.

Each error carries a stable ``code`` that the REST API exposes verbatim
(ARCHITECTURE.md §6.2). Codes are part of the public contract; messages are not.
"""

from __future__ import annotations


class MaitError(Exception):
    code: str = "mait.internal"


class UnknownEventType(MaitError):
    code = "execution.unknown_event_type"

    def __init__(self, event_type: str) -> None:
        super().__init__(f"unknown event type: {event_type!r}")
        self.event_type = event_type


class IllegalTransition(MaitError):
    code = "execution.illegal_transition"

    def __init__(self, status: str, event_type: str, detail: str = "") -> None:
        msg = f"event {event_type!r} is not allowed in status {status}"
        super().__init__(f"{msg}: {detail}" if detail else msg)
        self.status = status
        self.event_type = event_type


class StaleLease(MaitError):
    """A writer presented a lease generation that is no longer current (fencing)."""

    code = "execution.stale_lease"

    def __init__(self, presented: int, current: int) -> None:
        super().__init__(f"stale lease generation {presented}; current is {current}")
        self.presented = presented
        self.current = current


class LeaseNotHeld(MaitError):
    code = "execution.lease_not_held"


class WriterNotAllowed(MaitError):
    code = "execution.writer_not_allowed"


class SequenceConflict(MaitError):
    """An event's sequence is not exactly current + 1."""

    code = "execution.sequence_conflict"

    def __init__(self, expected: int, got: int) -> None:
        super().__init__(f"expected sequence {expected}, got {got}")
        self.expected = expected
        self.got = got


class TenantMismatch(MaitError):
    code = "tenant.mismatch"


class NonDeterminismError(MaitError):
    code = "execution.nondeterminism"


class BudgetExhausted(MaitError):
    code = "execution.budget_exhausted"


class ModelProviderError(MaitError):
    """Base for inference backend failures. ``retryable`` drives RETRY_WAIT vs run.failed."""

    code = "model.provider_error"
    retryable: bool = False


class ProviderUnavailable(ModelProviderError):
    """Connection refused/reset, DNS failure, or 5xx: the backend may come back."""

    code = "model.unavailable"
    retryable = True


class ProviderTimeout(ModelProviderError):
    code = "model.timeout"
    retryable = True


class ProviderRateLimited(ModelProviderError):
    code = "model.rate_limited"
    retryable = True

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderRequestRejected(ModelProviderError):
    """4xx other than 429: the request itself is wrong. Retrying cannot help."""

    code = "model.request_rejected"


class ProviderProtocolError(ModelProviderError):
    """The backend answered with something that is not a valid response."""

    code = "model.protocol_error"
