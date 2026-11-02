"""Durable step matching for native recovery (EXECUTION_MODEL.md §5.2–5.3, amendment A5).

On recovery the agent function runs again from the top. Each durable primitive call is
matched, in order, against the recorded history. ``StepCursor`` implements that matching
independently of storage so the rules can be tested in isolation.
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
    decision: Decision
    recorded: RecordedStep | None


class StepCursor:
    def __init__(self, history: list[RecordedStep]) -> None:
        self._history = history
        self._position = 0
        self._occurrences: Counter[str] = Counter()

    @property
    def replaying(self) -> bool:
        return self._position < len(self._history)

    def next(self, name: str, kind: StepKind, args_digest: str | None = None) -> Match:
        if "#" in name:
            raise ValueError("step names must not contain '#'")
        occurrence = self._occurrences[name]
        self._occurrences[name] += 1
        key = f"{name}#{occurrence}"

        if not self.replaying:
            self._position += 1
            return Match(key, Decision.EXECUTE, None)

        recorded = self._history[self._position]
        self._position += 1
        if recorded.key != key or recorded.kind is not kind:
            raise NonDeterminismError(
                f"position {self._position - 1}: code requested {kind.value} {key!r}, "
                f"history has {recorded.kind.value} {recorded.key!r}"
            )
        # Tools are matched on arguments too: replaying a different side effect under a
        # recorded result would be silently wrong.
        if kind is StepKind.TOOL and recorded.args_digest != args_digest:
            raise NonDeterminismError(f"{key!r}: tool arguments differ from recorded invocation")
        if recorded.completed:
            return Match(key, Decision.RETURN_RECORDED, recorded)
        return Match(key, Decision.RE_EXECUTE, recorded)
