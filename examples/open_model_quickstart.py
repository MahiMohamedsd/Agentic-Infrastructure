"""Talk to a local open-weight model through MAIT's provider, including one tool call.

    docker compose -f infrastructure/docker/docker-compose.yml --profile models up -d
    docker compose -f infrastructure/docker/docker-compose.yml exec ollama ollama pull qwen3:4b
    uv run python examples/open_model_quickstart.py

Override with MAIT_MODEL_BASE_URL / MAIT_MODEL / MAIT_MODEL_SERVER to use llama.cpp, vLLM,
LM Studio or any OpenAI-compatible server.
"""

import asyncio
import json
import os

from mait_core.models import Message, ModelRequest, ToolSpec
from mait_models import OpenAICompatibleProvider

ORDERS = {"42": {"status": "shipped", "carrier": "DHL", "eta": "2026-11-05"}}

LOOKUP = ToolSpec(
    name="orders.lookup",  # sent as "orders__lookup" on the wire
    description="Look up an order by its id and return its status.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
        "additionalProperties": False,
    },
)


async def main() -> None:
    provider = OpenAICompatibleProvider(
        base_url=os.environ.get("MAIT_MODEL_BASE_URL", "http://localhost:11434/v1"),
        model=os.environ.get("MAIT_MODEL", "qwen3:4b"),
        server=os.environ.get("MAIT_MODEL_SERVER", "ollama"),
    )
    messages = [
        Message("system", "You are a support agent. Use tools to answer. Be brief."),
        Message("user", "Where is my order 42?"),
    ]
    try:
        for _ in range(4):  # a tiny agent loop; the real one lives in mait-runtime
            response = await provider.generate(
                ModelRequest(tuple(messages), max_output_tokens=512, tools=(LOOKUP,), seed=7)
            )
            print(
                f"[{response.finish_reason}] served by {response.info.served_model} "
                f"weights={response.info.weights_digest} usage={response.usage}"
            )
            messages.append(response.message)
            if not response.message.tool_calls:
                print("\nAgent:", response.message.content)
                return
            for call in response.message.tool_calls:
                if call.arguments is None:
                    result = {"error": "arguments were not valid JSON"}
                else:
                    result = ORDERS.get(str(call.arguments.get("order_id")), {"error": "not found"})
                print(f"  tool {call.name}({call.raw_arguments}) -> {result}")
                messages.append(Message("tool", json.dumps(result), tool_call_id=call.id))
        print("Stopped: step limit reached.")
    finally:
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
