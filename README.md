# MAIT

**The operating infrastructure for autonomous AI systems.**

MAIT makes AI agents **executable, manageable, observable, recoverable, governable and verifiable**.
It wraps agents you already have (LangGraph, OpenAI Agents SDK, MCP tools) or runs native MAIT
agents. Every execution gets the same operational envelope: an immutable version, a durable
journal, crash recovery with fencing, a brokered and policy-checked tool layer, budgets, human
approvals, and evaluation-gated promotion.

> Status: **Phase 0 — architecture and foundations.** Not usable yet. See the [roadmap](docs/ROADMAP.md).

## Design documents

| Document | What it defines |
|---|---|
| [RFC-001](docs/rfcs/RFC-001-mait-architecture.md) | The architectural baseline |
| [RFC-001 Amendments](docs/rfcs/RFC-001-amendments.md) | A1–A13: review changes and their decisions |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Boundaries, processes, layout, public API |
| [EXECUTION_MODEL](docs/EXECUTION_MODEL.md) | State machine, journal, leases and fencing, recovery, tool outcomes, budgets |
| [DATA_MODEL](docs/DATA_MODEL.md) | Entities, tenancy, immutability, write paths |
| [SECURITY](docs/SECURITY.md) | Threat model, isolation modes, secrets, policy, prompt injection |
| [ROADMAP](docs/ROADMAP.md) | Phases, exit criteria, acceptance matrices |
| [ADRs](docs/adr/README.md) | Frozen and proposed decisions |

## What exists today

- `packages/mait-core`: the pure, property-tested definition of MAIT's execution semantics:
  the lifecycle transition table, the event envelope, the fenced projection, tool recovery rules,
  adapter capability → integration level, and durable step matching.
- `infrastructure/migrations/0001_init.sql`: the canonical PostgreSQL schema with tenant-safe
  composite keys, immutability triggers and RLS, plus SQL invariant tests.

## Develop

```bash
uv sync
uv run pytest
uv run ruff check .
```

Database (optional for now):

```bash
docker compose -f infrastructure/docker/docker-compose.yml up -d
```

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
