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
