# Security

**Status:** Normative. Source: RFC-001 §9, amendments A7, A10 and A13.
MAIT executes code, holds credentials and can trigger financial and destructive operations. It is
a security-sensitive platform from day one.

## 1. Threat model

| # | Threat | Primary control | Verified by |
|---|---|---|---|
| T1 | Cross-tenant data access | App-level authZ + composite tenant FKs + RLS | `sec-01` |
| T2 | Malicious agent code | Managed mode: sandbox (Docker for trusted; gVisor/Firecracker for untrusted) | `sec-08` |
| T3 | Prompt injection via inputs or tool outputs | Brokered tools, authorization outside the model, untrusted-data labeling | `sec-04` |
| T4 | Unauthorized tool execution | Central broker + policy, fail closed | `sec-02`, `sec-05` |
| T5 | Credential theft | `secret://` references, per-invocation resolution, no secrets in prompts/events/logs | `sec-07` |
| T6 | Duplicate transactions | Intent-before-submit, idempotency keys, reconciliation, no blind retries | `rel-10` |
| T7 | Resource exhaustion | Budgets (reserve→settle), per-tenant concurrency caps, timeouts | `sec-06` |
| T8 | Worker compromise | Least-privilege DB role, fencing, no registry write access from workers | review |
| T9 | Replay / forged requests | Signed API tokens, idempotency keys, approval binding to args digest | `sec-09` |
| T10 | Malicious tool outputs | Schema validation, size limits, trust labels | `sec-04` |

## 2. Tenant isolation

Every request resolves to `organization → project → environment → agent → execution`. Access is
checked at the API boundary on **every** resource. A guessed ID returns 404, never another tenant's
row.

Defense in depth:

1. **Application repositories** take `organization_id` as a required argument and include it in
   every query.
2. **Composite foreign keys** `(organization_id, id)` make cross-tenant references unrepresentable.
3. **PostgreSQL RLS**: the app connects as `mait_app` and runs `SET LOCAL mait.organization_id` per
   transaction. If that setting is missing, no rows are visible (fails closed).
4. **Object storage paths** are tenant-prefixed (`s3://bucket/{org}/{project}/…`). Phase 5 adds
   per-tenant KMS data keys.
5. **Automated cross-tenant tests** in CI (`sec-01`).

RLS never replaces application authorization.

## 3. Execution modes and their honest guarantees

| Mode | Where agent code runs | What MAIT can enforce |
|---|---|---|
| **Trusted integration** | Inside the customer's process (SDK) | Journal, budgets, and policy **for calls that go through MAIT**. The agent *can* bypass the broker. MAIT is an operations layer here, not a security boundary. |
| **Managed execution** | MAIT worker sandbox | Filesystem, egress allow-list, CPU/memory, duration, process privileges, mounted credentials, broker-only tool access |

Docker with dropped capabilities, read-only root, a non-root user, a seccomp profile and no host
network is acceptable for **trusted** customer workloads. **We do not claim Docker is a boundary
against hostile code.** Untrusted code requires gVisor or Firecracker (Phase 3+ investigation).

## 4. Secrets

- Specs, events, prompts, tool outputs, logs and VCS **never** contain raw secrets.
  `ToolDefinition` rejects non-`secret://` credentials, and the schema enforces it too.
- The broker resolves `secret://project/name` at invocation time through a `SecretProvider`: env
  locally, AWS Secrets Manager in the cloud. The model never sees credentials.
- An event redaction pass runs before append (known secret values plus patterns). `sec-07` injects
  a secret into a tool output and asserts it never reaches the journal or telemetry.

## 5. Authorization (policy)

- **Model:** principal (agent version / execution / human), action (`tool:<name>`,
  `run:cancel`, …), resource, context (amount, environment, attempt, approval state).
- **Interface:** `PolicyEvaluator.evaluate(request) -> Decision{allow|deny|require_approval, reasons, policy_digest}`.
- **Backends:** `mait-rules-v1` (built-in, small: effect/tool/argument predicates) first, and Cedar
  behind the same interface after a prototype (A13).
- **Defaults:** `destructive` tools require approval unless a policy explicitly waives it. Any
  evaluation error is a **deny** for `side_effect`/`destructive` actions.
- **Approvals (A10)** bind to `(tool_invocation_id, args_digest, policy_id)` and expire. At execution
  time the broker **re-evaluates** policy. An approval is necessary but never sufficient.

## 6. Prompt injection posture

A policy engine cannot stop an agent that can bypass it. So:

- In managed mode, every external effect goes through the broker, and network egress is denied
  except to the broker and model gateways.
- Tool outputs are labeled `untrusted` in the context the runtime builds and are validated against
  `output_schema` when one is declared.
- Authorization never depends on model output ("the model said it's approved" is not a signal).
- High-impact actions require independent human approval through a channel the agent cannot write
  to.

## 7. Supply chain

Pinned dependencies (uv lockfile), image digests in agent versions, the MCP SDK pinned to a spec
revision, and Cedar bindings evaluated for maintenance status before adoption.

## 8. Reporting

Until a security contact exists, report vulnerabilities privately to the maintainer, not in public
issues.
