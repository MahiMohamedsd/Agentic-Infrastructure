# Architecture

**Status:** Normative for Phases 0–2. Source: [RFC-001](rfcs/RFC-001-mait-architecture.md) §0–3, §7,
§12, with amendments A9, A11 and A13.

## 1. Thesis

MAIT is the infrastructure layer that turns independent AI agents into manageable, observable and
controllable software systems. **We own the semantics; we reuse the mechanisms.**

| Own | Reuse | Integrate |
|---|---|---|
| Execution state machine, journal, scheduler semantics (leases, fencing), tool broker, policy enforcement point, agent lifecycle, evaluation semantics | PostgreSQL, S3-compatible storage, Docker/gVisor/Firecracker, OpenTelemetry, Cedar, MCP SDK, AWS | LangGraph, OpenAI Agents SDK, MCP servers, via adapters with declared capabilities |

## 2. Canonical objects

Every module speaks in four objects: **Agent**, **Agent Version** (immutable), **Execution**, and
**Evidence**. The **Agent Execution Envelope** (identity, configuration, state, control, evidence)
is the contract every execution is wrapped in. Several components implement it; no single table
holds it.

## 3. Processes (V0)

```
             ┌────────────────────────── mait server ───────────────────────────┐
 CLI / SDK ──► API (FastAPI) │ Execution Gateway │ Scheduler + Reaper │ SSE   │
             │  registry · policies · runs · approvals · evaluations           │
             └───────────────┬───────────────────────────────┬────────────────┘
                             │ SQL (system of record)        │ S3 API
                       ┌─────▼──────┐                  ┌─────▼──────┐
                       │ PostgreSQL │                  │ Object     │
                       │ journal,   │                  │ storage    │
                       │ projection,│                  │ payloads,  │
                       │ tasks      │                  │ checkpoints│
                       └─────▲──────┘                  └─────▲──────┘
                             │ claim / heartbeat / append    │
             ┌───────────────┴──── mait worker (N) ──────────┴────────────────┐
             │ Runtime engine · ctx primitives · Tool Broker · Policy PEP      │
             │ Budget enforcement · Adapters (native, langgraph, openai, mcp)  │
             └─────────────────────────────────────────────────────────────────┘
```

- **Two deployables** (A11): `mait server` and `mait worker`. The scheduler and the reaper run inside
  the server, singleton-elected through a PostgreSQL advisory lock.
- **Workers** reach PostgreSQL only through the runtime's write path (fenced appends). They never
  touch registry tables directly.
- **Splitting later:** each domain module exposes a Python interface (`mait_core.contracts` and the
  repository interfaces). Turning one into a network service changes the transport, not the domain.

## 4. Repository layout

```
apps/
  server/            # FastAPI app: API + gateway + scheduler/reaper   (Phase 1)
  worker/            # worker process                                  (Phase 1)
  dashboard/         # Next.js                                         (Phase 5)
  cli/               # `mait` CLI                                      (Phase 1)
packages/
  mait-core/         # contracts, lifecycle, events, projection — pure, no I/O  ← exists
  mait-runtime/      # engine, ctx primitives, leases, recovery, budgets          (Phase 1)
  mait-storage/      # PostgreSQL repositories, object storage, migrations runner (Phase 1)
  mait-tools/        # broker, registry, effects, idempotency, reconciliation     (Phase 2)
  mait-policy/       # PolicyEvaluator interface, mait-rules-v1, cedar backend    (Phase 3)
  mait-adapters/     # langgraph, openai-agents, mcp                              (Phase 2)
  mait-eval/         # scenarios, evaluators, Bradley–Terry, gates                (Phase 4)
  mait-observability/# OTel exporter fed from the journal                         (Phase 3)
  mait-sdk/          # public Python SDK (thin: registration, submit, stream)     (Phase 1)
infrastructure/
  migrations/        # explicit SQL; the ORM never defines the domain model
  docker/            # docker-compose for local dev
  aws/               # IaC (Phase 5)
docs/                # RFCs, ADRs, normative specs
examples/
```

A package gets its own directory only when work on it starts. Packages share one uv workspace and
publish separately only after their interfaces stabilize.

### 4.1 Dependency rules (enforced in CI later with import-linter)

```
mait-core      ← everything (depends on nothing)
mait-storage   ← mait-runtime, apps/server
mait-runtime   ← apps/worker, apps/server
mait-tools     ← mait-runtime
mait-policy    ← mait-tools
mait-adapters  ← mait-runtime          (adapters never import storage)
mait-eval      ← apps/server           (eval reads evidence via storage interfaces, never runtime internals)
```

## 5. Component ownership

