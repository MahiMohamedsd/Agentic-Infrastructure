"""Provider tests against an in-process fake server (httpx.MockTransport).

These verify MAIT's mapping of the OpenAI-compatible wire format. They do not verify any
particular server's behavior; run examples/open_model_quickstart.py against a real server
for that.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
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
    ModelProvider,
    ModelRequest,
    ToolCall,
    ToolSpec,
    input_token_upper_bound,
)
from mait_models import OpenAICompatibleProvider, ScriptedProvider
from mait_models.openai_compat import wire_tool_name
from mait_models.scripted import ScriptExhausted

LOOKUP = ToolSpec(
    "orders.lookup",
    "Look up an order",
    {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
)
REQ = ModelRequest(
    messages=(Message("system", "You are support."), Message("user", "Where is order 42?")),
    max_output_tokens=256,
    tools=(LOOKUP,),
    seed=7,
)


def completion(message: dict, finish: str = "stop", usage: dict | None = None) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "qwen3:4b",
        "choices": [
            {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}
        ],
        "usage": usage if usage is not None else {"prompt_tokens": 31, "completion_tokens": 9},
    }


def provider(handler, **kw) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="http://models.local/v1",
        model="qwen3:4b",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def run(coro):
    return asyncio.run(coro)


class TestWireFormat:
    def test_request_body_shape(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=completion({"content": "hi"}))

        run(provider(handler).generate(REQ))
        body = seen["body"]
        assert seen["url"] == "http://models.local/v1/chat/completions"
        assert body["model"] == "qwen3:4b" and body["stream"] is False
        assert body["max_tokens"] == 256 and body["temperature"] == 0.0 and body["seed"] == 7
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["tools"][0] == {
            "type": "function",
            "function": {
                "name": "orders__lookup",
                "description": "Look up an order",
                "parameters": LOOKUP.parameters,
            },
        }

    def test_tool_round_trip_messages_are_encoded(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=completion({"content": "Shipped."}))

        call = ToolCall("call_1", "orders.lookup", {"order_id": "42"}, '{"order_id": "42"}')
        req = ModelRequest(
            messages=(
                Message("user", "Where is 42?"),
                Message("assistant", tool_calls=(call,)),
                Message("tool", '{"status": "shipped"}', tool_call_id="call_1"),
            ),
            max_output_tokens=64,
        )
        run(provider(handler).generate(req))
        msgs = seen["body"]["messages"]
        assert msgs[1]["tool_calls"][0]["function"] == {
            "name": "orders__lookup",
            "arguments": '{"order_id": "42"}',
        }
        assert msgs[2]["tool_call_id"] == "call_1" and "tools" not in seen["body"]


class TestResponseParsing:
    def test_text_response_and_usage(self):
        r = run(
            provider(lambda _: httpx.Response(200, json=completion({"content": "Hello"}))).generate(
                REQ
            )
        )
        assert r.message.content == "Hello" and r.finish_reason is FinishReason.STOP
        assert (r.usage.input_tokens, r.usage.output_tokens) == (31, 9)
        assert r.info.served_model == "qwen3:4b" and r.raw["id"] == "chatcmpl-1"

    def test_tool_call_parsed_and_stop_normalized_to_tool_calls(self):
        msg = {
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "orders__lookup", "arguments": '{"order_id":"42"}'},
                }
            ],
        }
        r = run(provider(lambda _: httpx.Response(200, json=completion(msg, "stop"))).generate(REQ))
        assert r.finish_reason is FinishReason.TOOL_CALLS
        (call,) = r.message.tool_calls
        assert call.name == "orders.lookup" and call.arguments == {"order_id": "42"}

    def test_broken_tool_json_from_small_model_is_kept_not_crashed(self):
        msg = {
            "tool_calls": [
                {"id": "c1", "function": {"name": "orders.lookup", "arguments": '{"order_id": 42'}}
            ]
        }
        r = run(
            provider(lambda _: httpx.Response(200, json=completion(msg, "tool_calls"))).generate(
                REQ
            )
        )
        (call,) = r.message.tool_calls
        assert call.arguments is None and call.raw_arguments == '{"order_id": 42'

    def test_object_arguments_and_missing_ids_are_tolerated(self):
        msg = {
            "tool_calls": [{"function": {"name": "orders.lookup", "arguments": {"order_id": "7"}}}]
        }
        r = run(
            provider(lambda _: httpx.Response(200, json=completion(msg, "tool_calls"))).generate(
                REQ
            )
        )
        (call,) = r.message.tool_calls
        assert call.id == "call_0" and call.arguments == {"order_id": "7"}
        assert json.loads(call.raw_arguments) == {"order_id": "7"}

    def test_length_finish_and_missing_usage(self):
        data = completion({"content": "partial"}, "length")
        del data["usage"]
        r = run(provider(lambda _: httpx.Response(200, json=data)).generate(REQ))
        assert r.finish_reason is FinishReason.LENGTH and r.usage is None


class TestToolNames:
    def test_dotted_names_translated_both_ways(self):
        assert wire_tool_name("orders.lookup") == "orders__lookup"
        msg = {
            "tool_calls": [{"id": "c", "function": {"name": "orders__lookup", "arguments": "{}"}}]
        }
        r = run(
            provider(lambda _: httpx.Response(200, json=completion(msg, "tool_calls"))).generate(
                REQ
            )
        )
        assert r.message.tool_calls[0].name == "orders.lookup"

    def test_unknown_tool_name_passes_through_for_the_broker_to_reject(self):
        msg = {"tool_calls": [{"id": "c", "function": {"name": "rm_rf", "arguments": "{}"}}]}
        r = run(
            provider(lambda _: httpx.Response(200, json=completion(msg, "tool_calls"))).generate(
                REQ
            )
        )
        assert r.message.tool_calls[0].name == "rm_rf"

    def test_colliding_wire_names_rejected(self):
        req = ModelRequest(
            messages=(Message("user", "x"),),
            max_output_tokens=8,
            tools=(ToolSpec("a.b", "", {}), ToolSpec("a__b", "", {})),
        )
        with pytest.raises(ValueError):
            run(provider(lambda _: httpx.Response(200)).generate(req))

    def test_unrepresentable_name_rejected(self):
        with pytest.raises(ValueError):
            wire_tool_name("orders lookup!")


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,error,retryable",
        [
            (429, ProviderRateLimited, True),
            (500, ProviderUnavailable, True),
            (503, ProviderUnavailable, True),
            (400, ProviderRequestRejected, False),
            (404, ProviderRequestRejected, False),
        ],
    )
    def test_status_codes(self, status, error, retryable):
        p = provider(lambda _: httpx.Response(status, text="nope", headers={"retry-after": "3"}))
        with pytest.raises(error) as info:
            run(p.generate(REQ))
        assert info.value.retryable is retryable
        if status == 429:
            assert info.value.retry_after_seconds == 3.0

    def test_connection_refused_is_retryable_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(ProviderUnavailable):
            run(provider(handler).generate(REQ))

    def test_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(ProviderTimeout):
            run(provider(handler).generate(REQ))

    @pytest.mark.parametrize("body", [b"<html>oops</html>", b'{"choices": []}', b"[]"])
    def test_garbage_bodies_are_protocol_errors(self, body):
        with pytest.raises(ProviderProtocolError):
            run(provider(lambda _: httpx.Response(200, content=body)).generate(REQ))

    def test_no_internal_retries(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)

        with pytest.raises(ProviderUnavailable):
            run(provider(handler).generate(REQ))
        assert len(calls) == 1  # MAIT owns retries (RETRY_WAIT), never the provider


class TestOllamaWeightsDigest:
    def test_digest_resolved_once_and_recorded(self):
        tags_calls = []

        def handler(request):
            if request.url.path == "/api/tags":
                tags_calls.append(1)
                return httpx.Response(
                    200, json={"models": [{"name": "qwen3:4b", "digest": "abc123"}]}
                )
            return httpx.Response(200, json=completion({"content": "ok"}))

        p = provider(handler, server="ollama")
        first, second = run(_twice(p))
        assert first.info.weights_digest == "ollama:abc123" == second.info.weights_digest
        assert len(tags_calls) == 1 and p.capabilities().weights_digest

    def test_digest_lookup_failure_never_fails_the_call(self):
        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(500)
            return httpx.Response(200, json=completion({"content": "ok"}))

        r = run(provider(handler, server="ollama").generate(REQ))
        assert r.message.content == "ok" and r.info.weights_digest is None


async def _twice(p):
    return await p.generate(REQ), await p.generate(REQ)


class TestScriptedProvider:
    def test_scripted_sequence_with_injected_outage(self):
        s = ScriptedProvider()
        s.add(
            s.tool_call("orders.lookup", {"order_id": "42"}),
            ProviderUnavailable("down"),
            s.text("Shipped."),
        )
        first = run(s.generate(REQ))
        assert first.finish_reason is FinishReason.TOOL_CALLS
        with pytest.raises(ProviderUnavailable):
            run(s.generate(REQ))
        assert run(s.generate(REQ)).message.content == "Shipped."
        assert len(s.requests) == 3 and s.remaining == 0
        with pytest.raises(ScriptExhausted):
            run(s.generate(REQ))

    def test_both_providers_satisfy_the_protocol(self):
        assert isinstance(ScriptedProvider(), ModelProvider)
        assert isinstance(provider(lambda _: httpx.Response(200)), ModelProvider)


class TestUpperBound:
    def test_upper_bound_dominates_real_token_counts_for_ascii_and_arabic(self):
        # Every byte-level BPE token covers >= 1 byte, so bytes bound tokens from above.
        for text in ["Where is my order?", "أين طلبي؟ " * 50, "Où est ma commande ?"]:
            req = ModelRequest(messages=(Message("user", text),), max_output_tokens=10)
            assert input_token_upper_bound(req) >= len(text.encode("utf-8"))
