"""Property tests for the execution state machine (RFC-001 §13, EXECUTION_MODEL.md §2.2).

Hypothesis generates arbitrary sequences of (event type, writer, presented generation) and
feeds them to the projection. Rejected events must leave state untouched; accepted events
must preserve every invariant.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mait_core.errors import MaitError, StaleLease
from mait_core.events import Event
from mait_core.lifecycle import (
    ANNOTATION_EVENT_TYPES,
    TERMINAL_STATES,
    TRANSITION_EVENT_TYPES,
    TRANSITIONS,
    Writer,
)
from mait_core.lifecycle import (
    ExecutionStatus as S,
)
from mait_core.projection import ExecutionState, apply

ORG, RUN = "org_test", "run_test"
ALL_TYPES = sorted(TRANSITION_EVENT_TYPES | set(ANNOTATION_EVENT_TYPES))

attempt_strategy = st.tuples(
    st.sampled_from(ALL_TYPES),
    st.sampled_from(list(Writer)),
    st.integers(min_value=-2, max_value=2),  # offset from current generation (0 = fresh)
)


def make_event(state: ExecutionState, etype: str, writer: Writer, gen_offset: int) -> Event:
    return Event(
        organization_id=ORG,
        execution_id=RUN,
        sequence=state.sequence + 1,
        type=etype,
        writer=writer,
        lease_generation=max(0, state.lease_generation + gen_offset),
    )


@settings(max_examples=600, deadline=None)
@given(st.lists(attempt_strategy, max_size=60))
def test_invariants_hold_under_arbitrary_event_streams(attempts):
    state = ExecutionState(ORG, RUN)
    for etype, writer, offset in attempts:
        before = state
        event = make_event(state, etype, writer, offset)
        try:
            state = apply(state, event)
        except MaitError:
            assert state == before  # rejected events never mutate (pure function)
            continue

        # Sequence is gap-free and strictly increasing.
        assert state.sequence == before.sequence + 1
        # Fencing token never decreases; attempt never decreases.
        assert state.lease_generation >= before.lease_generation
        assert state.attempt >= before.attempt
        # Terminal states never leave.
        if before.status in TERMINAL_STATES:
            assert state.status == before.status
        # Annotation events never change status.
        if etype in ANNOTATION_EVENT_TYPES:
            assert state.status == before.status
        # Worker events are accepted only with the exact current generation while RUNNING.
        if writer is Writer.WORKER:
            assert before.status is S.RUNNING
            assert event.lease_generation == before.lease_generation
        # Attempt changes only on retry.due.
        if state.attempt != before.attempt:
            assert etype == "retry.due"


@settings(max_examples=300, deadline=None)
@given(st.lists(attempt_strategy, max_size=40), st.integers(min_value=1, max_value=5))
def test_stale_worker_can_never_mutate_state(attempts, staleness):
    state = ExecutionState(ORG, RUN)
    for etype, writer, offset in attempts:
        try:
            state = apply(state, make_event(state, etype, writer, offset))
        except MaitError:
            pass
    if state.lease_generation < staleness:
        return
    for etype in ALL_TYPES:
        stale = Event(
            ORG, RUN, state.sequence + 1, etype, Writer.WORKER, state.lease_generation - staleness
        )
        with pytest.raises(MaitError):
            apply(state, stale)


def test_every_terminal_state_rejects_every_transition():
    for terminal in TERMINAL_STATES:
        state = ExecutionState(ORG, RUN, status=terminal, sequence=5)
        for etype in TRANSITION_EVENT_TYPES:
            for writer in Writer:
                with pytest.raises(MaitError):
                    apply(state, Event(ORG, RUN, 6, etype, writer, 0))


def test_every_non_terminal_state_is_cancellable_and_reachable():
    # Reachability: BFS over the table from CREATED.
    reachable, frontier = {S.CREATED}, [S.CREATED]
    while frontier:
        current = frontier.pop()
        for (src, _), rule in TRANSITIONS.items():
            if src is current and rule.next_status not in reachable:
                reachable.add(rule.next_status)
                frontier.append(rule.next_status)
    assert reachable == set(S)
    for status in S:
        if status not in TERMINAL_STATES:
            assert (status, "run.cancelled") in TRANSITIONS


def test_only_running_holds_lease_and_waiting_states_exit_via_queued():
    # Amendment A1: leaving any waiting state goes through QUEUED (re-claim, new generation).
    waiting = {S.WAITING_FOR_TOOL, S.WAITING_FOR_APPROVAL, S.RETRY_WAIT, S.SUSPENDED}
    for (src, etype), rule in TRANSITIONS.items():
        if src in waiting and rule.next_status not in TERMINAL_STATES:
            assert rule.next_status in {S.QUEUED, S.SUSPENDED}, (src, etype)
        if rule.next_status is S.RUNNING:
            assert etype == "worker.claimed" and rule.bumps_generation


def test_stale_worker_scenario_from_execution_model_4_4():
    s = ExecutionState(ORG, RUN)
    seq = iter(range(1, 100))

    def ev(t, w, g):
        return Event(ORG, RUN, next(seq), t, w, g)

    s = apply(s, ev("run.accepted", Writer.CONTROL, 0))
    s = apply(s, ev("worker.claimed", Writer.CONTROL, 0))
    worker_a_gen = s.lease_generation
    s = apply(s, ev("step.started", Writer.WORKER, worker_a_gen))
    # Worker A pauses; reaper fences; worker B claims.
    s = apply(s, ev("lease.expired", Writer.CONTROL, 0))
    s = apply(s, ev("worker.claimed", Writer.CONTROL, 0))
    assert s.recoveries == 1 and s.lease_generation == worker_a_gen + 2
    # Worker A wakes up and tries to complete the run.
    with pytest.raises(StaleLease):
        apply(s, Event(ORG, RUN, s.sequence + 1, "run.completed", Writer.WORKER, worker_a_gen))
    # Worker B completes it.
    s = apply(
        s, Event(ORG, RUN, s.sequence + 1, "run.completed", Writer.WORKER, s.lease_generation)
    )
    assert s.status is S.COMPLETED


def test_cancel_fences_running_worker_but_late_outcomes_are_still_recorded():
    s = ExecutionState(ORG, RUN, status=S.RUNNING, sequence=3, lease_generation=1)
    s = apply(s, Event(ORG, RUN, 4, "run.cancelled", Writer.CONTROL, 0))
    assert s.status is S.CANCELLED and s.lease_generation == 2
    with pytest.raises(MaitError):
        apply(s, Event(ORG, RUN, 5, "tool.invocation.completed", Writer.WORKER, 1))
    s = apply(s, Event(ORG, RUN, 5, "tool.invocation.late_outcome", Writer.CONTROL, 0))
    assert s.status is S.CANCELLED and s.sequence == 5


def test_retry_increments_attempt_but_recovery_does_not():
    s = ExecutionState(ORG, RUN, status=S.RUNNING, sequence=2, lease_generation=1)
    s = apply(s, Event(ORG, RUN, 3, "lease.expired", Writer.CONTROL, 0))
    assert s.attempt == 1 and s.recoveries == 1
    s = apply(s, Event(ORG, RUN, 4, "worker.claimed", Writer.CONTROL, 0))
    s = apply(s, Event(ORG, RUN, 5, "step.failed.retryable", Writer.WORKER, s.lease_generation))
    s = apply(s, Event(ORG, RUN, 6, "retry.due", Writer.CONTROL, 0))
    assert s.attempt == 2 and s.status is S.QUEUED


def test_sequence_gaps_and_duplicates_are_rejected():
    s = ExecutionState(ORG, RUN)
    with pytest.raises(MaitError):
        apply(s, Event(ORG, RUN, 2, "run.accepted", Writer.CONTROL, 0))
    s = apply(s, Event(ORG, RUN, 1, "run.accepted", Writer.CONTROL, 0))
    with pytest.raises(MaitError):
        apply(s, Event(ORG, RUN, 1, "run.accepted", Writer.CONTROL, 0))


def test_cross_tenant_event_rejected():
    s = ExecutionState(ORG, RUN)
    with pytest.raises(MaitError):
        apply(s, Event("org_other", RUN, 1, "run.accepted", Writer.CONTROL, 0))
