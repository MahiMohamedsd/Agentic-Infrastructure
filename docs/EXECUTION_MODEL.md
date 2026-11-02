# Execution Model

**Status:** Normative for Phase 1. Implements RFC-001 §5 and §10 with amendments A1–A6 and A8.
The reference implementation of the lifecycle and projection rules is
[`mait_core.lifecycle`](../packages/mait-core/src/mait_core/lifecycle.py) and
[`mait_core.projection`](../packages/mait-core/src/mait_core/projection.py). When this document and
that code disagree, the property tests decide which one is wrong.

---

## 1. Vocabulary

| Term | Definition |
|---|---|
| **Execution** | One invocation of one immutable agent version. Has exactly one lifecycle. |
| **Task** | A unit of schedulable work for an execution (`execution_tasks` row). At most one *claimable* task per execution. |
| **Lease** | Time-bounded exclusive right of one worker to advance one execution. Exists **only** in `RUNNING`. |
| **Lease generation** | Monotonic integer, incremented on every claim and on every forced fence (`lease.expired`, `run.cancelled`). It is the fencing token. |
| **Attempt** | Incremented only when a retry is scheduled (`RETRY_WAIT → QUEUED`). Crash recovery does **not** increment it. |
| **Durable boundary** | A point at which the result of an operation is committed to the journal before the agent may observe it. |
| **Transition event** | A journal event that changes `executions.status`. |
| **Annotation event** | A journal event that records facts without changing status (model call, tool invocation, checkpoint, late outcome, evaluation). |

## 2. Lifecycle

```
                 run.cancelled (from any non-terminal)
                 ─────────────────────────────────────────────► CANCELLED
 CREATED ──run.accepted──► QUEUED ──worker.claimed──► RUNNING ──run.completed──► COMPLETED
                             ▲  ▲  ▲  ▲                 │  │ │ │ │ │
                             │  │  │  └─lease.expired───┘  │ │ │ │ └─run.failed──► FAILED
                             │  │  │                       │ │ │ │
                             │  │  └─retry.due── RETRY_WAIT◄┘ │ │ │ step.failed.retryable
                             │  │                            │ │ │
                             │  └─approval.{granted,rejected,expired}── WAITING_FOR_APPROVAL◄┘ │ │
                             │                                                                │ │
                             └─tool.resolved── WAITING_FOR_TOOL ◄──tool.deferred──────────────┘ │
                                                  │                                             │
                                                  └─tool.unresolved─► SUSPENDED ◄───────────────┘
                                                                        │   run.suspended / budget.exhausted
                                                                        └─run.resumed──► QUEUED
```

### 2.1 Normative transition table

| From | Event | To | Notes |
|---|---|---|---|
| CREATED | `run.accepted` | QUEUED | Admission passed; first task enqueued in the same transaction. |
| QUEUED | `worker.claimed` | RUNNING | Increments `lease_generation`. |
| RUNNING | `lease.expired` | QUEUED | Recovery. Emitted by the scheduler's reaper, not by a worker. Increments `lease_generation` (fences the lost worker). |
| RUNNING | `tool.deferred` | WAITING_FOR_TOOL | Async/long tool only. Lease released. |
| WAITING_FOR_TOOL | `tool.resolved` | QUEUED | Outcome `CONFIRMED` or `REJECTED`. |
| WAITING_FOR_TOOL | `tool.unresolved` | SUSPENDED | Outcome `UNKNOWN` after reconciliation budget; needs a human. |
| RUNNING | `approval.requested` | WAITING_FOR_APPROVAL | Lease released. Approval timeout = task `available_at`. |
| WAITING_FOR_APPROVAL | `approval.granted` | QUEUED | Re-authorization still happens at execution time (A10). |
| WAITING_FOR_APPROVAL | `approval.rejected` | QUEUED | The rejection is delivered to the agent as the step result. |
| WAITING_FOR_APPROVAL | `approval.expired` | QUEUED | Delivered as a step result; the agent decides what to do. |
| RUNNING | `step.failed.retryable` | RETRY_WAIT | Only if `attempt < max_attempts`. Otherwise the worker emits `run.failed`. |
| RETRY_WAIT | `retry.due` | QUEUED | Increments `attempt`. |
| RUNNING | `budget.exhausted` | SUSPENDED | Resumable after an operator raises the budget. |
| RUNNING | `nondeterminism.detected` | SUSPENDED | See §5.3. |
| {QUEUED, RUNNING, WAITING_*, RETRY_WAIT} | `run.suspended` | SUSPENDED | From RUNNING: cooperative, applied at the next durable boundary. |
| SUSPENDED | `run.resumed` | QUEUED | |
| RUNNING | `run.completed` | COMPLETED | |
| RUNNING | `run.failed` | FAILED | Re-execution creates a **new** execution with `rerun_of`. |
| any non-terminal | `run.cancelled` | CANCELLED | Increments `lease_generation` (fences a running worker). |

