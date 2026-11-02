"""OpenAI-compatible Chat Completions provider.

One adapter for every server that speaks ``POST {base_url}/chat/completions``: Ollama,
llama.cpp ``llama-server``, vLLM, SGLang, LM Studio, and hosted OpenAI-compatible
endpoints. This is how MAIT runs open-weight models without depending on any vendor SDK.

Behavior required by ``mait_core.models.ModelProvider``:
* no internal retries (httpx does not retry; we never loop);
* every failure maps to a ``ModelProviderError`` with ``retryable`` set;
* the full response body is kept in ``ModelResponse.raw`` for the journal.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from mait_core.errors import (
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderRequestRejected,
    ProviderTimeout,
    ProviderUnavailable,
)
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

_FINISH = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        server: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Args:
            base_url: API root including the version path, e.g. ``http://localhost:11434/v1``.
            model: model name as the server knows it, e.g. ``qwen3:4b``.
            server: ``"ollama"`` enables weights-digest lookup; other values are recorded only.
            api_key: for hosted endpoints. Resolve it from a ``secret://`` reference first;
                never put a raw key in an agent spec.
            transport: injectable for tests.
        """
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._server = server
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds, headers=headers, transport=transport
        )
        self._weights_digest: str | None = None
        self._digest_resolved = False

    async def aclose(self) -> None:
        await self._client.aclose()

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            tool_calling=True,
            token_counting=False,
            seed=True,
            weights_digest=self._server == "ollama",
        )

    async def count_input_tokens(self, request: ModelRequest) -> int | None:
        # The OpenAI-compatible protocol has no standard tokenize endpoint. The runtime
        # falls back to mait_core.models.input_token_upper_bound (amendment A6).
        return None

    # ------------------------------------------------------------------ generate

    async def generate(self, request: ModelRequest) -> ModelResponse:
        to_mait = {wire_tool_name(t.name): t.name for t in request.tools}
        if len(to_mait) != len(request.tools):
            raise ValueError("two tools map to the same wire name (e.g. 'a.b' and 'a__b')")
        body = self._encode(request)
        data = await self._post_json(f"{self._base_url}/chat/completions", body)
        return ModelResponse(
            message=_decode_message(data, to_mait),
            finish_reason=_decode_finish(data),
            usage=_decode_usage(data),
            info=ModelInfo(
                provider="openai_compatible",
                server=self._server,
                requested_model=self._model,
                served_model=data.get("model"),
                weights_digest=await self._resolve_weights_digest(),
            ),
            raw=data,
        )

    def _encode(self, request: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [_encode_message(m) for m in request.messages],
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": wire_tool_name(t.name),
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.seed is not None:
            body["seed"] = request.seed
        body.update(request.extra)
        return body

    async def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(url, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"model server timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"model server unreachable: {exc}") from exc
        _raise_for_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderProtocolError("model server returned non-JSON body") from exc
        if not isinstance(data, dict) or not data.get("choices"):
            raise ProviderProtocolError("model server response has no choices")
        return data

    async def _resolve_weights_digest(self) -> str | None:
        """Ollama exposes a content digest per local model; pin it as evidence (ADR-019)."""
        if self._digest_resolved or self._server != "ollama":
            return self._weights_digest
        self._digest_resolved = True
        root = self._base_url.removesuffix("/v1")
        try:
            response = await self._client.get(f"{root}/api/tags")
            response.raise_for_status()
            for entry in response.json().get("models", []):
                if self._model in (entry.get("name"), entry.get("model")):
                    digest = entry.get("digest")
                    self._weights_digest = f"ollama:{digest}" if digest else None
                    break
        except (httpx.HTTPError, ValueError, AttributeError):
            self._weights_digest = None  # evidence is best-effort; never fail the call
        return self._weights_digest


# ---------------------------------------------------------------------- wire mapping

_WIRE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def wire_tool_name(name: str) -> str:
    """MAIT tool names are dotted (``orders.lookup``); the OpenAI-compatible protocol allows
    only ``[A-Za-z0-9_-]{1,64}``. Dots become ``__``; the mapping is reversed per request."""
    wire = name.replace(".", "__")
    if not _WIRE_NAME.match(wire):
        raise ValueError(f"tool name {name!r} cannot be expressed on the wire")
    return wire


def _encode_message(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": wire_tool_name(c.name), "arguments": c.raw_arguments},
            }
            for c in m.tool_calls
        ]
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    return out


def _decode_message(data: dict[str, Any], to_mait: dict[str, str]) -> Message:
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderProtocolError("response choice has no message") from exc
    calls = []
    for i, call in enumerate(msg.get("tool_calls") or []):
        fn = call.get("function") or {}
        raw = fn.get("arguments", "")
        if isinstance(raw, dict):  # some servers send an object instead of a JSON string
            args, raw = raw, json.dumps(raw)
        else:
            try:
                parsed = json.loads(raw) if raw else {}
                args = parsed if isinstance(parsed, dict) else None
            except ValueError:
                args = None  # small models do emit broken JSON; the broker rejects it
        calls.append(
            ToolCall(
                id=call.get("id") or f"call_{i}",
                # Unknown names pass through unchanged; the broker rejects tools it doesn't know.
                name=to_mait.get(fn.get("name", ""), fn.get("name", "")),
                arguments=args,
                raw_arguments=raw if isinstance(raw, str) else str(raw),
            )
        )
    return Message(role="assistant", content=msg.get("content") or "", tool_calls=tuple(calls))


def _decode_finish(data: dict[str, Any]) -> FinishReason:
    choice = data["choices"][0]
    reason = _FINISH.get(choice.get("finish_reason") or "", FinishReason.OTHER)
    # Several servers report "stop" even when the message carries tool calls.
    if reason is FinishReason.STOP and (choice.get("message") or {}).get("tool_calls"):
        return FinishReason.TOOL_CALLS
    return reason


def _decode_usage(data: dict[str, Any]) -> Usage | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        return Usage(int(usage["prompt_tokens"]), int(usage["completion_tokens"]))
    except (KeyError, TypeError, ValueError):
        return None


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    detail = response.text[:500]
    if status == 429:
        retry_after = response.headers.get("retry-after")
        try:
            seconds = float(retry_after) if retry_after else None
        except ValueError:
            seconds = None
        raise ProviderRateLimited(f"rate limited: {detail}", seconds)
    if status >= 500:
        raise ProviderUnavailable(f"model server error {status}: {detail}")
    raise ProviderRequestRejected(f"model server rejected request ({status}): {detail}")