| Component | Owns | Must not own |
|---|---|---|
| API Gateway | AuthN, routing, quotas | Agent execution |
| Agent Registry | Agent definitions and versions | Runtime state |
| Execution Gateway | Run admission (quotas, version resolution, budget validation) | Business tool logic |
| Runtime | Execution lifecycle and transitions (via `mait_core.lifecycle`) | Frontend concerns |
| Scheduler | Task claiming, leases, reaper, fairness | Agent reasoning |
| State Manager | Checkpoints and snapshots | Evaluation results |
| Tool Broker | Invocation protocol, effects, idempotency, reconciliation | Agent planning |
| Policy Engine | Authorization decisions (PDP) | Tool implementation |
| Event Journal | Durable ordered events | UI presentation |
| Telemetry | Traces, metrics, logs (derived) | Authoritative run state |
| Evaluation Engine | Datasets, evaluators, rankings, gate decisions | Production authorization |
| Deployment Manager | Release, canary, promotion, rollback | Model inference |

The journal is authoritative and telemetry is derived from it. A lost trace never makes an
execution unrecoverable.

## 6. Public API v1

Resource-oriented, JSON, versioned by path. Its versioning is independent of the event schema.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/agents` | Create agent |
| GET | `/v1/agents` | List agents (cursor) |
| GET | `/v1/agents/{id}` | Get agent |
| POST | `/v1/agents/{id}/versions` | Create immutable version |
| GET | `/v1/agents/{id}/versions` | List versions |
| POST | `/v1/agents/{id}/runs` | Start execution (`Idempotency-Key` header) |
| GET | `/v1/runs/{id}` | Get execution projection |
| POST | `/v1/runs/{id}/cancel` | Cancel |
| POST | `/v1/runs/{id}/suspend` | Suspend |
| POST | `/v1/runs/{id}/resume` | Resume (optionally with a raised budget) |
| GET | `/v1/runs/{id}/events` | History (JSON, cursor) or live stream (`Accept: text/event-stream`, `Last-Event-ID` = sequence) |
| GET | `/v1/runs/{id}/trace` | Trace view derived from the journal |
| POST | `/v1/tools` | Register tool definition |
| POST | `/v1/policies` | Create policy version |
| POST | `/v1/approvals/{id}/decision` | Approve or reject |
| POST | `/v1/evaluations` | Create evaluation run |
| GET | `/v1/evaluations/{id}` | Results and gate decision |

### 6.1 Conventions

- **Idempotency:** `POST /runs` with the same `Idempotency-Key` returns the original execution
  (backed by `UNIQUE (organization_id, idempotency_key)`).
- **Pagination:** opaque cursors. No offsets.
- **Request IDs:** `X-Request-Id` is echoed back and propagated into OTel.
- **SSE resumption:** the SSE event `id` is the journal `sequence`. A reconnect with
  `Last-Event-ID` resumes without gaps.

### 6.2 Errors

```json
{ "error": { "code": "execution.illegal_transition", "message": "…", "request_id": "req_…" } }
```

`code` values are stable and defined by `mait_core.errors`. Messages are not part of the contract.

## 7. SDK shape (Phase 1 target)

```python
from mait import MAIT, agent, tool

mait = MAIT(project="commerce")  # API key from env; never in code


@tool(name="orders.lookup", effect="read")
async def lookup_order(order_id: str) -> dict: ...


@agent(name="support-agent", tools=[lookup_order], max_steps=20, max_cost_usd=0.10)
async def support(ctx):
    order = await ctx.tool("orders.lookup", {"order_id": ctx.input["order_id"]})
    return await ctx.model.generate(messages=[...], context={"order": order})


run = await support.run({"order_id": "123"})
async for event in run.events():
    ...
```

Framework wrapping: `mait.integrations.langgraph.wrap(graph, name=..., policy=...)`. The adapter's
`AdapterCapabilities` decide the integration level, and the CLI prints the resulting guarantees.

## 8. Technology choices

| Area | Choice | ADR |
|---|---|---|
| Runtime and primary SDK | Python ≥ 3.12, asyncio | ADR-002 |
| API | FastAPI + Pydantic v2 | — |
| System of record | PostgreSQL ≥ 16 | ADR-004 |
| Queue | PostgreSQL `SKIP LOCKED` (V1); SQS/NATS later as transport only | ADR-005, A9 |
| Persistence layer | SQLAlchemy Core or psycopg 3 with explicit SQL; migrations are raw SQL | — |
| Object storage | S3 API (MinIO locally, optional) | — |
| Policy | `PolicyEvaluator` interface; built-in rules first, Cedar after a prototype | ADR-010, A13 |
| Telemetry | OpenTelemetry, GenAI semconv mapping versioned | ADR-011 |
| Dashboard | Next.js + TypeScript + Tailwind | Phase 5 |
| Packaging | uv workspace; Docker images | ADR-012 |
| Cloud | AWS: ECS Fargate, RDS, S3, Secrets Manager, KMS, ECR | ADR-014 |

Explicitly **not** in V1: Kafka, Kubernetes, Redis, Lambda as the execution substrate, Temporal as
the durability backend (A9), a universal memory system, a visual builder.