Every pair not listed is **illegal** and is rejected by the runtime.

### 2.2 Invariants (property-tested)

1. Terminal states (`COMPLETED`, `FAILED`, `CANCELLED`) have no outgoing transitions.
2. A lease is held iff status = `RUNNING`.
3. `sequence` is gap-free and strictly increasing per execution.
4. `lease_generation` never decreases.
5. A write carrying a stale `lease_generation` never mutates the projection.
6. Annotation events never change `status`; transition events never apply to a terminal execution.
7. `attempt` changes only on `retry.due`.

## 3. Journal and projection

Every write follows one pattern, inside **one PostgreSQL transaction**:

```sql
-- 1. Fence + lock the projection row
SELECT status, sequence, lease_generation
  FROM executions
 WHERE id = $exec AND organization_id = $org
   FOR UPDATE;
-- abort if lease_generation <> $worker_generation  (stale writer)
-- abort if transition is illegal for current status

-- 2. Append event with the next sequence (unique (execution_id, sequence) is the backstop)
INSERT INTO execution_events (..., sequence, type, payload, lease_generation)
VALUES (..., $current_sequence + 1, ...);

-- 3. Update projection
UPDATE executions SET status = $new_status, sequence = $current_sequence + 1, ... WHERE id = $exec;

-- 4. Enqueue / complete tasks as required by the transition
```

- **Idempotent appends:** every event carries a writer-generated `event_id` (unique). Re-sending the
  same `event_id` is a no-op that returns the original sequence. This is how "duplicate delivery is
  tolerated".
- **Payloads larger than 16 KiB** go to object storage. The event stores a `payload_ref` plus a
  sha256 digest.
- **Consumers** (telemetry exporter, evaluation, SSE) read with `sequence > cursor` and keep their
  own cursors. They never write to the journal.

## 4. Scheduling, leases and fencing

### 4.1 Claim

```sql
WITH next AS (
  SELECT t.id, t.execution_id
    FROM execution_tasks t
   WHERE t.state = 'ready'
     AND t.available_at <= now()
     AND t.pool = $pool
   ORDER BY t.priority DESC, t.available_at
   LIMIT 1
   FOR UPDATE SKIP LOCKED
)
UPDATE execution_tasks t
   SET state = 'leased', worker_id = $worker, lease_expires_at = now() + $lease_ttl
  FROM next WHERE t.id = next.id
RETURNING t.execution_id;
-- then, same transaction: executions.lease_generation += 1, append worker.claimed
```

Fair scheduling across tenants (Phase 3) replaces `ORDER BY` with per-tenant round-robin plus
per-tenant concurrency caps (`count(*) WHERE state='leased' AND organization_id=$org`).

### 4.2 Heartbeat and renewal

Workers renew at `lease_ttl / 3` with
`UPDATE … SET lease_expires_at = now()+ttl WHERE id=$task AND worker_id=$me AND generation=$gen`.
If zero rows are updated, the worker has lost the lease. It **must stop immediately** and must not
call any further tools.

### 4.3 Reaper

The reaper runs in the server process with a PostgreSQL advisory lock, so there is one active
reaper. It finds `leased` tasks whose `lease_expires_at < now()` and, for each one, appends
`lease.expired`, bumps `lease_generation` (fencing the old holder) and re-readies the task.

### 4.4 Stale-writer scenario (the reason fencing exists)

1. Worker A claims (gen 7), then pauses (GC or a network partition).
2. The lease expires. The reaper fences (gen 8) and re-queues; worker B claims (gen 9).
3. Worker A wakes up and tries to append with gen 7. The `FOR UPDATE` check sees 9 ≠ 7 and the
   transaction aborts with `StaleLease`.

