"""Durable step matching for native recovery (EXECUTION_MODEL.md §5.2–5.3, amendment A5).

On recovery the agent function runs again from the top. Each durable primitive call is
matched against the recorded history. ``StepCursor`` implements that matching independently
of storage so the rules can be tested in isolation.

Concurrency (A5 as accepted): steps launched together (``asyncio.gather``) *complete* in a
nondeterministic order, but they are *allocated* in program order, because ``ctx.step(...)``
allocates its key and ordinal synchronously before returning an awaitable. Matching therefore
uses the allocation ordinal recorded in ``step.started``, never the journal position of
completions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kiln_core.errors import NonDeterminismError


class StepKind(StrEnum):
    STEP = "step"
    MODEL = "model"
    TOOL = "tool"
    APPROVAL = "approval"
    SLEEP = "sleep"


@dataclass(frozen=True)
class RecordedStep:
    ordinal: int  # allocation order within the execution (0, 1, 2, ...)
    key: str  # name#occurrence
    kind: StepKind
    args_digest: str | None
    completed: bool
    result: Any = None


class Decision(StrEnum):
    RETURN_RECORDED = "return_recorded"  # completed in history: do not execute
    RE_EXECUTE = "re_execute"  # started but interrupted (tools go through broker recovery)
    EXECUTE = "execute"  # beyond recorded history: live mode


@dataclass(frozen=True)
class Match:
    key: str
    ordinal: int
    decision: Decision
    recorded: RecordedStep | None


class StepCursor:
    """Allocates step keys in program order and matches them to recorded history.

    Rules:
    * The n-th allocation must match the step recorded with ordinal n (same key, same kind,
      and for tools the same argument digest); otherwise ``NonDeterminismError``.
    * Ordinals absent from history (beyond it, or a gap left by a crash between allocation and
      the ``step.started`` append) execute live. Intent-before-submit guarantees nothing left
      Kiln for an unrecorded tool call.
    * ``assert_fully_consumed()`` at the end of a run detects a code path that skipped
      recorded steps.
    """

    def __init__(self, history: list[RecordedStep]) -> None:
        self._by_ordinal: dict[int, RecordedStep] = {}
        for step in history:
            if step.ordinal in self._by_ordinal:
                raise ValueError(f"duplicate ordinal {step.ordinal} in history")
            self._by_ordinal[step.ordinal] = step
        self._last_recorded = max(self._by_ordinal, default=-1)
        self._allocated = 0
        self._occurrences: Counter[str] = Counter()

    @property
    def replaying(self) -> bool:
        return self._allocated <= self._last_recorded

    def next(self, name: str, kind: StepKind, args_digest: str | None = None) -> Match:
        if "#" in name:
            raise ValueError("step names must not contain '#'")
        ordinal = self._allocated
        self._allocated += 1
        occurrence = self._occurrences[name]
        self._occurrences[name] += 1
        key = f"{name}#{occurrence}"

        recorded = self._by_ordinal.get(ordinal)
        if recorded is None:
            return Match(key, ordinal, Decision.EXECUTE, None)
        if recorded.key != key or recorded.kind is not kind:
            raise NonDeterminismError(
                f"ordinal {ordinal}: code requested {kind.value} {key!r}, "
                f"history has {recorded.kind.value} {recorded.key!r}"
            )
        # Tools are matched on arguments too: replaying a different side effect under a
        # recorded result would be silently wrong.
        if kind is StepKind.TOOL and recorded.args_digest != args_digest:
            raise NonDeterminismError(f"{key!r}: tool arguments differ from recorded invocation")
        if recorded.completed:
            return Match(key, ordinal, Decision.RETURN_RECORDED, recorded)
        return Match(key, ordinal, Decision.RE_EXECUTE, recorded)

    def assert_fully_consumed(self) -> None:
        skipped = sorted(o for o in self._by_ordinal if o >= self._allocated)
        if skipped:
            keys = [self._by_ordinal[o].key for o in skipped]
            raise NonDeterminismError(f"run finished without reaching recorded steps {keys}")
