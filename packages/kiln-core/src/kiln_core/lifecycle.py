"""Execution lifecycle: the normative state machine (EXECUTION_MODEL.md §2).

This module is the single source of truth for which transitions are legal and who may
perform them. The runtime, the API and the tests all import from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from kiln_core.errors import IllegalTransition, UnknownEventType


class ExecutionStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_TOOL = "WAITING_FOR_TOOL"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    RETRY_WAIT = "RETRY_WAIT"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def holds_lease(self) -> bool:
        """A lease exists only while RUNNING (amendment A1)."""
        return self is ExecutionStatus.RUNNING


class Writer(StrEnum):
    """Who is allowed to append an event.

    WORKER events are fenced: they require the execution to be RUNNING and the writer's
    lease generation to equal the current one. CONTROL events come from the control plane
    (scheduler, reaper, API, broker callbacks) and are serialized by the row lock instead.
    """

    WORKER = "worker"
    CONTROL = "control"


S = ExecutionStatus
TERMINAL_STATES = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED})
NON_TERMINAL_STATES = frozenset(s for s in S if s not in TERMINAL_STATES)


@dataclass(frozen=True)
class TransitionRule:
    next_status: ExecutionStatus
    writer: Writer
    bumps_generation: bool = False
    bumps_attempt: bool = False
    counts_recovery: bool = False


_W, _C = Writer.WORKER, Writer.CONTROL

TRANSITIONS: dict[tuple[ExecutionStatus, str], TransitionRule] = {
    (S.CREATED, "run.accepted"): TransitionRule(S.QUEUED, _C),
    (S.QUEUED, "worker.claimed"): TransitionRule(S.RUNNING, _C, bumps_generation=True),
    (S.RUNNING, "lease.expired"): TransitionRule(
        S.QUEUED, _C, bumps_generation=True, counts_recovery=True
    ),
    (S.RUNNING, "tool.deferred"): TransitionRule(S.WAITING_FOR_TOOL, _W),
    (S.WAITING_FOR_TOOL, "tool.resolved"): TransitionRule(S.QUEUED, _C),
    (S.WAITING_FOR_TOOL, "tool.unresolved"): TransitionRule(S.SUSPENDED, _C),
    (S.RUNNING, "approval.requested"): TransitionRule(S.WAITING_FOR_APPROVAL, _W),
    (S.WAITING_FOR_APPROVAL, "approval.granted"): TransitionRule(S.QUEUED, _C),
    (S.WAITING_FOR_APPROVAL, "approval.rejected"): TransitionRule(S.QUEUED, _C),
    (S.WAITING_FOR_APPROVAL, "approval.expired"): TransitionRule(S.QUEUED, _C),
    (S.RUNNING, "step.failed.retryable"): TransitionRule(S.RETRY_WAIT, _W),
    (S.RETRY_WAIT, "retry.due"): TransitionRule(S.QUEUED, _C, bumps_attempt=True),
    (S.RUNNING, "budget.exhausted"): TransitionRule(S.SUSPENDED, _W),
    (S.RUNNING, "nondeterminism.detected"): TransitionRule(S.SUSPENDED, _W),
    # Suspension of a RUNNING execution is cooperative: the worker applies it at a durable
    # boundary. Everywhere else the control plane applies it directly.
    (S.RUNNING, "run.suspended"): TransitionRule(S.SUSPENDED, _W),
    (S.QUEUED, "run.suspended"): TransitionRule(S.SUSPENDED, _C),
    (S.WAITING_FOR_TOOL, "run.suspended"): TransitionRule(S.SUSPENDED, _C),
    (S.WAITING_FOR_APPROVAL, "run.suspended"): TransitionRule(S.SUSPENDED, _C),
    (S.RETRY_WAIT, "run.suspended"): TransitionRule(S.SUSPENDED, _C),
    (S.SUSPENDED, "run.resumed"): TransitionRule(S.QUEUED, _C),
    (S.RUNNING, "run.completed"): TransitionRule(S.COMPLETED, _W),
    (S.RUNNING, "run.failed"): TransitionRule(S.FAILED, _W),
}
# Cancellation is allowed from every non-terminal state and fences any running worker (A4).
for _status in NON_TERMINAL_STATES:
    TRANSITIONS[(_status, "run.cancelled")] = TransitionRule(S.CANCELLED, _C, bumps_generation=True)

TRANSITION_EVENT_TYPES: frozenset[str] = frozenset(t for (_, t) in TRANSITIONS)

# Annotation events never change status (EXECUTION_MODEL.md §9).
ANNOTATION_EVENT_TYPES: dict[str, Writer] = {
    "step.started": _W,
    "step.completed": _W,
    "model.requested": _W,
    "model.completed": _W,
    "tool.invocation.pending": _W,
    "tool.invocation.submitted": _W,
    "tool.invocation.completed": _W,
    "tool.invocation.unknown": _W,
    "tool.invocation.denied": _W,
    "policy.evaluated": _W,
    "checkpoint.created": _W,
    "budget.reserved": _W,
    "budget.settled": _W,
    "log": _W,
    # Control-plane annotations may land on any execution, including terminal ones:
    # e.g. a side effect confirmed after the run was cancelled (A4).
    "tool.invocation.late_outcome": _C,
    "evaluation.recorded": _C,
}

assert not (TRANSITION_EVENT_TYPES & set(ANNOTATION_EVENT_TYPES)), "event type in both registries"


def is_transition_event(event_type: str) -> bool:
    if event_type in TRANSITION_EVENT_TYPES:
        return True
    if event_type in ANNOTATION_EVENT_TYPES:
        return False
    raise UnknownEventType(event_type)


def transition_rule(status: ExecutionStatus, event_type: str) -> TransitionRule:
    """Return the rule for applying ``event_type`` in ``status`` or raise IllegalTransition."""
    if not is_transition_event(event_type):
        raise IllegalTransition(status.value, event_type, "annotation events do not transition")
    rule = TRANSITIONS.get((status, event_type))
    if rule is None:
        raise IllegalTransition(status.value, event_type)
    return rule


def next_status(status: ExecutionStatus, event_type: str) -> ExecutionStatus:
    return transition_rule(status, event_type).next_status


def allowed_events(status: ExecutionStatus) -> frozenset[str]:
    return frozenset(t for (s, t) in TRANSITIONS if s is status)