Worker A may *already have sent* an external request between steps 1 and 3. That is why tool
invocations persist intent with a stable idempotency key **before** submission (§6). Fencing
protects MAIT's state; idempotency keys protect the outside world.

### 4.5 Timers

There is no separate timer service. `RETRY_WAIT`, approval timeouts and reconciliation polls are
`execution_tasks` rows with a future `available_at`.

### 4.6 Dead letters

A task whose execution has more than `max_recoveries` lease expirations (default 5) becomes
`dead`. The execution transitions to `SUSPENDED` with reason `poison`. An operator inspects it,
then resumes or cancels.

## 5. Native durable execution (`ctx`)

### 5.1 Primitives

| Primitive | Durable? | Recorded as |
|---|---|---|
| `await ctx.step(name, fn, *args)` | yes | `step.started` / `step.completed` (result or error) |
| `await ctx.model.generate(...)` | yes | `model.requested` / `model.completed` (provider, requested model, served model, params, usage, response ref) |
| `await ctx.tool(name, args)` | yes | Tool Broker protocol, `tool.invocation.*` |
| `await ctx.approval(...)` | yes | `approval.requested` → wait state |
| `await ctx.sleep(seconds)` | yes | task `available_at` (lease released) |
| `ctx.checkpoint(state)` | yes | `checkpoint.created` + `checkpoints` row |
| Plain Python between primitives | **no** | Re-run on recovery. Must be deterministic. |

### 5.2 Recovery by reconstruction

When a worker resumes execution *E*, it loads the journal and runs the agent function from the top.
Each durable primitive call gets two identifiers **synchronously, at call time** (before the
returned awaitable is scheduled):

- **ordinal**: the n-th durable call in this execution (0, 1, 2, …), and
- **step key** = `name#occurrence`: the n-th call with that name.

Both are written into `step.started` / `tool.invocation.pending`. Matching uses the **ordinal**.
Concurrent steps (`asyncio.gather`) *complete* in a nondeterministic order, but they are *allocated*
in program order, so allocation ordinals are stable across replays and completion order is
irrelevant. For each call:

- A recorded completed result exists → return it without executing.
- A recorded `started` exists but no completion → the operation was interrupted. Steps and model
  calls run again (charged again; budget-visible). Tool invocations follow the broker's
  reconciliation rules and are never blindly repeated.
- No record for that ordinal → execute normally (live mode). This includes a gap left by a crash
  between allocation and the `step.started` append. Intent-before-submit guarantees an unrecorded
  tool call never left MAIT.

Checkpoints are an optimization. With a checkpoint at sequence *s*, the runtime restores the
checkpointed state and only replays primitives after *s*.

### 5.3 Non-determinism detection

If the call allocated with ordinal *n* doesn't match the step recorded with ordinal *n* (different
key or kind, or a different args digest for tools), the runtime raises `NonDeterminismError`,
appends `nondeterminism.detected` and suspends. When the agent returns, any recorded step it never
reached is also non-determinism (the code took a different path). Recovery **always** uses the execution's pinned
`agent_version_id`. A deploy never changes the code of an in-flight execution.

### 5.4 Model calls

`ctx.model.generate` goes through a `ModelProvider` (ADR-019). Rules:

- **Providers never retry internally.** A failed call raises a `ModelProviderError` with
  `retryable` set. The runtime turns retryable errors into `step.failed.retryable → RETRY_WAIT`
  (honoring `retry_after_seconds`) and the rest into `run.failed`. Every retry is journaled and
  budgeted.
- **`model.completed` stores the full provider response verbatim** (`ModelResponse.raw`, through
  `payload_ref`), plus `ModelInfo`: provider, server, requested model, served model, and weights
  digest. Text alone is not enough evidence, and some providers require earlier responses to be
  sent back unchanged.
- **Malformed tool arguments are data, not crashes.** `ToolCall.arguments is None` reaches the
  broker, which records `tool.invocation.denied` with reason `invalid_arguments`. The agent
  receives the error as the tool result.
- `finish_reason = length` means truncated output. It's recorded, and the agent decides what to do.

