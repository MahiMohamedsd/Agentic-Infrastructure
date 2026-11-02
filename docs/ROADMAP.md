# Roadmap

**Status:** Normative. Phases are ordered by dependency, not by calendar. A phase is done when its
**exit criteria** pass in CI. A demo doesn't count.

## 1. Phases

### Phase 0 — Architecture and foundations ✅ (this change)

- [x] RFC-001 baseline with review amendments
- [x] ARCHITECTURE, EXECUTION_MODEL, DATA_MODEL, SECURITY, ROADMAP, ADR index
- [x] Canonical schema (`0001_init.sql`), verified on PostgreSQL 16
- [x] `kiln-core`: lifecycle table, event envelope, fenced projection, tool/adapter contracts, step matching
- [x] Property tests for state-machine invariants
- [x] Docker Compose dev environment (PostgreSQL; MinIO optional)

**Exit:** the architecture can be implemented without unresolved foundational contradictions.
Remaining owner decisions are listed in §4.

### Phase 1 — Runtime core (Milestone 1)

- `kiln-storage`: migration runner, tenant-scoped repositories, the fenced append path (§3 of EXECUTION_MODEL)
- Scheduler: claim (`SKIP LOCKED`), heartbeat, reaper with advisory lock, dead-lettering
- `kiln-runtime`: worker loop, `ctx.step`, `ctx.model` (one provider), `ctx.tool` (in-process broker stub with effects + intent-before-submit), checkpoints, cancellation, suspension, budgets (reserve→settle)
- `apps/server` endpoints: agents, versions, runs, cancel/suspend/resume, events (JSON + SSE)
- `kiln` CLI: `agent create`, `version create`, `run`, `runs show`, `runs events --follow`
- `kiln-chaos` harness: kill/pause workers at every durable boundary

**Exit: Milestone 1.** A developer registers an agent, executes it, inspects every operation,
kills the worker mid-run, and the execution recovers without repeating any committed external
action. Tests `rel-01`…`rel-05`, `rel-08`, `rel-09` pass.

### Phase 2 — Integrations and tool infrastructure

Tool registry API, full broker protocol, idempotency and reconciliation, `UNKNOWN` handling,
adapter interface hardening, **LangGraph adapter (Level 2 via a Kiln-backed checkpointer)**,
OpenAI Agents SDK adapter (Level 1), MCP adapter on a pinned spec.
**Exit:** an existing LangGraph agent runs inside the Kiln envelope with zero changes to its graph
code, and its guarantees are reported correctly. `rel-06`, `rel-07`, `rel-10` pass.

### Phase 3 — Governance and observability

`PolicyEvaluator` + `kiln-rules-v1`, approvals (bound, expiring, re-authorized), per-tenant
concurrency and fair scheduling, OTel exporter (GenAI semconv mapping, versioned), cost tracking,
journal partitioning, managed-mode Docker sandbox, gVisor/Firecracker spike.
**Exit:** developers can operate and govern real executions. The `sec-*` matrix passes.

### Phase 4 — Evaluation and verification

Datasets, scenario runner (executions tagged `evaluation`), deterministic/policy assertions, LLM
judges (both orderings), Bradley–Terry with bootstrap CIs, critical-failure veto, gate engine
(A12), version comparison.
**Exit:** a re-run of the same evaluation on the same versions gives the same gate decision. BT
rankings reproduce within CI. A planted critical regression is blocked even when quality improves.

### Phase 5 — Platform

Next.js dashboard (execution detail page first), org management, deployments/canary, hosted control
plane on AWS, billing, documentation, public release.
**Exit:** an external developer adopts Kiln without our help.

| Phase | Primary metric |
|---|---|
| 1 | Recovery correctness (chaos suite pass rate = 100%) |
| 2 | Integration effort (lines changed to adopt) and tool safety |
| 3 | Policy enforcement coverage and visibility |
| 4 | Evaluation reproducibility |
| 5 | External adoption and retention |

## 2. Milestone 1 vertical slice

```
register agent → create version → submit execution → schedule worker → model call
→ controlled tool call → persist events → complete → inspect full history
→ kill worker mid-run → lease expires → new worker recovers → no duplicate side effect
```

## 3. Acceptance test matrices

### Reliability (`kiln-chaos`)

| ID | Test | Required behavior |
|---|---|---|
| rel-01 | Worker killed during execution (at every durable boundary) | Lease expiry → recovery → same final result |
| rel-02 | Duplicate task delivery | No duplicate committed transition |
| rel-03 | Stale worker resumes after losing its lease | Fencing rejects every write (`StaleLease`) |
| rel-04 | Database unavailable mid-run | No false completion; worker stops on renewal failure |
| rel-05 | Model provider outage | Configured retry (`RETRY_WAIT`) or suspension |
| rel-06 | Tool timeout | Invocation → `UNKNOWN`, reconciliation per effect |
| rel-07 | Approval delayed for days | Durable waiting state, zero worker cost, timeout task fires |
| rel-08 | Cancellation racing completion | Exactly one terminal state; loser gets `IllegalTransition`/`StaleLease` |
| rel-09 | Budget exhausted | No call after reservation fails; `SUSPENDED` with reason |
| rel-10 | Duplicate refund after crash post-submit | One external effect when provider supports idempotency; escalation otherwise |

### Security

| ID | Test | Required behavior |
|---|---|---|
| sec-01 | Cross-tenant run access (API, repository, raw SQL as `kiln_app`) | Denied / not found |
| sec-02 | Unauthorized tool | Denied, `tool.invocation.denied` recorded |
| sec-03 | Expired credential | Fails; no fallback credential |
| sec-04 | Malicious tool output (injection, oversize, schema-violating) | Labeled untrusted; validated; size-capped |
| sec-05 | Invalid/erroring policy | Fail closed for mutating effects |
| sec-06 | Resource exhaustion | Limits enforced |
| sec-07 | Secret in tool output | Redacted before journal and telemetry |
| sec-08 | Escape attempt from managed sandbox | Contained (documented boundary per mode) |
| sec-09 | Approval reused for different arguments | Rejected (args digest mismatch) |

### State-machine properties (implemented in `kiln-core`)

- A terminal execution never transitions. ✅
- Every transition has a legal predecessor. ✅
- Sequence numbers are gap-free and never decrease. ✅
- A stale fencing token never mutates state. ✅
- A cancelled execution accepts no worker work. ✅
- Annotation events never change status. ✅
- `attempt` changes only on `retry.due`. ✅
- Tool invocations keep one logical identity across retries (DB: `UNIQUE(execution, step_key)`; runtime test in Phase 1).

## 4. Owner decisions needed

1. Accept or reject amendments **A1–A13** (RFC-001 Appendix A).
2. **License** for the open-source core (Apache-2.0 is the usual choice for infrastructure because
   of its patent grant; it hasn't been set).
3. First model provider for `ctx.model` in Phase 1.
4. Project name check ("Kiln" is a common word; check trademarks and PyPI/npm availability before
   the public release).
