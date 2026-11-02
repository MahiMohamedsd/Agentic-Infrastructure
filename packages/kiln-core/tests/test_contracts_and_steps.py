from __future__ import annotations

import pytest
from kiln_core.contracts import (
    GUARANTEES,
    AdapterCapabilities,
    Effect,
    IntegrationLevel,
    RecoveryAction,
    ToolDefinition,
    recovery_action,
)
from kiln_core.contracts import (
    ToolInvocationStatus as T,
)
from kiln_core.errors import NonDeterminismError, UnknownEventType
from kiln_core.events import Event, digest
from kiln_core.ids import kind_of, new_id
from kiln_core.lifecycle import Writer
from kiln_core.steps import Decision, RecordedStep, StepCursor, StepKind


def tool(effect: Effect, **kw) -> ToolDefinition:
    return ToolDefinition(name="x.y", version="1.0.0", effect=effect, input_schema={}, **kw)


class TestToolRecovery:
    def test_pending_is_safe_to_submit(self):
        assert recovery_action(tool(Effect.SIDE_EFFECT), T.PENDING) is RecoveryAction.SUBMIT

    def test_side_effect_without_idempotency_is_never_blindly_repeated(self):
        for status in (T.SUBMITTED, T.UNKNOWN):
            assert recovery_action(tool(Effect.SIDE_EFFECT), status) is RecoveryAction.ESCALATE
            assert recovery_action(tool(Effect.DESTRUCTIVE), status) is RecoveryAction.ESCALATE

    def test_reconciliation_preferred_over_resubmission(self):
        t = tool(Effect.SIDE_EFFECT, idempotency_supported=True, reconciliation_supported=True)
        assert recovery_action(t, T.UNKNOWN) is RecoveryAction.RECONCILE

    def test_provider_idempotency_allows_resubmit_with_same_key(self):
        t = tool(Effect.SIDE_EFFECT, idempotency_supported=True)
        assert recovery_action(t, T.SUBMITTED) is RecoveryAction.RESUBMIT_SAME_KEY

    def test_finished_outcomes_are_returned_as_recorded(self):
        for status in (T.CONFIRMED, T.REJECTED, T.DENIED):
            assert (
                recovery_action(tool(Effect.DESTRUCTIVE), status) is RecoveryAction.RETURN_RECORDED
            )

    def test_raw_secrets_rejected_and_destructive_requires_approval(self):
        with pytest.raises(ValueError):
            tool(Effect.READ, credentials_ref="sk-live-123")
        assert tool(Effect.DESTRUCTIVE).approval_required


class TestIntegrationLevels:
    def test_level_derived_from_capabilities_not_claims(self):
        assert AdapterCapabilities().integration_level() is IntegrationLevel.OBSERVED
        assert (
            AdapterCapabilities(tool_interception=True).integration_level()
            is IntegrationLevel.CONTROLLED
        )
        # Checkpointing without tool interception cannot be CHECKPOINTED: policies would be unenforced.
        assert (
            AdapterCapabilities(checkpointing=True, resume=True).integration_level()
            is IntegrationLevel.OBSERVED
        )
        full = AdapterCapabilities(True, True, True, True, True, True)
        assert full.integration_level() is IntegrationLevel.NATIVE

    def test_only_native_promises_side_effect_free_replay(self):
        assert [lvl for lvl, g in GUARANTEES.items() if g.replay_without_side_effects] == [
            IntegrationLevel.NATIVE
        ]


class TestStepCursor:
    def history(self):
        return [
            RecordedStep("lookup#0", StepKind.STEP, None, True, {"id": 1}),
            RecordedStep("llm#0", StepKind.MODEL, None, True, "hi"),
            RecordedStep("refund#0", StepKind.TOOL, digest({"amount": 10}), False),
        ]

    def test_replay_returns_recorded_then_reexecutes_interrupted_then_goes_live(self):
        c = StepCursor(self.history())
        assert c.next("lookup", StepKind.STEP).decision is Decision.RETURN_RECORDED
        assert c.next("llm", StepKind.MODEL).decision is Decision.RETURN_RECORDED
        m = c.next("refund", StepKind.TOOL, digest({"amount": 10}))
        assert m.decision is Decision.RE_EXECUTE and m.key == "refund#0"
        assert c.next("llm", StepKind.MODEL).key == "llm#1"
        assert not c.replaying

    def test_loops_get_distinct_keys(self):
        c = StepCursor([])
        assert [c.next("poll", StepKind.STEP).key for _ in range(3)] == [
            "poll#0",
            "poll#1",
            "poll#2",
        ]

    def test_code_change_detected(self):
        c = StepCursor(self.history())
        with pytest.raises(NonDeterminismError):
            c.next("something_else", StepKind.STEP)

    def test_changed_tool_arguments_detected(self):
        c = StepCursor(self.history())
        c.next("lookup", StepKind.STEP)
        c.next("llm", StepKind.MODEL)
        with pytest.raises(NonDeterminismError):
            c.next("refund", StepKind.TOOL, digest({"amount": 9999}))


class TestEventsAndIds:
    def test_ids_are_prefixed_sortable_and_parseable(self):
        a, b = new_id("execution", now_ms=1), new_id("execution", now_ms=2)
        assert a.startswith("run_") and a < b and kind_of(a) == "execution"
        with pytest.raises(ValueError):
            kind_of("run_short")

    def test_unknown_event_type_rejected(self):
        with pytest.raises(UnknownEventType):
            Event("o", "r", 1, "run.teleported", Writer.CONTROL, 0)

    def test_large_payload_must_be_offloaded(self):
        with pytest.raises(ValueError):
            Event("o", "r", 1, "log", Writer.WORKER, 0, payload={"x": "a" * 20_000})
        Event("o", "r", 1, "log", Writer.WORKER, 0, payload={}, payload_ref="s3://b/k")

    def test_digest_is_key_order_independent(self):
        assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
