# ADR-019 — Open-weight models first, through one OpenAI-compatible adapter

| Field | Value |
|---|---|
| Status | **Accepted** (2026-11-02) |
| Decided by | Owner ("let's use some small open model") |
| Implemented in | `packages/mait-models`, `mait_core.models`, `infrastructure/docker` (profile `models`), `examples/open_model_quickstart.py` |

## Context

MAIT is open source and meant as a playground anyone can join. The default experience must
therefore work **without an API key, an account or a bill**: clone, `docker compose up`, run.
MAIT is also not a model provider (RFC-001 §2), so it must not embed or depend on one model.

## Decision

1. **MAIT never runs a model in-process.** It calls an inference server through the
   `ModelProvider` contract (`mait_core.models`).
2. **The first and default provider speaks the OpenAI-compatible Chat Completions protocol.**
   One adapter (`OpenAICompatibleProvider`) covers Ollama, llama.cpp `llama-server`, vLLM,
   SGLang, LM Studio, and hosted OpenAI-compatible endpoints. Closed-model providers (Anthropic,
   OpenAI, Gemini) come later as additional `ModelProvider` implementations; none is required.
3. **Default local server: Ollama**, pinned (`ollama/ollama:0.40.5`) in the `models` Compose profile.
4. **Default model family: Qwen3, Apache-2.0** (the same license as MAIT):

   | Tier | Model | Use |
   |---|---|---|
   | Tiny | `qwen3:0.6b` / `qwen3:1.7b` | Laptops without a GPU, smoke tests of a real server |
   | **Default** | **`qwen3:4b`** | Development and examples |
   | Better | `qwen3:8b` | Demos where tool-call quality matters |

   Why Qwen3: a permissive OSI-approved license, native tool calling, small sizes that run on a
   CPU, and multilingual training that covers Arabic and French (MAIT's first users are in MENA).

5. **A scripted provider (`ScriptedProvider`)** is the default for tests and the `mait-chaos`
   suite: deterministic responses and injected failures, no network, no model.

## Alternatives that also work (choice is configuration, not code)

| Model | License | Note |
|---|---|---|
| IBM Granite (small sizes) | Apache-2.0 | Built for tool calling and enterprise use |
| Microsoft Phi-4-mini | MIT | Strong small reasoning model |
| Hugging Face SmolLM3 | Apache-2.0 | Fully open training recipe |
| OpenAI gpt-oss-20b | Apache-2.0 | Much stronger, needs ~16 GB of memory |
| Meta Llama, Google Gemma | Custom licenses | Usable, but not OSI open source; never a MAIT default |

**Default-model rule:** a MAIT default must have an OSI-approved license, native tool calling,
and a size that runs on a laptop CPU. New model releases are evaluated against that rule. This
ADR was written without live access to the Ollama model library, so re-check the current tags
before the public release.

## Consequences

**Gains specific to open weights:**
- **Truly immutable agent versions (ADR-007).** A closed API can change what sits behind a model
  name; open weights have a content digest. The provider records it (`ModelInfo.weights_digest`,
  from Ollama's `/api/tags`), and the agent version spec pins it.
- **Reproducible evaluation (Phase 4).** With fixed weights, `temperature: 0` and a `seed`,
  repeated runs are close to deterministic on the same server and hardware. This is not
  guaranteed: batching and different GPU kernels can still change outputs.
- **Data never leaves the machine.** That matters for regulated and MENA clients.

**Costs and how MAIT handles them:**
- **Small models make more tool-call mistakes** (wrong tool, broken JSON). The adapter keeps
  broken arguments as evidence (`arguments=None`, `raw_arguments` kept), and the broker rejects
  them, so these mistakes become visible, measurable failures rather than crashes. That's useful
  data for the policy-distillation idea.
- **No standard token-count endpoint.** `count_input_tokens` returns `None`, and the runtime
  reserves with the byte-length upper bound (A6).
- **Dollar cost per token is ~0 locally.** Budgets must also be expressible in tokens and time
  (EXECUTION_MODEL §7). The price table may assign an operator-defined compute cost.
- **Tool-name constraint.** The protocol allows only `[A-Za-z0-9_-]{1,64}`. Dotted MAIT names are
  sent with `__` and mapped back per request; colliding names are rejected.
