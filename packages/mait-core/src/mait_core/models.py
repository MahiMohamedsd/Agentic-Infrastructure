"""Model provider contracts (ADR-019).

MAIT never embeds a model. It talks to an inference server through a ``ModelProvider``.
The default implementation speaks the OpenAI-compatible Chat Completions protocol, which
Ollama, llama.cpp ``llama-server``, vLLM, SGLang and LM Studio all serve, so any open-weight
model those servers can load works with MAIT.

These types are provider-neutral and pure. Providers convert to and from their wire formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from mait_core.events import canonical_json

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None when the model emitted unparseable JSON
    raw_arguments: str  # exactly what the model produced; kept for evidence


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # set on role="tool" messages


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    max_output_tokens: int
    tools: tuple[ToolSpec, ...] = ()
    temperature: float | None = 0.0  # deterministic-leaning default for agents
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # provider-specific passthrough

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("a model request needs at least one message")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"  # hit max_output_tokens: output is truncated
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ModelInfo:
    """What actually served a call. Recorded on every ``model.completed`` event."""

    provider: str  # e.g. "openai_compatible"
    server: str | None  # e.g. "ollama", "llama.cpp", "vllm" when known
    requested_model: str
    served_model: str | None  # what the server reported
    weights_digest: str | None  # content digest of the weights, when the server exposes it


@dataclass(frozen=True)
class ModelResponse:
    message: Message
    finish_reason: FinishReason
    usage: Usage | None  # None if the server reported no usage
    info: ModelInfo
    raw: dict[str, Any]  # full provider response, stored verbatim as evidence


@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool = True
    token_counting: bool = False
    seed: bool = False
    weights_digest: bool = False


@runtime_checkable
class ModelProvider(Protocol):
    """Contract between the runtime and an inference backend.

    Rules every provider follows (EXECUTION_MODEL §5.4):
    * No internal retries. MAIT owns retries so each one is journaled and budgeted.
    * Errors are raised as ``ModelProviderError`` subclasses with ``retryable`` set.
    * ``count_input_tokens`` returns None when the backend cannot count exactly; the
      runtime then uses ``input_token_upper_bound``.
    """

    def capabilities(self) -> ModelCapabilities: ...

    async def generate(self, request: ModelRequest) -> ModelResponse: ...

    async def count_input_tokens(self, request: ModelRequest) -> int | None: ...


def input_token_upper_bound(request: ModelRequest) -> int:
    """Safe upper bound on input tokens without a tokenizer (amendment A6).

    Byte-level BPE tokenizers emit at least one byte per token, so the UTF-8 byte length of
    everything sent bounds the token count from above. A small per-message allowance covers
    chat-template tokens the server adds.
    """
    payload = {
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_calls": [[c.name, c.raw_arguments] for c in m.tool_calls],
                "tool_call_id": m.tool_call_id,
            }
            for m in request.messages
        ],
        "tools": [[t.name, t.description, t.parameters] for t in request.tools],
    }
    template_allowance = 16 * (len(request.messages) + len(request.tools)) + 64
    return len(canonical_json(payload)) + template_allowance
