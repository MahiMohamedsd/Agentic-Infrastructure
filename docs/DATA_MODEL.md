# Data Model

**Status:** Normative for Phase 1. DDL: [`infrastructure/migrations/0001_init.sql`](../infrastructure/migrations/0001_init.sql).
The DDL has been verified on PostgreSQL 16: tenant FKs, immutability triggers, the single-live-task
index, the terminal/`finished_at` check, and RLS isolation all behave as described.

## 1. Principles

1. **PostgreSQL is the system of record.** The queue, telemetry and caches can be rebuilt from it.
2. **Projection + journal.** `executions` holds current state. `execution_events` is the
   authoritative history. Both change in the same transaction.
3. **Tenant column everywhere (A7).** Every tenant-owned row carries `organization_id`, and children
   reference parents by `(organization_id, id)`. A cross-tenant reference is a constraint
   violation, not a bug waiting to happen.
4. **Immutability lives in the database.** Triggers reject `UPDATE` on `agent_versions`,
   `tool_definitions`, `policies` and `evaluation_datasets`, and reject `UPDATE`/`DELETE` on
   `execution_events`.
5. **Large or sensitive payloads go to object storage** (`*_ref` + `sha256:` digest). Inline payloads
   are limited to 16 KiB and stored redacted.
6. **IDs are prefixed and time-sortable** (`run_01J…`, see `mait_core.ids`). They're readable in
   logs and append-friendly for B-trees.

## 2. Entity map

```
organizations
  └─ projects (environment)
       ├─ agents ──────────────── agent_versions (immutable, digests)
       │     │                        ├─ agent_version_tools ── tool_definitions (immutable)
       │     │                        └─ policy_id ──────────── policies (immutable, versioned)
       │     └─ deployments (one active per agent+env)
       ├─ evaluation_datasets (immutable, versioned)
       └─ executions (projection) ─┬─ execution_events (append-only journal)
                                   ├─ execution_tasks (queue, timers)
                                   ├─ checkpoints (→ event sequence)
                                   ├─ tool_invocations ── approval_requests
                                   └─ evaluation_results ── evaluation_runs
workers (global, not tenant-owned)
```

## 3. Entities

| Table | Purpose | Key constraints |
|---|---|---|
| `organizations` | Tenant boundary | — |
| `projects` | Resource grouping per environment | `UNIQUE(org, name, environment)` |
| `agents` | Logical identity | `UNIQUE(project, name)` |
| `agent_versions` | Immutable definition: `spec`, `spec_digest`, `artifact_digest`, `runtime_compat`, `integration_level`, `adapter`, `adapter_capabilities`, `policy_id` | immutable; `UNIQUE(agent, version)` |
| `agent_version_tools` | Pins exact tool contract versions | PK `(agent_version, tool_definition)` |
| `tool_definitions` | Tool contract: effect, schemas, idempotency/reconciliation support, approval, timeout, transport, `credentials_ref` | immutable; `credentials_ref LIKE 'secret://%'` |
| `policies` | Versioned policy documents (`mait-rules-v1` or `cedar`) | immutable; `UNIQUE(project, name, version)` |
| `deployments` | Version → environment, canary %, promotion evidence | one `active` per agent+env |
| `executions` | Projection: status, `sequence`, `lease_generation`, `attempt`, `recoveries`, `rerun_of`, budget, usage | status CHECK; terminal ⇔ `finished_at` set; `UNIQUE(org, idempotency_key)` |
| `execution_events` | Journal | PK `(execution_id, sequence)`; `event_id` UNIQUE; append-only |
| `execution_tasks` | Durable queue and timers (`available_at`) | ≤ 1 live (`ready`/`leased`) task per execution |
| `workers` | Heartbeats, pools, capabilities | — |
| `checkpoints` | Recoverable state at a journal position | FK to `(execution_id, sequence)` |
| `tool_invocations` | Stable logical identity of an external action | `UNIQUE(execution, step_key)`, `UNIQUE(tool, idempotency_key)` |
| `approval_requests` | Human decisions bound to `args_digest` + `policy_id`, with `expires_at` (A10) | pending ⇔ `decided_at IS NULL` |
| `evaluation_datasets` | Versioned scenario sets in object storage | immutable |
| `evaluation_runs` | Candidate vs baseline over a dataset; `gate_decision` | — |
| `evaluation_results` | Per-case scores linked to the **real execution** that produced the evidence | — |

## 4. Agent version spec (canonical, resolved)

The registry stores the **resolved** spec. Aliases and floating references are resolved at version
creation time, and `spec_digest = sha256(canonical_json(spec))`.

```json
{
  "mait_spec": 1,
  "runtime": { "type": "python", "entrypoint": "agents.support:run", "compat": ">=0.1,<0.2" },
  "artifact": { "kind": "oci", "digest": "sha256:…" },
  "model": {
    "provider": "openai_compatible",
    "server": "ollama",
    "base_url_ref": "config://models/local",
    "requested_model": "qwen3:4b",
    "weights_digest": "ollama:<digest from /api/tags>",
    "params": { "temperature": 0, "seed": 7, "max_output_tokens": 1024 },
    "price_table": "prices/local-zero"
  },
  "tools": [{ "name": "orders.read", "version": "1.0.0", "digest": "sha256:…" }],
  "policy": { "id": "pol_…", "digest": "sha256:…" },
  "limits": { "max_steps": 20, "max_duration_seconds": 300, "max_cost_usd": 0.10, "max_tool_calls": 10, "max_model_tokens": 50000 },
  "adapter": { "name": "native", "capabilities": { "checkpointing": true, "resume": true, "tool_interception": true, "deterministic_replay": true } }
}
```

The provider's *served* model and, for open weights, the **weights digest** are recorded per call
in `model.completed`. A name like `qwen3:4b` can be re-pulled with different weights. If the
served digest differs from the version's pinned `weights_digest`, the call is recorded as drift,
and evaluation treats it as a different model.

## 5. Write paths

| Operation | Tables touched in one transaction |
|---|---|
| Create run | `executions` (CREATED→QUEUED), 2 events, `execution_tasks` (ready) |
| Claim | `execution_tasks` (leased), `executions` (gen+1, RUNNING), `worker.claimed` |
| Worker event | lock `executions` → fence check → `execution_events` → projection update |
| Tool intent | `tool_invocations` (PENDING) + `tool.invocation.pending`, **before** network I/O |
| Wait state | event + projection + task `done` + new task with future `available_at` (for timeouts) |
| Reaper | `lease.expired` + gen+1 + task back to `ready` (or `dead` past `max_recoveries`) |

## 6. Retention and growth

- `execution_events` is the largest table. Plan to partition it by month on `occurred_at` (Phase 3).
  The PK must then include the partition key, which is handled by a migration that swaps in a
  partitioned table.
- Payload blobs follow a per-project retention policy. Digests stay so evidence remains verifiable
  even after a blob is deleted.
- `execution_tasks` rows in `done` are deleted after 7 days. They are not evidence.

## 7. Open items

- Whether `usage` should become its own table (`execution_usage`, one row per model call) for
  cost analytics. This is likely in Phase 3.
- Multi-agent parent/child semantics. `parent_execution_id` is reserved but has no behavior yet.
