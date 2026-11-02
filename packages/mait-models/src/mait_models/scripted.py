"""Scripted provider: deterministic responses and injected failures, no network.

Used by the ``mait-chaos`` suite and by anyone writing agent tests: script what the "model"
says, including tool calls, truncation, outages and garbage, then assert how MAIT behaves.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from typing import Any

from mait_core.models import (
    FinishReason,
    Message,
    ModelCapabilities,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)

Step = ModelResponse | BaseException


class ScriptExhausted(AssertionError):
    """The agent made more model calls than the test scripted."""


class ScriptedProvider:
    def __init__(self, steps: Iterable[Step] = (), *, model: str = "scripted") -> None:
        self._steps: deque[Step] = deque(steps)
        self._model = model
        self.requests: list[ModelRequest] = []

    def add(self, *steps: Step) -> ScriptedProvider:
        self._steps.extend(steps)
        return self

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(tool_calling=True, token_counting=True, seed=True)

    async def count_input_tokens(self, request: ModelRequest) -> int | None:
        return sum(len(m.content.split()) + 4 for m in request.messages)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._steps:
            raise ScriptExhausted(f"no scripted response left for call #{len(self.requests)}")
        step = self._steps.popleft()
        if isinstance(step, BaseException):
            raise step
        return step

    # ------------------------------------------------------------- step builders

    def _info(self) -> ModelInfo:
        return ModelInfo("scripted", None, self._model, self._model, "scripted:none")

    def text(self, content: str, *, output_tokens: int | None = None) -> ModelResponse:
        tokens = output_tokens if output_tokens is not None else len(content.split())
        return ModelResponse(
            message=Message(role="assistant", content=content),
            finish_reason=FinishReason.STOP,
            usage=Usage(10, tokens),
            info=self._info(),
            raw={"scripted": "text", "content": content},
        )

    def tool_call(self, name: str, arguments: dict[str, Any], *, call_id: str = "call_0"):
        raw = json.dumps(arguments, sort_keys=True)
        return ModelResponse(
            message=Message(
                role="assistant",
                tool_calls=(ToolCall(call_id, name, arguments, raw),),
            ),
            finish_reason=FinishReason.TOOL_CALLS,
            usage=Usage(10, 8),
            info=self._info(),
            raw={"scripted": "tool_call", "name": name, "arguments": arguments},
        )

    def truncated(self, content: str) -> ModelResponse:
        return ModelResponse(
            message=Message(role="assistant", content=content),
            finish_reason=FinishReason.LENGTH,
            usage=Usage(10, len(content.split())),
            info=self._info(),
            raw={"scripted": "truncated"},
        )
