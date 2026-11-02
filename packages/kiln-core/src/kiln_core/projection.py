"""Pure execution projection: fold journal events into current execution state.

The PostgreSQL write path (EXECUTION_MODEL.md §3) performs exactly these checks inside one
transaction under ``SELECT ... FOR UPDATE``. Keeping the rules in a pure function lets us
property-test them exhaustively and reuse them for replay and for consumers that rebuild
state from the journal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from kiln_core.errors import (
    IllegalTransition,
    LeaseNotHeld,
    SequenceConflict,
    StaleLease,
    TenantMismatch,
    WriterNotAllowed,
)
from kiln_core.events import Event
from kiln_core.lifecycle import (
    ANNOTATION_EVENT_TYPES,
    ExecutionStatus,
    Writer,
    transition_rule,
)


@dataclass(frozen=True)
class ExecutionState:
    organization_id: str
    execution_id: str
    status: ExecutionStatus = ExecutionStatus.CREATED
    sequence: int = 0
    lease_generation: int = 0
    attempt: int = 1
    recoveries: int = 0
    last_event_id: str | None = None


def _check_writer(state: ExecutionState, event: Event, expected: Writer) -> None:
    if event.writer is not expected:
        raise WriterNotAllowed(
            f"{event.type!r} in {state.status.value} must be written by {expected.value}, "
            f"not {event.writer.value}"
        )
    if event.writer is Writer.WORKER:
        if not state.status.holds_lease:
            raise LeaseNotHeld(f"worker event {event.type!r} while {state.status.value}")
        if event.lease_generation != state.lease_generation:
            raise StaleLease(event.lease_generation, state.lease_generation)


def apply(state: ExecutionState, event: Event) -> ExecutionState:
    """Apply one event. Raises without mutating anything if the event is not admissible.

    Check order matters and mirrors the database path: tenant → sequence → transition
    legality → writer/fencing.
    """
    if (event.organization_id, event.execution_id) != (state.organization_id, state.execution_id):
        raise TenantMismatch("event does not belong to this execution")
    if event.sequence != state.sequence + 1:
        raise SequenceConflict(state.sequence + 1, event.sequence)

    if event.is_transition:
        if state.status.is_terminal:
            raise IllegalTransition(state.status.value, event.type, "execution is terminal")
        rule = transition_rule(state.status, event.type)
        _check_writer(state, event, rule.writer)
        return replace(
            state,
            status=rule.next_status,
            sequence=event.sequence,
            lease_generation=state.lease_generation + (1 if rule.bumps_generation else 0),
            attempt=state.attempt + (1 if rule.bumps_attempt else 0),
            recoveries=state.recoveries + (1 if rule.counts_recovery else 0),
            last_event_id=event.event_id,
        )

    _check_writer(state, event, ANNOTATION_EVENT_TYPES[event.type])
    return replace(state, sequence=event.sequence, last_event_id=event.event_id)


def fold(state: ExecutionState, events: Iterable[Event]) -> ExecutionState:
    for event in events:
        state = apply(state, event)
    return state