### 5.5 What we explicitly do not promise

- Restoring an arbitrary Python call stack or local variables outside durable primitives.
- Identical model output on re-execution.
- Exactly-once external effects for tools that support neither idempotency keys nor reconciliation.

## 6. Tool invocation states

```
PENDING ──submit──► SUBMITTED ──► CONFIRMED
   │                    │      └──► REJECTED
   │                    └─────────► UNKNOWN ──reconcile──► CONFIRMED | REJECTED
   └──denied (policy/budget/approval)──► DENIED            └─give up──► (execution SUSPENDED)
```

`PENDING` is committed (with `idempotency_key = invocation_id`) **before** any network I/O.
On recovery:

| Found state | Action |
|---|---|
| PENDING | Safe to submit (nothing has left MAIT). |
| SUBMITTED / UNKNOWN, tool supports reconciliation | Query the provider by idempotency key. |
| SUBMITTED / UNKNOWN, `idempotent_write` or provider honors idempotency keys | Resubmit with the same key. |
| SUBMITTED / UNKNOWN, otherwise | **Do not resubmit.** Mark UNKNOWN → `tool.unresolved` → SUSPENDED. |
| CONFIRMED / REJECTED | Return the recorded outcome. |

## 7. Budgets (reserve → settle)

Budget dimensions: `max_steps`, `max_duration_seconds` (wall clock from first claim, excluding
waiting states), `max_cost_usd`, `max_tool_calls`, `max_model_tokens` (input + output across all
model calls). Local open-weight models cost ~$0 per token, so `max_model_tokens` and
`max_duration_seconds` are the budgets that bind for them (ADR-019). Reservation and settlement
work the same way in tokens as in dollars.

1. Before a model call: `reservation = max_output_tokens × output_price + input_tokens × input_price`.
   `input_tokens` comes from the provider's token-counting endpoint when one is available. Otherwise
   the UTF-8 byte length of the request is used as a safe upper bound (byte-level tokenizers emit at
   least one byte per token).
2. If `spent + outstanding_reservations + reservation > max_cost_usd`, raise `budget.exhausted` and
   do not call.
3. After the call, settle the actual usage and release the remainder of the reservation.
4. Concurrent calls in one execution each hold their own reservation, so `outstanding_reservations`
   covers parallelism.

Prices come from a versioned price table referenced by the agent version. Unknown price → the call
is refused when a cost budget is set (fail closed).

## 8. Cancellation and suspension semantics

| Operation | Terminal | Resumable | Running worker |
|---|---|---|---|
| Cancel | yes | no | fenced immediately (generation bump); in-flight tool outcomes still recorded as annotation events |
| Suspend | no | yes | cooperative: honored at the next durable boundary |
| Fail | yes | no (re-execute = new execution) | n/a |

## 9. Event type registry (v1)

Transition events: `run.accepted`, `worker.claimed`, `lease.expired`, `tool.deferred`,
`tool.resolved`, `tool.unresolved`, `approval.requested`, `approval.granted`, `approval.rejected`,
`approval.expired`, `step.failed.retryable`, `retry.due`, `budget.exhausted`,
`nondeterminism.detected`, `run.suspended`, `run.resumed`, `run.completed`, `run.failed`,
`run.cancelled`.

Annotation events: `step.started`, `step.completed`, `model.requested`, `model.completed`,
`tool.invocation.pending`, `tool.invocation.submitted`, `tool.invocation.completed`,
`tool.invocation.unknown`, `tool.invocation.denied`, `tool.invocation.late_outcome`,
`policy.evaluated`, `checkpoint.created`, `budget.reserved`, `budget.settled`, `log`.

Event envelope (schema version 1):

```json
{
  "schema": 1,
  "event_id": "evt_01JC…",
  "organization_id": "org_…",
  "execution_id": "run_…",
  "sequence": 14,
  "lease_generation": 3,
  "type": "tool.invocation.completed",
  "occurred_at": "2026-11-03T00:00:05Z",
  "payload": { "invocation_id": "tinv_…", "status": "CONFIRMED" },
  "payload_ref": null,
  "payload_digest": "sha256:…"
}
```

Event schema evolution is additive within a major `schema` version. Consumers ignore unknown
fields and unknown annotation types.
