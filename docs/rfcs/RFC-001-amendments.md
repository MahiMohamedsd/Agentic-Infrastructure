# RFC-001 Amendments (v0.1 → v0.2)

| Field | Value |
|---|---|
| Amends | [RFC-001 — MAIT Architecture](RFC-001-mait-architecture.md) |
| Status | **Decided 2026-11-02**: 10 accepted, 3 accepted with changes (A5, A6, A12), 0 rejected |
| Decided by | Review by Claude, delegated by the owner ("you judge what's best and decide"). The owner can reopen any item. |
| Already reflected in | [EXECUTION_MODEL](../EXECUTION_MODEL.md) · [DATA_MODEL](../DATA_MODEL.md) · [SECURITY](../SECURITY.md) · [ADR index](../adr/README.md) · `mait-core` · `0001_init.sql` |

These came out of reviewing RFC-001 v0.1. Each one removes an internal contradiction or names a
semantic the RFC left implicit.

Each decision below says why. "Accepted with changes" means the amendment itself had a flaw, which
was fixed in the documents and code before acceptance. To reopen an item, change its decision back to
`Proposed` and note the objection.

## Summary

| # | Area | One-line change | Decision |
|---|---|---|---|
| [A1](#a1--lease-held-only-in-running) | Lifecycle | Lease only in `RUNNING`; waits exit via `QUEUED` | Accepted |
| [A2](#a2--missing-transitions) | Lifecycle | Transitions for budget, `UNKNOWN` tool, approval outcomes, lease expiry | Accepted |
| [A3](#a3--three-counters-instead-of-attempt) | Counters | `lease_generation`, `attempt`, re-run = new execution | Accepted |
| [A4](#a4--cancellation-race) | Cancellation | Cancel fences the worker; late outcomes become annotation events | Accepted |
| [A5](#a5--durable-step-identity-and-non-determinism) | Durable steps | `name#occurrence` keys; mismatch detection | Accepted with changes |
| [A6](#a6--budget-reserve--settle) | Budgets | Reserve before the call, settle after | Accepted with changes |
| [A7](#a7--tenant-column-on-every-table) | Tenancy | `organization_id` everywhere, composite FKs | Accepted |
| [A8](#a8--task-queue-in-the-data-model) | Scheduler | `execution_tasks` + `workers` tables | Accepted |
| [A9](#a9--temporal-is-a-reference-not-a-backend) | Temporal | Reference only; the queue is the swappable seam | Accepted |
| [A10](#a10--approval-binding) | Approvals | Bound to args digest + policy, expiring, re-authorized | Accepted |
| [A11](#a11--fewer-deployables-in-v0) | Layout | Two processes: `server` + `worker` | Accepted |
| [A12](#a12--evaluation-gates-absolute-vs-relative) | Eval gates | Absolute vs relative gates; judge both orderings | Accepted with changes |
| [A13](#a13--policyevaluator-interface-before-cedar) | Policy | Interface first, built-in rules, Cedar later | Accepted |

---

## A1 — Lease held only in RUNNING

**Problem.** v0.1 sends `WAITING_FOR_TOOL --tool.completed--> RUNNING` but
`WAITING_FOR_APPROVAL --approval.granted--> QUEUED`. That's inconsistent, and it leaves open
whether a worker holds its lease while waiting. A worker that holds a lease for a two-day approval
wastes capacity. A worker that releases it and then jumps straight back to `RUNNING` has no claim,
so there's nothing to fence.

**Amendment.**
- A lease exists **iff** the status is `RUNNING`.
- Every waiting state (`WAITING_FOR_TOOL`, `WAITING_FOR_APPROVAL`, `RETRY_WAIT`, `SUSPENDED`)
  releases the lease, and every non-terminal exit from one goes to `QUEUED`. The run is then
  re-claimed with a new lease generation.
- Short tool calls run **inside** the lease as annotation events with no status change.
  `WAITING_FOR_TOOL` is only for deferred/async tools.

**Applied in.** EXECUTION_MODEL §2; `mait_core.lifecycle`; test
`test_only_running_holds_lease_and_waiting_states_exit_via_queued`; ADR-016.

**Decision.** **Accepted** — Re-claiming after every wait adds one claim round-trip (milliseconds) and buys a single fencing rule. Worth it.

---

## A2 — Missing transitions

**Problem.** v0.1 has no transition for budget exhaustion, a tool outcome stuck at `UNKNOWN`,
approval rejection or expiry, or a worker disappearing. The runtime would have to improvise.

**Amendment.**

| From | Event | To |
|---|---|---|
| RUNNING | `budget.exhausted` | SUSPENDED (resumable after a budget raise) |
| WAITING_FOR_TOOL | `tool.unresolved` | SUSPENDED (needs a human) |
| WAITING_FOR_APPROVAL | `approval.rejected` / `approval.expired` | QUEUED. The decision is delivered to the agent as the step result. |
| RUNNING | `lease.expired` | QUEUED (recovery, emitted by the reaper) |
| RUNNING | `nondeterminism.detected` | SUSPENDED (see A5) |

**Applied in.** EXECUTION_MODEL §2.1; `mait_core.lifecycle.TRANSITIONS`.

**Decision.** **Accepted** — Default for an expired approval is "deliver to the agent". A per-agent setting `on_approval_expired: fail` can be added later without a lifecycle change.

---

## A3 — Three counters instead of "attempt"

**Problem.** v0.1's `attempt` mixes three things: crash recovery, retry after a transient failure,
and running the agent again. They need different semantics. Fencing needs a number that changes on
every claim; retry limits need one that changes only on retries.

**Amendment.**
- `lease_generation` goes up on every claim and every forced fence (`lease.expired`,
  `run.cancelled`). It is the fencing token.
- `attempt` goes up only on `retry.due` (`RETRY_WAIT → QUEUED`). Crash recovery never increments it.
- `recoveries` counts lease expirations and drives dead-lettering.
- **Re-execution is a new execution** with `rerun_of` pointing to the original.

**Applied in.** EXECUTION_MODEL §1, §4.6; `executions` columns in `0001_init.sql`;
`mait_core.projection`; test `test_retry_increments_attempt_but_recovery_does_not`.

**Decision.** **Accepted**

---

## A4 — Cancellation race

**Problem.** The control plane cancels while a worker is running, and a side effect (a refund, say)
lands after the cancellation. v0.1 doesn't say whether the worker can keep writing or where the late
outcome goes.

**Amendment.**
- Cancelling a `RUNNING` execution bumps `lease_generation`, so every later write from that worker
  fails with `StaleLease`.
- Terminal executions accept **control-plane annotation events** (`tool.invocation.late_outcome`,
  `evaluation.recorded`) but never transition events. The refund is recorded, and the run stays
  `CANCELLED`.

**Applied in.** EXECUTION_MODEL §8; `mait_core.lifecycle.ANNOTATION_EVENT_TYPES`; test
`test_cancel_fences_running_worker_but_late_outcomes_are_still_recorded`.

**Decision.** **Accepted**

---

## A5 — Durable step identity and non-determinism

**Problem.** Identifying `ctx.step("poll", …)` by name alone collides inside loops. If the agent's
code changes between a crash and its recovery, recorded results get handed to the wrong calls, and
nothing notices.

**Amendment.**
- Step key = `name#occurrence`, the n-th call with that name in this execution: `poll#0`,
  `poll#1`, …
- On recovery, a mismatch at a recorded position (name, kind, or tool argument digest) raises
  `NonDeterminismError`, appends `nondeterminism.detected`, and suspends the run.
- Recovery always uses the execution's pinned `agent_version_id`. A deploy never changes in-flight
  code.

**Applied in.** EXECUTION_MODEL §5.2–5.3; `mait_core.steps`; `tool_invocations.step_key`; ADR-015.

**Decision.** **Accepted with changes** — The original rule matched calls by journal position, which breaks under concurrency: with `asyncio.gather(ctx.step("a"), ctx.step("b"))`, steps complete (and are journaled) in a nondeterministic order, so a correct replay would be flagged as non-determinism or results would be swapped. **Change:** every durable call gets an **allocation ordinal** synchronously at call time (program order, stable under `gather`), recorded in `step.started`. Matching is by ordinal, not by journal position. Two further rules: an ordinal missing from history (a crash between allocation and append) executes live, and recorded steps never reached by the end of the run are non-determinism. Implemented in `mait_core.steps` with three new tests (concurrent completion order, crash gap, skipped steps).

---

## A6 — Budget reserve → settle

**Problem.** If cost is counted after each call, one call can blow through `max_cost_usd` (for
example a $0.09 call when $0.02 is left).

**Amendment.**
1. Before a model call, reserve the worst case: `max_output_tokens × output_price + input_tokens × input_price`.
2. If `spent + outstanding + reservation > max_cost_usd`, emit `budget.exhausted` and **do not call**.
3. After the call, settle the actual usage and release the rest of the reservation.
4. An unknown price with a cost budget set means the call is refused (fail closed).
5. A model call interrupted by a crash is charged again on retry, and the journal shows it.

**Applied in.** EXECUTION_MODEL §7; `budget.reserved` / `budget.settled` events; `usage.reserved_usd`; ADR-018.

**Decision.** **Accepted with changes** — The reservation formula needs `input_tokens` *before* the call, which v0.2 left unspecified. **Change:** use the provider's token-counting endpoint when available; otherwise use the request's UTF-8 byte length as a safe upper bound. Concurrent calls each hold their own reservation. EXECUTION_MODEL §7 updated.

---

## A7 — Tenant column on every table

**Problem.** v0.1 allows tenant ownership "indirectly" through other entities. RLS can't enforce
that efficiently, and a bug could link a child row to another tenant's parent.

**Amendment.**
- `organization_id` goes on **every** tenant-owned table.
- Each parent exposes `UNIQUE (organization_id, id)`, and children reference it with composite FKs.
  A cross-tenant reference becomes a constraint violation.
- RLS policy: `organization_id = current_setting('mait.organization_id')`. A missing setting shows
  no rows.

**Applied in.** DATA_MODEL §1; `0001_init.sql`; SQL invariants (cross-tenant FK, RLS read/write);
SECURITY §2.

**Decision.** **Accepted** — Costs some index space on composite keys. That is the right price for making cross-tenant references impossible.

---

## A8 — Task queue in the data model

**Problem.** v0.1 specifies a PostgreSQL-backed queue but has no table for it, and nowhere to keep
durable timers (retry backoff, approval timeouts).

**Amendment.**
- `execution_tasks`: `state` (ready/leased/done/dead), `available_at` (which doubles as the durable
  timer), lease fields, and a partial unique index allowing **one live task per execution**.
- `workers`: heartbeats, pool, capabilities.
- The queue is rebuildable from `executions` and the journal. It is never a source of truth.

**Applied in.** EXECUTION_MODEL §4; DATA_MODEL §3; `0001_init.sql`.

**Decision.** **Accepted**

---

## A9 — Temporal is a reference, not a backend

**Problem.** v0.1 suggests Temporal as a possible future durability backend, but MAIT owns its own
leases, fencing and journal. Temporal's workflow-history model doesn't compose with that. Keeping
the option open would leak into every interface.

**Amendment.**
- Temporal is a **design reference** only, through V2.
- The pluggable seam is the **queue transport** (PostgreSQL → SQS/NATS), not the durability model.

**Applied in.** ARCHITECTURE §8; ADR-017.

**Decision.** **Accepted** — Revisit only if building our own durable timers or worker coordination becomes the bottleneck. The ADR-005 benchmark will tell us.

---

## A10 — Approval binding

**Problem.** In v0.1, an approval granted for one action could be reused for another (a $50 refund
approved, then a $5,000 refund executed), or used after the policy changed.

**Amendment.**
- An approval binds to `(tool_invocation_id, args_digest, policy_id)` and has `expires_at`.
- The broker **re-evaluates policy** at execution time. An approval is necessary but never
  sufficient.

**Applied in.** `approval_requests` in `0001_init.sql`; SECURITY §5; ROADMAP test `sec-09`.

**Decision.** **Accepted**

---

## A11 — Fewer deployables in V0

**Problem.** `apps/api` + `apps/runtime` + `apps/scheduler` + `apps/worker` plus eight packages, plus
Redis in Compose, is more surface than V0 needs. It brings deployment and debugging overhead before
there are any users.

**Amendment.**
- V0 ships **two processes**: `mait server` (API + gateway + scheduler/reaper, singleton through a
  PostgreSQL advisory lock) and `mait worker`.
- A package gets its own directory only when work on it starts. `mait-core` is first.
- Redis is removed. MinIO sits behind an optional Compose profile.

**Applied in.** ARCHITECTURE §3–4; `infrastructure/docker/docker-compose.yml`; ADR-001.

**Decision.** **Accepted**

---

## A12 — Evaluation gates: absolute vs relative

**Problem.** v0.1's `minimum_score: 0.90` mixes absolute and relative metrics. Bradley–Terry gives
*relative* strengths between versions, not an absolute score. LLM judges also have position bias.

**Amendment.**
- **Absolute gates:** deterministic pass rate, policy violations = 0, critical regressions = 0,
  cost/latency deltas.
- **Relative gate:** the candidate beats the baseline with the lower confidence bound of P(win) above
  0.5, using a bootstrap over Bradley–Terry fits.
- Judges score **both orderings** of every pair to cancel position bias.
- A critical failure vetoes promotion whatever the quality score is.

**Applied in.** ROADMAP Phase 4 exit criteria; `evaluation_runs.gate_decision`.

**Decision.** **Accepted with changes** — A gate that only tests "candidate beats baseline" blocks a legitimate class of releases: moving to a cheaper or faster model with *equal* quality. **Change:** the relative gate has two modes. **Superiority**: lower bound of P(win) > 0.5. **Non-inferiority**: lower bound of P(win) > 0.5 − margin (default margin 0.05), only valid together with an improved cost or latency gate. If the confidence interval is too wide to decide, the result is `inconclusive`, which blocks promotion and asks for more samples, but is not recorded as a failure. ROADMAP Phase 4 updated.

---

## A13 — PolicyEvaluator interface before Cedar

**Problem.** Cedar's Python bindings are community-maintained, which is a supply-chain and
maintenance risk if the policy engine depends on them directly from day one.

**Amendment.**
- `PolicyEvaluator.evaluate(request) -> Decision{allow | deny | require_approval, reasons, policy_digest}`.
- V1 ships a small built-in evaluator (`mait-rules-v1`) for effect/tool/argument rules.
- Cedar plugs in behind the same interface after a prototype. ADR-010 stays Proposed.

**Applied in.** SECURITY §5; `policies.language` in `0001_init.sql`; ADR-010.

**Decision.** **Accepted**
