# RFC 001 — Kiln Architecture

| Field | Value |
|---|---|
| Status | **Draft v0.1** — architectural baseline |
| Model | Open-source first |
| Owner | Mohamed Seddik Mahi |
| Normative companions | [ARCHITECTURE](../ARCHITECTURE.md) · [EXECUTION_MODEL](../EXECUTION_MODEL.md) · [DATA_MODEL](../DATA_MODEL.md) · [SECURITY](../SECURITY.md) · [ROADMAP](../ROADMAP.md) · [ADR index](../adr/README.md) |

> **Kiln — The Operating Infrastructure for Autonomous AI Systems**
>
> Mission: build an open, extensible infrastructure platform that makes autonomous AI systems
> **executable, manageable, observable, recoverable, governable and verifiable**.

This is an engineering RFC, not a pitch. It defines what we build, what we do not build,
architectural boundaries, internal contracts, entities, API interfaces, execution semantics,
failure handling, security boundaries, deployment, milestones and acceptance criteria.

Where this RFC and a companion document disagree, **the companion document wins**. Those
documents incorporate the review amendments in [Appendix A](#appendix-a--review-amendments-v01--v02).

---

## 0. Executive decision

**Thesis.** Kiln is an independent agent operations platform with its own execution engine,
agent integration layer, governance system and verification infrastructure.

| Verb | Scope |
|---|---|
| **Own** | Agent execution semantics, state machine, execution journal, tool broker, policy enforcement, agent lifecycle |
| **Reuse** | Database engines, container runtimes, cryptographic primitives, telemetry standards, cloud infrastructure, selected distributed-systems primitives |
| **Integrate** | Existing agent frameworks through adapters, without forcing developers to rewrite agents |
| **Unify** | Every capability around a common agent identity, immutable version, execution record and behavioral contract |

**Positioning.** Kiln is the infrastructure layer that turns independent AI agents into
manageable, observable and controllable software systems.

**Governing principle.** *We own the semantics. We don't reinvent every underlying mechanism.*

## 1. Decisions under challenge

| Decision | Choice | Verdict |
|---|---|---|
| Runtime ownership | Build our own | Keep, with boundaries |
| Integration | Wrap existing agents | Keep |
| Technical identity | Unified platform | Keep, define a central abstraction |

### 1.1 Own the *agent* runtime, not distributed-systems infrastructure

**A. Agent runtime (owned):** agent identities, versions, execution state, model interactions,
tool calls, context/memory references, budgets, policies, human approvals, execution events,
recovery semantics.

**B. Distributed-systems infrastructure (reused):** scheduling at scale, worker coordination,
durable timers, queue delivery, process recovery, replication, leader election, cluster management.

Decision: **build Kiln Runtime, not a Kiln distributed database or container orchestrator.**
PostgreSQL holds transactional state, Docker isolates processes. Backends sit behind Kiln
interfaces so they can be replaced without changing the public SDK. (Temporal as a backend: see
amendment A9.)

### 1.2 Wrapping existing agents — integration levels

The outer infrastructure cannot recover a framework's internal state unless the framework exposes
checkpoint/resume. Guarantees are therefore **per integration level**:

| Level | Capability | Recovery guarantee |
|---|---|---|
| 0 — Observed | Track execution and metadata | None internal |
| 1 — Controlled | Intercept tools, enforce policies | Only at exposed boundaries |
| 2 — Checkpointed | Persist framework checkpoints | Resume from supported checkpoints |
| 3 — Native | Kiln execution primitives | Full Kiln-managed lifecycle |

**We never claim an arbitrary agent can be safely resumed from any point.**

### 1.3 Unified ≠ monolithic

Four canonical objects; every module operates on them:

1. **Agent** — a logical autonomous system.
2. **Agent Version** — an immutable definition of that system.
3. **Execution** — one invocation of an agent version.
4. **Evidence** — recorded events, outcomes and evaluations associated with executions.

## 2. Product boundaries

**Kiln is:** an agent execution infrastructure · lifecycle management platform · framework
integration layer · controlled tool execution system · durable execution system · agent
observability platform · evaluation and verification platform · optimization and deployment platform.

**Kiln is not:** a model provider · a replacement for Claude/GPT/Gemini · a general cloud provider ·
a generic workflow builder · a no-code chatbot builder · a replacement for every framework · a
proprietary programming language.

### 2.1 The Agent Execution Envelope (AEE)

The logical operational boundary around every execution. It is a **contract implemented by several
components**, not a row in a table.

| Facet | Contents |
|---|---|
| Identity | Tenant, agent, version, environment, execution ID |
| Configuration | Model, tools, policies, framework adapter, resource limits |
| State | Execution status, checkpoint, context references |
| Control | Permissions, budgets, approvals, cancellation |
| Evidence | Events, traces, tool results, metrics, outcome |

## 3. System architecture

Modular control plane + separately scalable execution plane. Not 15 microservices.

- One modular control-plane application.
- One independent worker service.
- One PostgreSQL database initially; one object store.
- A separately deployable execution sandbox.
- Explicit interfaces between every domain, so services can be split later without redesigning the domain model.

```
                         DEVELOPER / USER
                                |
                   CLI / Python SDK / TypeScript SDK
                                |
                         REST API / SSE
                                |
                    +-----------------------+
                    |     CONTROL PLANE     |
                    | Organization / Tenant |
                    | Agent + Version Reg.  |
                    | Policy Management     |
                    | Run Management        |
                    | Evaluation Mgmt       |
                    | Deployment Mgmt       |
                    +-----------+-----------+
                                |  internal domain APIs
              +-----------------+-----------------+
              |                                   |
      +-------v--------+                  +-------v--------+
      | Execution      |                  | Event /        |
      | Gateway        |                  | Telemetry      |
      +-------+--------+                  +----------------+
              |
      +-------v---------------------------------------+
      |               KILN RUNTIME                    |
      | Run State Machine · Scheduler / Lease Manager |
      | Context & Checkpoint Manager · Budgets        |
      | Tool Broker · Policy Enforcement              |
      | Recovery & Retry Engine                       |
      +----------------------+------------------------+
                             |  durable work queue (PostgreSQL)
                +------------+------------+
          +-----v------+            +-----v------+
          | Worker 1   |   ...      | Worker N   |
          +-----+------+            +-----+------+
                +------------+------------+
                     Framework adapters
            LangGraph · OpenAI Agents SDK · Native SDK
                             |
                        TOOL BROKER
                             |
                External APIs / MCP / DBs

         PostgreSQL + Object Storage + Telemetry backend
```

The diagram shows logical relationships, not processes in the first release.

### 3.1 Component ownership

| Component | Owns | Must not own |
|---|---|---|
| API Gateway | AuthN, routing, quotas | Agent execution |
| Agent Registry | Agent definitions and versions | Runtime state |
| Execution Gateway | Run admission | Business tool logic |
| Runtime | Execution lifecycle and transitions | Frontend concerns |
| Scheduler | Worker assignment and leases | Agent reasoning |
| State Manager | Checkpoints and snapshots | Evaluation results |
| Tool Broker | Tool execution and effect controls | Agent planning |
| Policy Engine | Authorization decisions | Tool implementation |
| Event Journal | Durable ordered execution events | UI presentation |
| Telemetry | Traces, metrics, logs | Authoritative run state |
| Evaluation Engine | Scenarios and results | Production authorization |
| Deployment Manager | Release and promotion | Direct model inference |

**The journal is authoritative; telemetry is analytical.** Losing a trace must never mean losing
the ability to recover an execution.

### 3.2 Repository layout

See [ARCHITECTURE.md §4](../ARCHITECTURE.md#4-repository-layout) (amended by A11).

## 4. Canonical data model (summary)

PostgreSQL is the initial system of record. Entities: `organizations`, `projects`, `agents`,
`agent_versions`, `deployments`, `executions`, `execution_events`, `checkpoints`,
`tool_definitions`, `tool_invocations`, `policies`, `approval_requests`, `evaluation_datasets`,
`evaluation_runs`, `evaluation_results`. Full definitions: [DATA_MODEL.md](../DATA_MODEL.md).

Key rules:
- **Agent versions are immutable** and reference immutable digests: agent source or image digest,
  SDK/runtime compatibility version, tool definition versions, policy version, model provider and
  requested model identifier, inference parameters, dependency lockfile or image digest. A provider
  may change what sits behind a model alias; Kiln records the request and the provider-reported
  model, and never promises alias stability.
- **The execution row is a projection; the journal is the authoritative transition history.** The
  event append and the projection update happen in one transaction.
- **Journal rules:** append-only · per-execution monotonic sequence · transactional with projection ·
  duplicate delivery tolerated · consumers keep their own cursors · large payloads in object storage ·
  sensitive content redacted/encrypted per policy.

## 5. Execution engine (summary)

Canonical lifecycle: `CREATED, QUEUED, RUNNING, WAITING_FOR_TOOL, WAITING_FOR_APPROVAL,
RETRY_WAIT, SUSPENDED, COMPLETED, FAILED, CANCELLED`. **Suspended** is resumable;
**cancelled** is terminal; **failed** may be eligible for a *new* execution.
The normative transition table is in [EXECUTION_MODEL.md](../EXECUTION_MODEL.md) (amended by A1–A3).

Execution walk-through: admission → creation (`run.accepted`) → scheduling → worker invokes agent or
adapter → model interaction recorded with provider metadata and usage → tool request validated by
Tool Broker → Policy Engine evaluates principal/action/resource/context → tool executes, outcome
recorded → agent returns, output and final state recorded.

**Workers** are stateless between tasks: claim → load state → hold a time-limited lease → execute an
authorized unit of work → persist events and checkpoint → renew/release. **Every state-changing
write validates the lease generation (fencing token).** Without this, a worker that paused and lost
its lease could overwrite newer state.

**V0 scheduler:** PostgreSQL task queue with transactional enqueue, `FOR UPDATE SKIP LOCKED`, lease
expiry, retry scheduling, dead-lettering, heartbeats, fencing. No Kafka, no Kubernetes. The queue is
never the source of truth.

## 6. Tool Broker (summary)

The broker is the **enforcement point between autonomous reasoning and external effects**, not a
collection of API wrappers.

| Effect | Meaning |
|---|---|
| `read` | Does not intentionally mutate external state |
| `idempotent_write` | Repeated invocation yields the same intended result |
| `side_effect` | Can create an externally observable action |
| `destructive` | Can delete, revoke or irreversibly alter data |

Effect labels are declared by the tool owner, validated at registration and enforced by policy.
A `read` label is never treated as proof of safety.

Invocation protocol: receive → validate input → authenticate principal → check tenant boundary →
evaluate policy → check budget → check idempotency → approval? (wait) → **re-authorize** →
execute → reconcile → persist → emit. **Approval is not a permanent grant.**

Outcome states: `PENDING → SUBMITTED → CONFIRMED | REJECTED | UNKNOWN`. **`UNKNOWN` ≠ failure.**
The broker persists intent before submission, uses a stable invocation ID as idempotency key,
reconciles where possible, never blindly repeats, and escalates. Kiln guarantees **controlled
execution with duplicate prevention where supported**, not universal exactly-once external effects.

**MCP is an adapter, not our domain model:** Agent → Kiln Tool Interface → Tool Broker → MCP Adapter
→ MCP Server. Implement against a pinned specification revision and the official SDK.

## 7. SDK and API (summary)

Native Python SDK (`@tool`, `@Agent`, `ctx.model`, `ctx.step`), framework wrapping
(`kiln.integrations.langgraph.wrap`), an `AgentAdapter` interface with a **capability manifest**
(`streaming`, `checkpointing`, `resume`, `tool_interception`, `cancellation`,
`deterministic_replay`). The runtime adapts its guarantees to the declared capabilities.

REST API v1 is resource-oriented (agents, versions, runs, events, trace, tools, policies, approvals,
evaluations), streams over **SSE**, and supports idempotency keys on run creation, cursor pagination,
consistent error codes, request IDs, OpenAPI, and compatibility guarantees. The REST API is
versioned independently of the internal event schema. Full listing: [ARCHITECTURE.md §6](../ARCHITECTURE.md#6-public-api-v1).

## 8. Evaluation and verification (summary)

Evaluation consumes execution evidence from the same infrastructure; it is not bolted onto a
dashboard. Pipeline: agent version → scenario dataset → execution runner → evidence → evaluators
(deterministic, policy, model-judge, pairwise, robustness/adversarial, economic) → aggregate →
compare with baseline → promotion decision.

- Scenarios are versioned, reproducible and provider-independent.
- Bradley–Terry pairwise ranking compares versions but is **not a universal quality metric**.
  **Averages must never conceal critical failures.**
- Promotion gates combine hard safety constraints, deterministic regressions, quality scores,
  statistical uncertainty, cost/latency and live canary behavior. **LLM judges are never the only
  gate for high-impact actions.**

## 9. Security (summary)

Security is designed in from the start. Threats: cross-tenant leakage, malicious agent code, prompt
injection, unauthorized tool execution, credential theft, duplicate transactions, resource
exhaustion, worker compromise, replay attacks, malicious tool outputs. Controls: tenant isolation
(application authZ **plus** PostgreSQL RLS as defense in depth), sandboxing (Docker for trusted
workloads; gVisor/Firecracker for untrusted code — **Docker is not a hostile-code boundary**),
secret references (`secret://…`, never raw keys in specs/events/prompts/logs), Cedar policies that
**fail closed**, brokered tool access with authorization outside model reasoning.
Details: [SECURITY.md](../SECURITY.md).

## 10. Reliability, recovery, replay (summary)

Every operation that can affect future execution has a durable boundary. Three distinct operations:

| Operation | Meaning |
|---|---|
| **Recovery** | Continue an interrupted execution |
| **Replay** | Reconstruct a past execution from recorded events; repeats no external operation |
| **Re-execution** | Run the agent again against the same or new inputs |

`ctx.step()` is a durable boundary: on recovery the recorded result is returned instead of
re-executing. Model calls are durable operations too. *This is not deterministic AI; it is
deterministic reconstruction from recorded results.* We never promise to restore an arbitrary Python
call stack. Crashes must be tested at **every** boundary.

## 11. Observability

Built on the journal and OpenTelemetry, mapped to the **GenAI semantic conventions**. Those
conventions are still in development, so instrumentation is versioned and Kiln's internal event
contract stays stable. Three levels:
**infrastructure** (workers, queue latency, DB) · **execution** (lifecycle, model/tool calls, retries,
checkpoints, approvals) · **behavioral** (goal completion, policy violations, quality, cost per
successful task, regressions). The most important screen is the **execution detail page**: what the
agent attempted, what the system allowed, what failed, how it recovered.

## 12. Infrastructure and deployment

Stack (subject to prototype validation): Python 3.12+ runtime and SDK, TypeScript SDK second,
FastAPI, PostgreSQL, PostgreSQL-backed queue, S3-compatible object storage, Next.js + Tailwind
dashboard, Cedar, OpenTelemetry, Docker, AWS, pytest. Explicit SQL migrations; **the ORM does not
define the domain model.**

Local: one command (Docker Compose). Redis and MinIO are **optional**; PostgreSQL is the source of
truth. AWS: ECS Fargate (API, workers, scheduler), RDS PostgreSQL, S3, Secrets Manager, KMS, ECR,
CloudWatch + OTel, VPC, SQS later. **No Lambda** for core execution, **no Kubernetes** until there is
an operational reason. Scaling: Compose → single API + PG + workers → worker pools + separate
scheduler → dedicated queue + partitioned event processing → isolated/private deployments. Plan for
backpressure, per-tenant concurrency limits, fair scheduling and noisy-neighbor protection
**before** the first large customer.

## 13. Acceptance criteria

`kiln-chaos` reliability matrix, security matrix and state-machine properties: see
[ROADMAP.md §3](../ROADMAP.md#3-acceptance-test-matrices).

## 14. Phases

Phase 0 Foundations → 1 Runtime Core → 2 Integrations & Tools → 3 Governance & Observability →
4 Evaluation & Verification → 5 Platform & Commercialization. Exit criteria and metrics:
[ROADMAP.md](../ROADMAP.md).

## 15. ADRs

See [adr/README.md](../adr/README.md).

## 16. Open questions

| Question | Current preference |
|---|---|
| Event sourcing vs event journal | Append-only journal + transactional projection |
| Own scheduler vs Temporal | Own agent scheduler (see A9) |
| Python vs Rust core | Python first; Rust only for proven bottlenecks |
| One DB vs separate stores | PostgreSQL initially |
| Native vs framework execution | Both, with explicit capability levels |
| Agent memory | External pluggable providers — **no universal memory in V1** |
| Multi-agent coordination | Explicit parent/child executions, later |
| Sandbox | Docker for trusted, stronger isolation for untrusted |
| Evaluation storage | Separate evaluation domain sharing execution evidence |

## 17. Long-term: the intelligence loop

Executions → evidence → failure and quality analysis → optimization proposals → evaluation and
verification → controlled deployment → executions. With 500 agents under management, Kiln can detect
repeated failure classes, expensive patterns, unsafe permission configurations and pre-deployment
regressions, and can compare providers. Promotion controls stay human-defined. This is where the
evaluation research and the execution infrastructure meet, and it is the main long-term
differentiator.

## 18. References

Temporal (durable execution, replay) · OpenTelemetry GenAI semantic conventions · Model Context
Protocol specification and SDKs · Cedar policy language · PostgreSQL (`SKIP LOCKED`, RLS,
transactions) · Firecracker / gVisor · Docker.

## 19. Next step and first milestone

Five documents first (done in this change), then **one vertical slice**:
register agent → create version → submit execution → schedule → model call → controlled tool call →
persist events → complete → inspect full history. **Then kill the worker and prove recovery.**

> **Milestone 1 — a reliable agent execution engine.** A developer registers an agent, executes it,
> inspects every operation, interrupts the worker, and watches the execution recover without
> repeating already-committed external actions.

**Non-negotiable:** every feature builds on the execution contract. None gets its own architecture.

---

## Appendix A — Review amendments (v0.1 → v0.2)

These came out of reviewing v0.1. Each one either removes an internal contradiction or names a
semantic the RFC left implicit. They are already reflected in the companion documents. Status:
**Proposed** until the owner accepts them.

| # | Area | Problem in v0.1 | Amendment |
|---|---|---|---|
| A1 | Lifecycle | `tool.completed → RUNNING` but `approval.granted → QUEUED`: inconsistent. Who holds the lease while waiting? | **A lease exists only in `RUNNING`.** Every waiting state releases the lease, and every exit from a waiting state goes to `QUEUED`. Short tool calls run *inside* the lease as events with no status change; `WAITING_FOR_TOOL` is only for deferred/async tools. |
| A2 | Lifecycle | No transition for budget exhaustion, `UNKNOWN` tool outcomes, approval rejection/expiry, or worker loss. | `budget.exhausted → SUSPENDED` (resumable after a budget raise); `tool.unresolved → SUSPENDED`; approval `granted/rejected/expired → QUEUED`, with the decision delivered to the agent as the step result; `lease.expired → QUEUED`. |
| A3 | Attempts | `attempt` is overloaded (crash recovery vs retry vs re-run). | Three counters: `lease_generation` (every claim, used for fencing), `attempt` (each `RETRY_WAIT → QUEUED`), and **re-execution = a new execution** with `rerun_of`. Recovery never increments `attempt`. |
| A4 | Cancellation | Race: the control plane cancels while a worker runs, and a side-effect may land after cancellation. | Cancelling `RUNNING` bumps `lease_generation`, which fences out the worker. Terminal executions accept **annotation events** (late tool outcomes, evaluations) but never transition events. |
| A5 | Durable steps | Step identity by name alone collides inside loops. Code changes between crash and recovery corrupt replay. | Step key = `name#occurrence`. On recovery, a name mismatch at a recorded position raises a **non-determinism error** and suspends the execution. Recovery always uses the execution's pinned `agent_version_id`. |
| A6 | Budgets | Post-hoc cost accounting lets a single call overshoot `max_cost_usd`. | **Reserve → settle**: reserve the worst-case cost (max tokens × price) before the call, settle actual usage after. A call is refused if its reservation would exceed the budget. A model call interrupted by a crash is charged again on retry, and that is visible in the journal. |
| A7 | Tenancy / RLS | RLS needs the tenant column on the row itself; "indirect" ownership through joins cannot be enforced efficiently. | Denormalize `organization_id` onto **every** tenant-owned table. Composite FKs `(organization_id, id)` make cross-tenant references impossible at the schema level. |
| A8 | Scheduler | The task queue is not in the data model. | Add `execution_tasks` (`available_at` doubles as the durable timer for retries and approval timeouts) and `workers` (heartbeats). |
| A9 | Temporal | "Temporal as a future backend" conflicts with owning leases, fencing and the journal; the two models don't compose cleanly. | **Non-goal for V1/V2.** Temporal stays a design reference. The pluggable seam is the *queue transport* (PG → SQS/NATS), not the durability model. |
| A10 | Approvals | An approval could be reused for different arguments. | An approval binds to `(tool_invocation_id, args_digest, policy_version)` and has an `expires_at`. Re-authorization happens at execution time. |
| A11 | Layout | `apps/runtime` + `apps/scheduler` + `apps/api` + 8 packages is too much surface for V0. | V0 ships two processes: **`server`** (API + scheduler + runtime coordination) and **`worker`**. Packages start as one distribution per RFC domain only once its interface stabilizes; `kiln-core` comes first. Redis is dropped from Compose; MinIO is behind an optional profile. |
| A12 | Eval gates | `minimum_score: 0.90` mixes absolute and relative metrics. Bradley–Terry yields *relative* strengths. | Gates separate **absolute** checks (deterministic pass rate, policy violations = 0, cost/latency deltas) from **relative** ones (candidate beats baseline with P(win) lower confidence bound > 0.5 via bootstrap over BT fits). Judges score both orderings to cancel position bias. |
| A13 | Cedar | Cedar's Python bindings are community-maintained, which is a supply-chain and maintenance risk. | A `PolicyEvaluator` interface. V1 ships a small built-in evaluator for effect/tool/amount rules; Cedar plugs in behind the same interface after a prototype (ADR-010 stays Proposed). |
