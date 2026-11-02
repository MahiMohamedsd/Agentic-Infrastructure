# Architecture Decision Records

**Accepted** principles are frozen; changing one requires a superseding ADR. **Proposed** choices
need a prototype or benchmark before acceptance. Each ADR gets its own file when it is first
challenged or prototyped.

| ADR | Decision | Status | Notes / validation required |
|---|---|---|---|
| ADR-001 | Modular architecture, separate execution plane (`server` + `worker`) | Accepted | A11 |
| ADR-002 | Python-first runtime | Proposed | Revisit only for proven bottlenecks (Rust) |
| ADR-003 | Own agent-specific execution engine | Accepted | |
| ADR-004 | PostgreSQL as initial system of record | Proposed | Schema verified; load test in Phase 1 |
| ADR-005 | PostgreSQL-backed scheduling for V1 | Proposed | Benchmark: lock contention, lease recovery latency, fairness at 50 workers / 1k runs/min |
| ADR-006 | Append-only execution journal | Accepted | Enforced by trigger |
| ADR-007 | Immutable agent versions | Accepted | Enforced by trigger |
| ADR-008 | Framework adapters with capability manifests | Accepted | Level derived from capabilities |
| ADR-009 | Central Tool Broker | Accepted | |
| ADR-010 | Cedar policy evaluation | Proposed | Behind `PolicyEvaluator` (A13); evaluate Python bindings |
| ADR-011 | OpenTelemetry integration | Accepted | GenAI semconv mapping is versioned |
| ADR-012 | Docker-based local deployment | Accepted | |
| ADR-013 | Open-source core | Accepted | Apache-2.0 |
| ADR-014 | Managed cloud as commercial offering | Proposed | |
| ADR-015 | Native execution with explicit durable steps | Accepted | Step key `name#occurrence`, matched by allocation ordinal (A5) |
| ADR-016 | Lease held only in `RUNNING`; waits exit via `QUEUED` | Accepted | A1 |
| ADR-017 | Temporal is a reference, not a backend, through V2 | Accepted | A9 |
| ADR-018 | Budget reserve → settle | Accepted | A6 (input tokens: provider count or byte-length upper bound) |
